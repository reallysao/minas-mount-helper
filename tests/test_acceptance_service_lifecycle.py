#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · ResidentService 生命周期（审查者独占 test_acceptance_service_*）。

构造注入 fake discovery/mount/proxy；进程内线程 + 真实 fork 进程双形态；
不连真实 NAS、不挂载、不碰真实路径。覆盖：断开存活、A→B 继续发布并采纳、
失败→恢复、单实例锁、安全停止。
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance_service_fakes as F  # noqa: E402
from acceptance_service_fakes import (  # noqa: E402
    ScriptedDiscovery, RecordingMount, RecordingProxy, view, IpcClient, wait_until)

# 加载生产 service（fakes 模块已把 mountapp 挂上 sys.path）
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "acc_service", F._MOUNTAPP / "service.py")
service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service)


def _make(tmp, views, interval=0.2):
    # 注入 adopt_target 记录采纳序列（生产默认委托全局 UP.adopt；ProxyServer 无实例
    # adopt，§8.6 与 ProxyServer.adopt 缺口另行上报，不在此修补）。
    rp = RecordingProxy(18445)
    svc = service.ResidentService(
        state_path=str(Path(tmp) / "state.json"),
        socket_dir=str(Path(tmp) / "run"),
        discovery=ScriptedDiscovery(views),
        mount_registry=RecordingMount(),
        proxy_factory=lambda p: rp,
        adopt_target=rp.adopt,
        proxy_port=18445,
        stop_timeout=2.0,
        interval=interval)
    return svc, rp


class ServiceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="acc-svc-")
        self.addCleanup(self.tmp.cleanup)

    # ---- SL-1：connected 即发布并采纳，代次单调，status 无凭证 ----
    def test_sl1_publish_adopt_and_status_redacted(self):
        svc, rp = _make(self.tmp.name, [view(True, 9001)])
        svc.start()
        self.addCleanup(svc.stop)
        wait_until(lambda: rp.adopted)
        last = rp.adopted[-1]
        self.assertEqual(last["port"], 9001)
        self.assertEqual(svc.last_generation(), 1)
        st = svc.status_view()
        self.assertTrue(st["connected"])
        blob = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("password", blob.lower())
        self.assertNotIn(F.FIXTURE_PASSWORD, blob)

    # ---- SL-2：GUI 不在线，A→B 轮换继续 CAS 发布并采纳 B，A 不再被采纳 ----
    def test_sl2_rotation_a_to_b_continues_without_gui(self):
        svc, rp = _make(self.tmp.name, [view(True, 9001), view(True, 9002)])
        svc.start()
        self.addCleanup(svc.stop)
        wait_until(lambda: len(rp.adopted) >= 2, timeout=8)
        ports = [a["port"] for a in rp.adopted]
        self.assertEqual(ports, [9001, 9002])
        self.assertEqual(rp.adopted[-1]["port"], 9002)
        gens = [a["generation"] for a in rp.adopted]
        self.assertEqual(gens, sorted(gens))
        self.assertEqual(svc.last_generation(), 2)
        # 状态文件目标 = B
        import state_store
        loaded = state_store.load_snapshot(str(Path(self.tmp.name) / "state.json"))
        self.assertEqual(loaded.port, 9002)

    # ---- SL-3：失败窗口→恢复，不崩溃，恢复后重新发布采纳 ----
    def test_sl3_failure_then_recovery_republishes(self):
        svc, rp = _make(self.tmp.name, [
            view(True, 9001),
            view(False, None, error="p2p down"),
            view(True, 9003),
        ])
        svc.start()
        self.addCleanup(svc.stop)
        wait_until(lambda: any(a["port"] == 9003 for a in rp.adopted), timeout=8)
        self.assertEqual(rp.adopted[-1]["port"], 9003)
        self.assertTrue(svc.status_view()["connected"])

    # ---- SL-4：第二实例抢锁失败，不起代理、不双写 ----
    def test_sl4_single_instance_lock(self):
        svc, rp = _make(self.tmp.name, [view(True, 9001)])
        svc.start()
        self.addCleanup(svc.stop)
        wait_until(lambda: rp.adopted)
        second, _ = _make(self.tmp.name, [view(True, 9009)])
        with self.assertRaises(service.InstanceAlreadyRunning):
            second.start()
        self.assertEqual(rp.adopted[-1]["port"], 9001)

    # ---- SL-5：stop 幂等、停代理、清 socket、停后不再发布 ----
    def test_sl5_safe_stop_idempotent_no_more_publish(self):
        svc, rp = _make(self.tmp.name, [view(True, 9001)])
        svc.start()
        wait_until(lambda: rp.adopted)
        before = len(rp.adopted)
        svc.stop()
        svc.stop()  # 幂等
        self.assertTrue(rp.stopped >= 1)
        sock = Path(self.tmp.name) / "run" / "mwdav.sock"
        self.assertFalse(sock.exists())
        time.sleep(0.3)
        self.assertEqual(len(rp.adopted), before)
        self.assertEqual(svc.last_generation(), 1)

    # ---- SL-6：真实 fork 进程 serve()：客户端断开服务仍 alive，refresh 继续，stop 干净退出 ----
    def test_sl6_real_process_survives_client_disconnect(self):
        driver = HERE / "acceptance_service_driver.py"
        env = dict(os.environ)
        env["ACC_SOCKET_DIR"] = str(Path(self.tmp.name) / "run")
        env["ACC_STATE_PATH"] = str(Path(self.tmp.name) / "state.json")
        env["ACC_INTERVAL"] = "0.3"
        proc = subprocess.Popen(
            [sys.executable, str(driver)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # 关闭子进程管道 BufferedReader，避免 ResourceWarning 泄漏。
        self.addCleanup(lambda: (proc.stdout.close(), proc.stderr.close()))
        sock = Path(env["ACC_SOCKET_DIR"]) / "mwdav.sock"
        wait_until(lambda: sock.exists(), timeout=8)

        # 第一个客户端连上读 status，然后断开（非 SIGTERM 服务）
        c = IpcClient(str(sock))
        r1 = c.call("status")
        self.assertTrue(r1["ok"])
        c.close()
        # 服务仍 alive
        os.kill(proc.pid, 0)
        # 新客户端 refresh 并读 status，服务继续响应
        c2 = IpcClient(str(sock))
        rr = c2.call("refresh")
        self.assertTrue(rr["ok"], rr)
        st = c2.call("status")
        self.assertTrue(st["ok"])
        c2.close()
        # 安全停止：stop 会先关 socket 再回 ack（响应竞态），发令后不等 ack，
        # 直接轮询进程干净退出；不断言 ack 报文到达，避免夹具阻塞。
        import socket as _sock
        s3 = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s3.connect(str(sock))
        s3.sendall((json.dumps({"cmd": "stop", "id": 3, "args": {}}) + "\n").encode())
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("serve() stop 后未干净退出")
        finally:
            s3.close()
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
