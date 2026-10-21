#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产挂载适配器回归：服务发现目标必须直达 AppCore 显式凭证入口。"""
import os
import sys
import unittest
from unittest.mock import Mock

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)

from service import SystemMount  # noqa: E402


class ProductionMountAdapterTest(unittest.TestCase):
    def test_service_credentials_are_forwarded_to_core(self):
        core = Mock()
        core.mount_with_credentials.return_value = (True, "挂载成功")
        core.conn.snapshot.return_value = {
            "connected": False, "webdav_port": None,
            "username": "", "password": "",
        }
        adapter = SystemMount(core)

        result = adapter.mount(
            "http://127.0.0.1:18445/", "/Volumes/127.0.0.1",
            "fixture-user", "fixture-password-not-real")

        self.assertEqual(result, (True, "挂载成功"))
        core.mount_with_credentials.assert_called_once_with(
            "http://127.0.0.1:18445/", "/Volumes/127.0.0.1",
            "fixture-user", "fixture-password-not-real")
        core.mount.assert_not_called()
        core.conn.snapshot.assert_not_called()

    def test_adapter_returns_core_failure_without_fallback(self):
        core = Mock()
        core.mount_with_credentials.return_value = (False, "helper 协议过旧，需重新编译")
        adapter = SystemMount(core)

        result = adapter.mount(
            "http://127.0.0.1:18445/", "/Volumes/127.0.0.1",
            "fixture-user", "fixture-password-not-real")

        self.assertFalse(result[0])
        self.assertIn("helper", result[1])
        core.mount.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
