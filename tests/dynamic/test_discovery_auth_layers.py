#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D05/D06 + SR-3：分层探测，不折叠 mTLS/401/TLS 失败；发现层强制链+主机名校验。

本地 loopback TLS 夹具，无真实 NAS/凭证/挂载。
"""
import base64
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)
sys.path.insert(0, HERE)

import conn_manager as cm
from _https_fixture import make_certificates, DavEndpoint

USER = "fixture-user"
PASSWORD = "fixture-password-not-a-real-credential-123"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


class DiscoveryAuthLayers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="minas-discovery-")
        cls.certs = make_certificates(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.eps = []

    def tearDown(self):
        for e in self.eps:
            e.close()

    def _endpoint(self, **kw):
        e = DavEndpoint(self.certs, expected_auth=AUTH, **kw)
        self.eps.append(e)
        return e

    def _cfg(self, cert=True, server_name="localhost"):
        return cm.TlsConfig(
            ca_file=self.certs["ca"],
            cert_file=self.certs["cert"] if cert else None,
            key_file=self.certs["key"] if cert else None,
            server_name=server_name)

    # D05：mTLS 要求客户端证书。未配证书不得误判可连（TLS1.3 下拒绝无证书常表现为
    # 模糊 EOF，只能 fail-closed，不冒充 mtls_required）；已配证书时匿名探测后自动
    # 带证书升级为 confirmed。
    def test_d05_mtls_required_then_confirmed_with_client_cert(self):
        ep = self._endpoint(mtls=True, label="mtls")
        # 未配证书：阻断/不可达，绝不可连，dav_capable=False
        no_cert = cm.probe_webdav(ep.port, tls=self._cfg(cert=False), timeout=3)
        self.assertNotEqual(no_cert.status, "confirmed")
        self.assertFalse(no_cert.dav_capable)
        # 已配证书且未显式给凭证：匿名先探 → 自动带证书升级为 confirmed
        upgraded = cm.probe_webdav(ep.port, tls=self._cfg(cert=True), timeout=3)
        self.assertEqual(upgraded.status, "confirmed")
        self.assertTrue(upgraded.dav_capable)
        self.assertTrue(upgraded.client_cert_loaded)

    # D06：匿名 401 是"需认证候选"，不丢弃；带 Basic 重试 confirmed
    def test_d06_401_auth_required_not_dropped_then_confirmed(self):
        ep = self._endpoint(auth_required=True, label="auth")
        anon = cm.probe_webdav(ep.port, tls=self._cfg(), timeout=3)
        self.assertEqual(anon.status, "auth_required")
        authed = cm.probe_webdav(ep.port, username=USER, password=PASSWORD,
                                 tls=self._cfg(), timeout=3)
        self.assertEqual(authed.status, "confirmed")
        self.assertTrue(authed.dav_capable)

    # SR-3：缺 server_name → 配置错误阻断（tls_blocked），不回退弱校验
    def test_missing_server_name_is_blocked_not_trusted(self):
        ep = self._endpoint(label="x")
        cfg = cm.TlsConfig(ca_file=self.certs["ca"], server_name=None)
        outcome = cm.probe_webdav(ep.port, tls=cfg, timeout=3)
        self.assertEqual(outcome.status, "tls_blocked")
        self.assertEqual(outcome.error_type, "config")

    # SR-3：主机名不匹配 → tls_blocked（verify），绝不 CERT_NONE 放行
    def test_wrong_server_name_is_blocked(self):
        ep = self._endpoint(label="y")
        cfg = self._cfg(server_name="wrong.invalid")
        outcome = cm.probe_webdav(ep.port, tls=cfg, timeout=3)
        self.assertEqual(outcome.status, "tls_blocked")
        self.assertEqual(outcome.error_type, "verify_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
