#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T09：无端口/失败分支必须清空陈旧 connected 与旧 webdav_port，不得保留在线态。

本地 fixture 证书 + mock 进程发现，无真实 NAS/挂载。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "..", "mountapp"))
DYN = os.path.abspath(os.path.join(HERE, "..", "dynamic"))
sys.path.insert(0, MOUNTAPP)
sys.path.insert(0, DYN)

import conn_manager as cm
from _https_fixture import make_certificates


class StateReset(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="minas-reset-")
        self.certs = make_certificates(self.tmp.name)
        self.tls = cm.TlsConfig(
            ca_file=self.certs["ca"], cert_file=self.certs["cert"],
            key_file=self.certs["key"], server_name="localhost")

    def tearDown(self):
        self.tmp.cleanup()

    def _manager(self):
        m = cm.ConnManager(tls=self.tls)
        # 预置陈旧在线态，验证失败分支会清空
        m.state.update(connected=True, webdav_port=12345, mode="p2p",
                       username="u", password="p")
        return m

    def test_no_pid_clears_stale_connected(self):
        m = self._manager()
        with patch.object(cm, "find_sso_login_pid", return_value=None), \
             patch.object(cm, "_proxy_alive", return_value=False):
            st = m.refresh(force=True)
        self.assertFalse(st["connected"])
        self.assertIsNone(st["webdav_port"])
        self.assertTrue(st["error"])

    def test_pid_but_no_ports_clears_stale_connected(self):
        m = self._manager()
        with patch.object(cm, "find_sso_login_pid", return_value=123), \
             patch.object(cm, "find_tunnel_ports", return_value=[]):
            st = m.refresh(force=True)
        self.assertFalse(st["connected"])
        self.assertIsNone(st["webdav_port"])
        self.assertTrue(st["error"])

    def test_no_confirmed_probe_clears_stale_connected(self):
        m = self._manager()
        with patch.object(cm, "find_sso_login_pid", return_value=123), \
             patch.object(cm, "find_tunnel_ports", return_value=[59999]), \
             patch.object(cm, "read_webdav_from_leveldb",
                          return_value=("u", "p")), \
             patch.object(cm, "read_legacy", return_value={}):
            st = m.refresh(force=True)
        self.assertFalse(st["connected"])
        self.assertIsNone(st["webdav_port"])
        self.assertTrue(st["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
