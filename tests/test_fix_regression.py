#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试（fix 批次）：
- 共享单 IpcClient 并发 status/mount 无错配；注入错 id 必须跳过/抛错；重连 id 语义一致。
- 慢 mount（6-10s）期间独立 status 连接 <1s 返回，最终成功无死锁。
- 代理端口被占/启动失败时拒绝进入 NetFS，mount backend 零调用。
- 401 带体拒绝：旧连接收到 close，后续合法请求经新连接成功。

全部内存 fake / 回环；不连真实 NAS、不挂载、不用真实凭证。
"""
import importlib.util
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOUNTAPP = HERE.parent / "mountapp"
sys.path.insert(0, str(MOUNTAPP))

import client as prod_client  # noqa: E402
import ipc as prod_ipc  # noqa: E402


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, MOUNTAPP / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


service = _load("fix_service", "service.py")


class FakeDiscovery:
    def __init__(self, connected=True, port=9001):
        self.connected, self.port = connected, port

    def refresh(self, force=False):
        return service.DiscoveryView(
            connected=self.connected, mode="p2p", webdav_port=self.port,
            username="u" if self.connected else "",
            password="p" if self.connected else "",
            storage=[{"total": 100, "used": 30}], error="")

    def poll_storage(self):
        return [{"total": 100, "used": 30}]


class RecordingMount:
    def __init__(self, sleep=0.0):
        self.mounts = []
        self.sleep = sleep

    def ensure_capable(self):
        return True

    def mount(self, url, mountpoint, username, password):
        self.mounts.append({"url": url, "user": username, "has_pwd": bool(password)})
        if self.sleep:
            time.sleep(self.sleep)
        return True, ""

    def unmount(self, mountpoint):
        return True, ""

    def own_mountpoint(self, expected):
        return None

    def open_finder(self):
        return True, None


class RecordingProxy:
    def __init__(self, port, fail_start=False):
        self.port = port
        self.fail_start = fail_start
        self._running = False

    def start(self):
        if self.fail_start:
            raise OSError(98, "地址已占用")
        self._running = True
        return self.port

    def stop(self, timeout=5.0):
        self._running = False

    def is_running(self):
        return self._running

    def adopt(self, snap):
        return True


def _make_service(tmp, mount_registry=None, proxy_factory=None, interval=60.0):
    return service.ResidentService(
        {"auto_mount": False},
        state_path=os.path.join(tmp, "state.json"),
        socket_dir=os.path.join(tmp, "run"),
        discovery=FakeDiscovery(),
        mount_registry=mount_registry or RecordingMount(),
        proxy_factory=proxy_factory or (lambda p: RecordingProxy(p)),
        adopt_target=lambda snap: True,
        interval=interval,
    )


class SharedClientConcurrencyTest(unittest.TestCase):
    """T1：共享单 client 并发 status/mount 300+ 次无错配、无异常。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fix-t1-")
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name)
        self.svc.start()
        self.addCleanup(self.svc.stop)
        self.sock = Path(self.tmp.name) / "run" / "mwdav.sock"
        self._wait()

    def _wait(self, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.sock.exists():
                return
            time.sleep(0.05)
        raise AssertionError("socket 未就绪")

    def test_shared_client_300_calls_no_mismatch(self):
        c = prod_client.IpcClient(str(self.sock))
        c.connect()
        self.addCleanup(c.close)
        errors = []
        lock = threading.Lock()

        def status_loop(n):
            for _ in range(n):
                try:
                    data = c.status()
                    self.assertIn("connected", data)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(("status", repr(e)))

        def mount_loop(n):
            for _ in range(n):
                try:
                    data = c.call("mount")
                    self.assertNotIn("connected", data)
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errors.append(("mount", repr(e)))

        threads = [
            threading.Thread(target=status_loop, args=(150,)),
            threading.Thread(target=status_loop, args=(150,)),
            threading.Thread(target=mount_loop, args=(75,)),
            threading.Thread(target=mount_loop, args=(75,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [], f"并发出现异常: {errors[:5]}")


class ResponseIdTest(unittest.TestCase):
    """T2：响应 id 错配/缺失抛专属协议错误并断连；非 ok 响应抛 RuntimeError。"""

    @staticmethod
    def _stub_client():
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        c._sock = type("S", (), {"sendall": lambda self, b: None,
                                  "close": lambda self: None})()
        return c

    def _reader(self, line):
        class _R:
            def readline(self):
                return line

            def close(self):
                pass
        return _R()

    def test_wrong_id_raises_protocol_error_and_invalidates(self):
        c = self._stub_client()
        c._reader = self._reader(b'{"ok": true, "id": 99, "data": {}}\n')
        with self.assertRaises(prod_client.IpcClientProtocolError):
            c._exchange("status", {})
        self.assertIsNone(c._sock, "错 id 后应失效连接")

    def test_missing_id_raises_protocol_error(self):
        c = self._stub_client()
        c._reader = self._reader(b'{"ok": true, "data": {}}\n')
        with self.assertRaises(prod_client.IpcClientProtocolError):
            c._exchange("status", {})

    def test_non_ok_response_raises(self):
        c = self._stub_client()
        c._reader = self._reader(b'{"ok": false, "id": 1, "error": "boom", "data": null}\n')
        with self.assertRaises(RuntimeError):
            c._call_once("status", {})


class SlowMountStatusTimelyTest(unittest.TestCase):
    """T3：慢 mount（7s）期间独立 status 连接 <1s 返回，最终成功无死锁。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fix-t3-")
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make_service(self.tmp.name, mount_registry=RecordingMount(sleep=7.0))
        self.svc.start()
        self.addCleanup(self.svc.stop)
        self.sock = Path(self.tmp.name) / "run" / "mwdav.sock"
        end = time.monotonic() + 5
        while time.monotonic() < end and not self.sock.exists():
            time.sleep(0.05)

    def test_status_returns_during_slow_mount(self):
        status_c = prod_client.IpcClient(str(self.sock))
        status_c.connect()
        self.addCleanup(status_c.close)
        cmd_c = prod_client.IpcClient(str(self.sock))
        cmd_c.connect()
        self.addCleanup(cmd_c.close)

        result = {}

        def do_mount():
            result["mount"] = cmd_c.call("mount")

        t = threading.Thread(target=do_mount)
        t.start()
        time.sleep(0.5)  # 让 mount 持锁进入慢路径

        start = time.monotonic()
        snap = status_c.status()
        elapsed = time.monotonic() - start
        self.assertIn("connected", snap)
        self.assertLess(elapsed, 1.0, f"status 耗时 {elapsed:.3f}s 未及时返回")

        t.join(15)
        self.assertFalse(t.is_alive(), "mount 线程未结束，疑似死锁")
        self.assertIn("mounted", result["mount"])


class PortOccupationRejectsMountTest(unittest.TestCase):
    """T4：代理启动失败（端口被占）→ 拒绝挂载，mount backend 零调用。"""

    def test_mount_rejected_without_netfs_call(self):
        tmp = tempfile.TemporaryDirectory(prefix="fix-t4-")
        self.addCleanup(tmp.cleanup)
        registry = RecordingMount()
        svc = _make_service(
            tmp.name, mount_registry=registry,
            proxy_factory=lambda p: RecordingProxy(p, fail_start=True))
        svc._view = service.DiscoveryView(
            connected=True, mode="p2p", webdav_port=9001,
            username="u", password="p", storage=[], error="")
        with self.assertRaises(ValueError) as cm:
            svc._cmd_mount({})
        self.assertIn("代理未启动", str(cm.exception))
        self.assertEqual(registry.mounts, [], "代理未启动仍调用了 mount backend（NetFS）")


class StatusViewNoProbeTest(unittest.TestCase):
    """N-B：status_view 是纯内存快照，不探测后端、不覆盖已完成挂载。"""

    def test_status_view_does_not_probe_or_overwrite(self):
        class CountingMount:
            def __init__(self):
                self.own_calls = 0
                self.mounts = []

            def own_mountpoint(self, expected):
                self.own_calls += 1
                return expected

            def mount(self, *a):
                self.mounts.append(a)
                return True, ""

        tmp = tempfile.TemporaryDirectory(prefix="fix-nb-")
        self.addCleanup(tmp.cleanup)
        registry = CountingMount()
        svc = _make_service(tmp.name, mount_registry=registry)
        # 已完成挂载的内存状态
        svc._mounted = True
        svc._mount_path = "/Volumes/127.0.0.1"
        snap = svc.status_view()
        self.assertTrue(snap["mounted"])
        self.assertEqual(snap["mount_path"], "/Volumes/127.0.0.1")
        # status_view 不得触发后端探测
        self.assertEqual(registry.own_calls, 0, "status_view 调用了 own_mountpoint")
        # 再读一次，已完成挂载不被改写
        snap2 = svc.status_view()
        self.assertTrue(snap2["mounted"])
        self.assertEqual(snap2["mount_path"], "/Volumes/127.0.0.1")


class Auth401BodyCloseTest(unittest.TestCase):
    """T5：401 带体拒绝立即关闭、无 stall；旧连接 EOF；残留字节不毒化下一请求；
    新连接合法请求成功。"""

    def _read_until_eof(self, sock, deadline=1.0):
        sock.settimeout(deadline)
        chunks = []
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        except socket.timeout:
            pass
        return b"".join(chunks)

    def _bootstrap(self):
        ss = _load("fix_state", "state_store.py")
        proxy_mod = _load("fix_proxy", "mwdav_proxy.py")
        tmp = tempfile.TemporaryDirectory(prefix="fix-t5-")
        self.addCleanup(tmp.cleanup)
        state = os.path.join(tmp.name, "state.json")
        ss.publish_target(state, port=9001, username="u", password="p")
        proxy_mod.STATE_FILE = state
        srv = proxy_mod.ProxyServer(0)
        srv.start()
        self.addCleanup(srv.stop)
        return srv.httpd.server_port

    def test_401_no_stall_eof_and_no_poisoning(self):
        port = self._bootstrap()
        # 1) 错凭证 + 1024B 带体，客户端不 EOF：服务须立即回 401+close，
        #    不 drain、不 stall（<1s）。
        body = b"x" * 1024
        req = (
            "PUT /upload.txt HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "Authorization: Basic aaa\r\n"
            "Content-Length: 1024\r\n\r\n"
        ).encode() + body
        a = socket.create_connection(("127.0.0.1", port), timeout=5)
        a.sendall(req)
        start = time.monotonic()
        resp_a = self._read_until_eof(a, deadline=1.0)
        elapsed = time.monotonic() - start
        text = resp_a.decode("utf-8", "replace")
        self.assertIn("401", text)
        self.assertIn("Connection: close", text)
        self.assertLess(elapsed, 1.0, f"401 路径 stall 了 {elapsed:.3f}s")
        # 2) 旧连接读取完整 401 后应在 1s 内 EOF（服务主动关闭）。
        # 3) 毒化检查：401 后在同一连接再塞残留 body + GET，不得被当作新请求
        #    处理（不得出现 501 或任何 HTTP 状态行）；连接已关闭。
        try:
            a.sendall(b"LEFTOVER-BODY\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n")
            poison = self._read_until_eof(a, deadline=0.5)
        except OSError:
            poison = b""
        self.assertNotIn(b"501", poison)
        self.assertFalse(poison.strip(), f"残留字节被误处理为响应: {poison!r}")
        a.close()

        # 4) 新连接：正确凭证 GET 不再被 401 拒绝（无上游 502 亦可，关键是认证通过）。
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/", headers={"Authorization": "Basic " +
                                          __import__("base64").b64encode(b"u:p").decode()})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertNotEqual(r.status, 401, "新连接合法请求被误拒")
        self.assertIn(r.status, (200, 502, 503))


class WriteNoReplayTest(unittest.TestCase):
    """写命令断线：结果未知报错，绝不自动重放；只读命令可重连重试一次。"""

    @staticmethod
    def _dummy_sock():
        return type("S", (), {"sendall": lambda self, b: None,
                              "close": lambda self: None})()

    def test_write_disconnect_raises_without_replay(self):
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        sends = {"n": 0}

        class _Boom:
            def sendall(self, b):
                sends["n"] += 1
                raise OSError("connection reset")

        c._sock = _Boom()
        c._reader = object()  # 跳过懒 makefile；sendall 即抛错
        c._open = lambda: None  # 避免真实 connect；仅验证不重放
        with self.assertRaises(RuntimeError) as cm:
            c.call("mount")
        self.assertIn("结果未知", str(cm.exception))
        self.assertEqual(sends["n"], 1, "写命令断线后被自动重放了")

    def test_readonly_retries_after_reconnect(self):
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        state = {"fail": True}

        class _Boom:
            def sendall(self, b):
                if state["fail"]:
                    raise ConnectionError("drop")
                return None

        c._sock = _Boom()
        c._reader = object()  # 跳过懒 makefile

        class _GoodReader:
            def readline(self):
                return b'{"ok": true, "id": 2, "data": {"connected": true}}\n'

        def _fake_open():
            state["fail"] = False
            c._reader = _GoodReader()
            c._sock = self._dummy_sock()

        c._open = _fake_open
        data = c.call("status")
        self.assertTrue(data["connected"])


class GuiCacheNonBlockingTest(unittest.TestCase):
    """L4：状态快照后台拉取写缓存，主线程读缓存不被慢/阻塞 status 卡住。"""

    def test_main_thread_reads_cache_without_blocking(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fix_main", str(MOUNTAPP / "main.py"))
        main_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main_mod)

        class SlowStatus:
            def status(self):
                time.sleep(5.0)
                return {"connected": True, "mounted": False, "storage": []}

        class FakeCmd:
            def call(self, cmd, args=None):
                return {}

        core = main_mod._ServiceCore(SlowStatus(), FakeCmd(), pump=True)
        self.addCleanup(core._stop.set)
        start = time.monotonic()
        snap = core._status()  # 主线程同步读，必须立即返回空缓存而非等待 5s
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5, f"主线程读缓存阻塞了 {elapsed:.3f}s")
        self.assertFalse(snap["connected"])
        # 后台线程最终会把快照填入缓存
        end = time.monotonic() + 6
        while time.monotonic() < end and not core._cached:
            time.sleep(0.05)
        self.assertTrue(core._cached["connected"])


class ProtocolErrorRecoveryTest(unittest.TestCase):
    """协议错误当前请求不重试；下一请求起始 ensure 重建后可恢复。"""

    def test_wrong_id_then_next_status_recovers(self):
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        c._sock = type("S", (), {"sendall": lambda self, b: None,
                                 "close": lambda self: None})()
        c._reader = type("R", (), {
            "readline": lambda self: b'{"ok":true,"id":99,"data":{}}\n',
            "close": lambda self: None})()
        with self.assertRaises(prod_client.IpcClientProtocolError):
            c.call("status")
        self.assertIsNone(c._sock, "协议错误后应失效连接")

        # 下一请求起始 ensure 重建后，正确 id 的 status 可恢复
        def _good_open():
            c._reader = type("R", (), {
                "readline": lambda self:
                    b'{"ok":true,"id":2,"data":{"connected":true}}\n',
                "close": lambda self: None})()
            c._sock = type("S", (), {
                "sendall": lambda self, b: None,
                "close": lambda self: None})()
        c._open = _good_open
        data = c.call("status")
        self.assertTrue(data["connected"])

    def test_write_error_open_failure_keeps_unknown_result_one_send(self):
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        sends = {"n": 0}

        class _Boom:
            def sendall(self, b):
                sends["n"] += 1
                raise OSError("read/executed after")

        c._sock = _Boom()
        c._reader = object()

        def _fail_open():
            raise OSError("rebuild unavailable")

        c._open = _fail_open
        with self.assertRaises(RuntimeError) as cm:
            c.call("mount")
        self.assertIn("结果未知", str(cm.exception))
        self.assertEqual(sends["n"], 1, "写命令被重放")


class ConnectionAbsentTest(unittest.TestCase):
    """B：初始 socket 缺失 / 错 id 后 socket 消失，抛 ConnectionError 而非
    AttributeError；服务恢复后下一请求可成功；connect 失败保证 _sock 为 None。"""

    def _good_open(self, c):
        def _open():
            c._reader = type("R", (), {
                "readline": lambda self:
                    b'{"ok":true,"id":2,"data":{"connected":true}}\n',
                "close": lambda self: None})()
            c._sock = type("S", (), {
                "sendall": lambda self, b: None,
                "close": lambda self: None})()
        return _open

    def test_initial_socket_missing_raises_connection_error_then_recovers(self):
        c = prod_client.IpcClient("/tmp/absent-x.sock")
        # 未连接直接调用：明确 ConnectionError，而非 None.sendall AttributeError
        with self.assertRaises(ConnectionError):
            c.call("status")
        # 服务恢复后下一请求成功
        def _open1():
            c._reader = type("R", (), {
                "readline": lambda self:
                    b'{"ok":true,"id":1,"data":{"connected":true}}\n',
                "close": lambda self: None})()
            c._sock = type("S", (), {
                "sendall": lambda self, b: None,
                "close": lambda self: None})()
        c._open = _open1
        data = c.call("status")
        self.assertTrue(data["connected"])

    def test_wrong_id_then_socket_gone_no_attribute_error(self):
        c = prod_client.IpcClient("/tmp/x")
        c._seq = 0
        c._sock = type("S", (), {"sendall": lambda self, b: None,
                                  "close": lambda self: None})()
        c._reader = type("R", (), {
            "readline": lambda self: b'{"ok":true,"id":99,"data":{}}\n',
            "close": lambda self: None})()
        with self.assertRaises(prod_client.IpcClientProtocolError):
            c.call("status")
        # 错 id 断连后 socket 消失：下一请求抛 ConnectionError，不 AttributeError
        c._sock = None
        with self.assertRaises(ConnectionError):
            c.call("status")
        # 服务恢复后下一请求成功
        c._open = self._good_open(c)
        data = c.call("status")
        self.assertTrue(data["connected"])

    def test_connect_refused_cleans_local_sock(self):
        c = prod_client.IpcClient("/tmp/absent-x.sock")
        # 路径存在但拒绝连接：connect 失败应抛 OSError 且 self._sock 归 None
        import unittest.mock as mock
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(socket.socket, "connect",
                               side_effect=OSError("refused")):
            with self.assertRaises(OSError):
                c.connect()
        self.assertIsNone(c._sock)
        self.assertIsNone(c._reader)


if __name__ == "__main__":
    unittest.main()
