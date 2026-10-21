#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2-5：慢路径 get_pool_info 的 SNI/主机名必须用显式 server_name，
不得用 URL 里的 127.0.0.1 冒充身份（设备证书无 IP SAN 时旧写法会误失败）。

本地 mock 连接层，不发真实请求、不触网、不挂载。
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


class _FakeResp:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body


class _FakeConn:
    last = {}

    def __init__(self, host, port, ctx, server_name, timeout):
        type(self).last.update(host=host, port=port, server_name=server_name)

    def request(self, method, path, body=None, headers=None):
        type(self).last.update(method=method, path=path, headers=headers)

    def getresponse(self):
        return _FakeResp(b'{"storage":[]}')

    def close(self):
        pass


class PoolSni(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="minas-pool-")
        self.certs = make_certificates(self.tmp.name)
        self.tls = cm.TlsConfig(
            ca_file=self.certs["ca"], cert_file=self.certs["cert"],
            key_file=self.certs["key"], server_name="device.local")

    def tearDown(self):
        self.tmp.cleanup()

    def test_slow_path_uses_server_name_for_sni_not_loopback(self):
        m = cm.ConnManager(tls=self.tls)
        m.cert_files = {"ca": self.certs["ca"], "cert": self.certs["cert"],
                        "key": self.certs["key"]}
        m.state.update(username="u", password="p")
        with patch.object(cm, "_VerifiedConn", _FakeConn):
            m._probe_pool_once(None, 49999, timeout=2)
        # 连接目标仍是回环端口，但 SNI/期望身份必须是显式 server_name
        self.assertEqual(_FakeConn.last["host"], "127.0.0.1")
        self.assertEqual(_FakeConn.last["server_name"], "device.local")
        self.assertNotEqual(_FakeConn.last["server_name"], "127.0.0.1")
        self.assertEqual(_FakeConn.last["method"], "POST")
        self.assertIn("Authorization", _FakeConn.last["headers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
