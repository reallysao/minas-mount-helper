#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻单服务架构（runtime owner）验收自测。

全部 in-process 线程 + 临时目录 + fake discovery/mount/proxy/adopt：
不连真实 NAS、不执行真实 /sbin/mount/netfs、不用真实凭证。
覆盖：单服务存活、GUI 断开继续 A→B 发布、IPC 脱敏与串行、单实例、安全关停、只读无副作用。
"""
import os
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)

import service as S
from service import ResidentService, DiscoveryView
from client import IpcClient

FIXTURE_USER = "fixture-user"
FIXTURE_PASSWORD = "fixture-pw-not-real-0000000000"


class FakeDiscovery:
    """脚本化候选序列；refresh 依次返回，便于观测 A→B。"""

    def __init__(self, views):
        self.views = list(views)
        self.calls = 0
        self.polls = 0

    def refresh(self, force=False):
        v = self.views[min(self.calls, len(self.views) - 1)]
        self.calls += 1
        return v

    def poll_storage(self):
        self.polls += 1
        return []


class FakeMount:
    """内存挂载 registry：记录意图，绝不碰真实路径/二进制。"""

    def __init__(self):
        self.mounts = []
        self.unmounts = []
        self.mounted_point = None

    def ensure_capable(self):
        return True

    def mount(self, url, mountpoint, username, password):
        self.mounts.append((mountpoint, username, password))
        self.mounted_point = mountpoint
        return True, "ok"

    def unmount(self, mountpoint):
        self.unmounts.append(mountpoint)
        self.mounted_point = None
        return True, "ok"

    def own_mountpoint(self, expected):
        return self.mounted_point

    def open_finder(self):
        return True, self.mounted_point


class FakeProxy:
    """仅生命周期；不提供 adopt，目标经注入 adopt_target 采纳。"""

    def __init__(self, port):
        self.port = port
        self.running = False

    @property
    def listen_port(self):
        return self.port

    def start(self):
        self.running = True
        return self.port

    def stop(self, timeout=5.0):
        self.running = False

    def is_running(self):
        return self.running


def view_a():
    return DiscoveryView(True, "p2p", 40001, FIXTURE_USER, FIXTURE_PASSWORD, [], "")


def view_b():
    return DiscoveryView(True, "p2p", 40002, FIXTURE_USER, FIXTURE_PASSWORD, [], "")


class ResidentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="svc-")
        self.dir = self.tmp.name
        self.state_path = os.path.join(self.dir, "state.json")
        self.adopted = []

    def tearDown(self):
        self.tmp.cleanup()

    def make_service(self, views, interval=0.05, mount=None):
        disc = FakeDiscovery(views)
        reg = mount or FakeMount()
        svc = ResidentService(
            {"auto_mount": False},
            state_path=self.state_path,
            socket_dir=self.dir,
            discovery=disc,
            mount_registry=reg,
            proxy_factory=lambda port: FakeProxy(port),
            adopt_target=lambda snap: self.adopted.append((snap.port, snap.generation)),
            interval=interval)
        return svc, disc, reg


class TestSingleServiceLifecycle(ResidentBase):
    def test_single_service_alive_and_clean_stop(self):
        svc, disc, reg = self.make_service([view_a()])
        svc.start()
        try:
            # 等待至少一轮发布
            deadline = time.time() + 3
            while time.time() < deadline and not self.adopted:
                time.sleep(0.02)
            self.assertTrue(self.adopted, "服务未发布目标")
            self.assertTrue(svc._proxy.is_running())
            st = svc.status_view()
            self.assertTrue(st["connected"])
            self.assertEqual(st["webdav_port"], 40001)
            # 脱敏：status 不含凭证
            self.assertNotIn(FIXTURE_PASSWORD, str(st))
        finally:
            svc.stop()
        self.assertFalse(svc._proxy.is_running())
        self.assertFalse(os.path.exists(svc.socket_path))

    def test_second_instance_refused_one_proxy(self):
        svc, _, _ = self.make_service([view_a()])
        svc.start()
        try:
            svc2, _, _ = self.make_service([view_a()])
            with self.assertRaises(S.InstanceAlreadyRunning):
                svc2.start()
        finally:
            svc.stop()

    def test_stop_idempotent_no_orphan(self):
        svc, _, _ = self.make_service([view_a()])
        svc.start()
        svc.stop()
        svc.stop()  # 重复停止不崩
        self.assertFalse(os.path.exists(svc.socket_path))


class TestGuiExitKeepsService(ResidentBase):
    def test_client_disconnect_service_continues_ab_rotation(self):
        svc, disc, reg = self.make_service([view_a(), view_b()])
        svc.start()
        try:
            # 客户端连上读一次后断开（模拟 GUI 关闭）
            with IpcClient(svc.socket_path) as c:
                st = c.status()
                self.assertTrue(st["connected"])
                self.assertNotIn(FIXTURE_PASSWORD, str(st))
            # 断开后服务继续：等待 A→B 两次发布，代次单调
            deadline = time.time() + 4
            while time.time() < deadline and len(self.adopted) < 2:
                time.sleep(0.02)
            self.assertEqual(len(self.adopted), 2)
            ports = [p for p, _ in self.adopted]
            self.assertEqual(ports, [40001, 40002])
            gens = [g for _, g in self.adopted]
            self.assertLess(gens[0], gens[1], "generation 必须单调不回退")
            # 状态文件由同一 path 发布，读到 B
            import state_store
            snap = state_store.load_snapshot(self.state_path)
            self.assertEqual(snap.port, 40002)
        finally:
            svc.stop()


class TestIpcContract(ResidentBase):
    def _serve(self, views=None, mount=None):
        svc, disc, reg = self.make_service(views or [view_a()], mount=mount)
        svc.start()
        return svc, disc, reg

    def test_status_redacted_and_readonly_no_side_effect(self):
        svc, disc, _ = self._serve()
        try:
            before = disc.calls
            with IpcClient(svc.socket_path) as c:
                st = c.status()
            self.assertEqual(disc.calls, before, "status 不得触发发现/发布")
            self.assertNotIn("password", st)
            self.assertNotIn(FIXTURE_PASSWORD, str(st))
            self.assertNotIn(FIXTURE_USER, str(st))
        finally:
            svc.stop()

    def test_unknown_command_rejected_no_side_effect(self):
        svc, _, _ = self._serve()
        try:
            with IpcClient(svc.socket_path) as c:
                with self.assertRaises(RuntimeError):
                    c.call("publish_arbitrary_target", {"port": 1})
                with self.assertRaises(RuntimeError):
                    c.call("bogus")
        finally:
            svc.stop()

    def test_set_config_validation(self):
        svc, _, _ = self._serve()
        try:
            with IpcClient(svc.socket_path) as c:
                # 合法布尔接受
                out = c.call("set_config", {"auto_mount": False, "auto_start": True})
                self.assertFalse(out["auto_mount"])
                # 类型错误拒绝
                with self.assertRaises(RuntimeError):
                    c.call("set_config", {"auto_mount": "yes"})
                # .. 逃逸拒绝
                with self.assertRaises(RuntimeError):
                    c.call("set_config", {"mount_dir": "/Volumes/../../etc"})
                # 非 /Volumes 绝对路径（如 /tmp/x）拒绝
                with self.assertRaises(RuntimeError):
                    c.call("set_config", {"mount_dir": "/tmp/x"})
                # 未知键拒绝
                with self.assertRaises(RuntimeError):
                    c.call("set_config", {"bogus_key": 1})
        finally:
            svc.stop()

    def test_commands_serialized_concurrent_all_ok(self):
        svc, _, _ = self._serve()
        try:
            errors = []

            def worker():
                try:
                    with IpcClient(svc.socket_path) as c:
                        for _ in range(5):
                            c.status()
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(errors, [])
        finally:
            svc.stop()

    def test_mount_uses_fake_registry_no_real_path(self):
        reg = FakeMount()
        svc, _, _ = self._serve(mount=reg)
        try:
            with IpcClient(svc.socket_path) as c:
                out = c.call("mount")
            self.assertTrue(out["mounted"])
            self.assertEqual(len(reg.mounts), 1, "意图只应记录一次")
            # 凭证不出现在命令回显
            self.assertNotIn(FIXTURE_PASSWORD, str(out))
        finally:
            svc.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
