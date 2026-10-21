#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LaunchAgent 模板/安装卸载可测性：fake runner( argv 列表 ) + 临时目录，不写真实系统。

域用当前真实 uid（gui/{uid}），不硬编码 501；注入 uid 时 argv 必须反映指定 uid。
"""
import os
import plistlib
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)

import launch_agent as la


class LaunchAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="la-")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def _runner(self, argv):
        self.calls.append(list(argv))

    def _parse(self, path):
        with open(path, "rb") as f:
            return plistlib.load(f)

    def test_plist_constrained_keepalive_and_service_entry(self):
        parsed = self._parse_bytes(la.build_plist("/opt/launcher.sh"))
        self.assertEqual(parsed["ProgramArguments"], ["/opt/launcher.sh", "--service"])
        self.assertTrue(parsed["RunAtLoad"])
        # 受限 KeepAlive：仅崩溃重启，正常退出不强拉；有限节流，避免无限重启风暴
        self.assertTrue(parsed["KeepAlive"]["Crashed"])
        self.assertFalse(parsed["KeepAlive"]["SuccessfulExit"])
        self.assertEqual(parsed["ThrottleInterval"], 10)

    def _parse_bytes(self, data):
        return plistlib.loads(data)

    def test_special_char_launcher_path_xml_escaped(self):
        weird = "/Applications/小米 & Co/launcher<.sh>"
        parsed = self._parse_bytes(la.build_plist(weird))
        self.assertEqual(parsed["ProgramArguments"][0], weird)

    def test_domain_uses_current_uid_not_hardcoded_501(self):
        # 默认域 = 当前真实 uid，不得出现硬编码 501
        la.install("/opt/launcher.sh", self.tmp.name, runner=self._runner)
        dom = f"gui/{os.getuid()}"
        self.assertTrue(any(dom in c for c in self.calls))
        self.assertFalse(any("gui/501" in c for c in self.calls))

    def test_injected_uid_reflected_in_argv(self):
        self.calls.clear()
        la.install("/opt/launcher.sh", self.tmp.name, runner=self._runner, uid=502)
        self.assertTrue(any("gui/502" in c for c in self.calls))
        self.assertFalse(any("gui/501" in c for c in self.calls))

    def test_install_writes_0600_plist_via_argv_runner(self):
        path = la.install("/opt/launcher.sh", self.tmp.name, runner=self._runner)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(any(c[0] == "launchctl" and "bootstrap" in c for c in self.calls))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(self._parse(path)["ProgramArguments"][-1], "--service")

    def test_uninstall_removes_plist_via_argv_runner(self):
        path = la.install("/opt/launcher.sh", self.tmp.name, runner=self._runner, uid=502)
        self.calls.clear()
        la.uninstall(self.tmp.name, runner=self._runner, uid=502)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any("bootout" in c and "gui/502" in c for c in self.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
