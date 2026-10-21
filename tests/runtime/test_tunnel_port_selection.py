#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D08：lsof 端口选择必须限定为目标 sso_login PID 的 TCP LISTEN 交集。"""
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)

import conn_manager as cm
import app_core as ac


class TunnelPortSelection(unittest.TestCase):
    def test_lsof_selection_ands_pid_tcp_and_listen(self):
        """缺少 -a 时 lsof 会 OR 选择条件，混入系统所有监听端口。"""
        captured = []

        def fake_run(cmd, timeout=5):
            captured.append(cmd)
            return "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"

        with patch.object(cm, "_run", side_effect=fake_run):
            self.assertEqual(cm.find_tunnel_ports(4242), [])

        self.assertEqual(len(captured), 1)
        cmd = captured[0]
        self.assertEqual(cmd[0], "lsof")
        self.assertIn("-a", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "4242")
        self.assertIn("-iTCP", cmd)
        self.assertIn("-sTCP:LISTEN", cmd)
        # -a 必须出现在选择条件前，冻结命令契约并便于人工审查。
        self.assertLess(cmd.index("-a"), cmd.index("-p"))
        self.assertLess(cmd.index("-a"), cmd.index("-iTCP"))
        self.assertLess(cmd.index("-a"), cmd.index("-sTCP:LISTEN"))

    def test_proxy_port_lsof_selection_is_anded(self):
        """相邻同类调用也须冻结为 AND，防止回归为系统任意监听进程。"""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return type("Result", (), {"stdout": ""})()

        with patch.object(ac.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(ac.AppCore._proxy_pid_on(18445))
        self.assertEqual(captured["cmd"], [
            "lsof", "-nP", "-a", "-iTCP:18445", "-sTCP:LISTEN"])

        captured.clear()
        with patch.object(cm.subprocess, "run", side_effect=fake_run):
            self.assertFalse(cm._proxy_alive())
        self.assertEqual(captured["cmd"], [
            "lsof", "-nP", "-a", "-iTCP:18445", "-sTCP:LISTEN"])

    def test_parser_accepts_only_output_from_filtered_command(self):
        """解析 lsof 的 IPv4/IPv6 监听地址，并去重排序。"""
        fixture = """COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME
sso_login 4242 me 10u IPv4 0x1 0t0 TCP 127.0.0.1:25445 (LISTEN)
sso_login 4242 me 11u IPv6 0x2 0t0 TCP [::1]:56099 (LISTEN)
sso_login 4242 me 12u IPv4 0x3 0t0 TCP *:56107 (LISTEN)
sso_login 4242 me 13u IPv4 0x4 0t0 TCP 127.0.0.1:56099 (LISTEN)
"""
        with patch.object(cm, "_run", return_value=fixture):
            self.assertEqual(cm.find_tunnel_ports(4242), [25445, 56099, 56107])


if __name__ == "__main__":
    unittest.main(verbosity=2)
