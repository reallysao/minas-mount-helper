#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T07/T08：只认领本代理回环端点的卷；自定义 mount_dir 须实测。
另验：旧 helper（无能力标记）被拒绝；NetFS 凭证走 stdin、不进 argv（SR-4）。

本地 mock，不执行真实 /sbin/mount、不挂载、不触网。
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
MOUNTAPP = __import__("os").path.abspath(
    __import__("os").path.join(HERE, "..", "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)

import app_core as ac

OURS = "//u@127.0.0.1:18445/ on /Volumes/127.0.0.1 (webdav, nodev, local)"
OTHER = "//other@10.0.0.9:2345/ on /Volumes/UnrelatedDAV (webdav, nodev)"
SECRET = "fixture-secret-pw-1234567890"


def _core(mount_dir=""):
    core = object.__new__(ac.AppCore)
    core.config = {"mount_dir": mount_dir}
    core.mount_path = None
    core.mounted = False
    return core


class MountIdentity(unittest.TestCase):
    # T07：任意 WebDAV 卷不被认领（表里只有他卷，不得认领）
    def test_t07_unrelated_webdav_volume_not_claimed(self):
        core = _core()
        with patch.object(ac.subprocess, "run",
                          return_value=SimpleNamespace(stdout=f"{OTHER}\n", stderr="")):
            self.assertIsNone(core.find_mount_path())

    def test_t07_own_loopback_volume_claimed(self):
        core = _core()
        with patch.object(ac.subprocess, "run",
                          return_value=SimpleNamespace(stdout=f"{OURS}\n", stderr="")):
            self.assertEqual(core.find_mount_path(), "/Volumes/127.0.0.1")

    # T08：自定义 mount_dir 须在挂载表中实测归属
    def test_t08_custom_mount_dir_string_alone_is_not_mounted(self):
        core = _core("/fixture/not-mounted")
        with patch.object(ac.subprocess, "run",
                          return_value=SimpleNamespace(stdout=f"{OURS}\n", stderr="")):
            self.assertFalse(core.is_mounted())

    def test_t08_custom_mount_dir_verified_in_table(self):
        core = _core("/fixture/mounted")
        line = "//u@127.0.0.1:18445/ on /fixture/mounted (webdav, nodev)"
        with patch.object(ac.subprocess, "run",
                          return_value=SimpleNamespace(stdout=f"{line}\n", stderr="")):
            self.assertTrue(core.is_mounted())

    # 纯函数：解析只认本代理 host:port
    def test_parse_table_matches_only_own_proxy(self):
        entries = ac.parse_mount_table([OURS, OTHER])
        own = [e for e in entries if e["host"] == "127.0.0.1" and e["port"] == 18445]
        self.assertEqual(len(own), 1)
        self.assertEqual(own[0]["mountpoint"], "/Volumes/127.0.0.1")

    # 旧 helper（无 --protocol 标记）被拒绝，不调用旧二进制冒充成功
    def test_old_helper_without_marker_is_rejected(self):
        core = _core()
        captured = {"mount_called": False}

        def fake_popen(args, **kw):
            captured["mount_called"] = "mount" in args

            def fake_communicate(**k):
                captured["stdin"] = k.get("input")
                return b"", b""

            return SimpleNamespace(communicate=fake_communicate)

        snap = {"connected": True, "webdav_port": 49559,
                "username": "u-fixture", "password": SECRET}
        core.conn = SimpleNamespace(snapshot=lambda: snap)
        with patch.object(core, "_publish_target", return_value=True), \
             patch.object(core, "ensure_proxy", return_value=True), \
             patch.object(core, "is_mounted", return_value=False), \
             patch.object(core, "_helper_supported", return_value=False), \
             patch.object(ac.subprocess, "Popen", side_effect=fake_popen):
            ok, msg = core.mount()
        self.assertFalse(ok)
        self.assertFalse(captured["mount_called"])
        self.assertIn("重新编译", msg)

    # 新协议：argv 不含密码，密码经 stdin 交付
    def test_credentials_via_stdin_not_argv(self):
        captured = {}

        def fake_popen(args, **kw):
            captured["args"] = list(args)

            def fake_communicate(**k):
                captured["stdin"] = k.get("input")
                return b"MOUNT_OK\n", b""

            return SimpleNamespace(communicate=fake_communicate)

        core = _core()
        snap = {"connected": True, "webdav_port": 49559,
                "username": "u-fixture", "password": SECRET}
        core.conn = SimpleNamespace(snapshot=lambda: snap)
        with patch.object(core, "_publish_target", return_value=True), \
             patch.object(core, "ensure_proxy", return_value=True), \
             patch.object(core, "is_mounted", return_value=False), \
             patch.object(core, "_helper_supported", return_value=True), \
             patch.object(ac.subprocess, "Popen", side_effect=fake_popen):
            ok, _ = core.mount()
        self.assertTrue(ok)
        self.assertNotIn(SECRET, " ".join(captured["args"]))
        self.assertEqual(captured["args"][:3], [ac.NETFS_MOUNT, "mount", ac.MOUNT_URL])
        self.assertIn(SECRET, captured["stdin"].decode())


if __name__ == "__main__":
    unittest.main(verbosity=2)
