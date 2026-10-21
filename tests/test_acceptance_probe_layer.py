#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · 分层探测 D05(mTLS 自动升级) / D06(401 Basic 重试)（审查者独占）。

进程内调用生产 probe_webdav，上游为本机回环 TLS 夹具；不触网、不挂真实 NAS。
- D05：对端要求客户端证书时，匿名先暴露 mtls_required，本地已配证书则自动带证书
  升级一次转 confirmed；不配证书不得被折叠成 confirmed。
- D06：匿名 401 Basic 挑战，带凭证重试转 confirmed；无凭证保持 auth_required。
- verify_failed（链/主机名）绝不用换证书掩盖（由 tls_policy 套件反向覆盖）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_helpers as H
from acceptance_helpers import make_ca_tree, FixtureDav, load_file_module

CM = load_file_module(H.MOUNTAPP / "conn_manager.py", "acc_cm")


class ProbeLayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="acc-probe-")
        self.certs = make_ca_tree(Path(self.tmp.name) / "ca", "fixture-nas.test")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, with_cert=True):
        return CM.TlsConfig(
            ca_file=self.certs["ca_cert"],
            cert_file=self.certs["client_cert"] if with_cert else None,
            key_file=self.certs["client_key"] if with_cert else None,
            server_name="fixture-nas.test")

    # ---- D05：mtls_required 且本地已配证书 → 自动升级 confirmed ----
    def test_d05_mtls_auto_upgrade_with_cert(self):
        ep = FixtureDav(self.certs, label="B", mtls=True)
        self.addCleanup(ep.close)
        out = CM.probe_webdav(
            ep.port, username=H.FIXTURE_USER, password=H.FIXTURE_PASSWORD,
            tls=self._cfg(with_cert=True), timeout=5)
        self.assertEqual(out.status, "confirmed",
                         f"带证书应自动升级成功: {out.status}/{out.error_type}")
        self.assertTrue(out.dav_capable)

    # ---- D05：无证书面对 mTLS 不得折叠成 confirmed（fail-closed，两态都接受）----
    # 同一 mTLS 对端，无证书时协议投递有两种 fail-closed 形态：
    #  (a) 对端回 CERTIFICATE_REQUIRED 告警 → mtls_required/client_cert_required；
    #  (b) 对端不发告警直接断开（TLS1.3 EOF，~局部时序）→ tls_blocked/handshake。
    # 生产对模糊 EOF 主动 fail-closed，不冒充 mtls_required；本测试的硬不变量是
    # 绝不折叠成 confirmed / auth_required，而不是强求每次都拿到告警字符串。
    def test_d05_mtls_without_cert_not_collapsed(self):
        ep = FixtureDav(self.certs, label="B", mtls=True)
        self.addCleanup(ep.close)
        out = CM.probe_webdav(
            ep.port, username=H.FIXTURE_USER, password=H.FIXTURE_PASSWORD,
            tls=self._cfg(with_cert=False), timeout=5)
        self.assertNotIn(out.status, ("confirmed", "auth_required"),
                         f"无证书面对 mTLS 不得折叠为开放: {out.status}/{out.error_type}")
        self.assertIn(out.status, ("mtls_required", "tls_blocked"),
                      f"无证书 mTLS 应为 fail-closed 两态之一: {out.status}/{out.error_type}")
        if out.status == "mtls_required":
            self.assertEqual(out.error_type, "client_cert_required")
        else:
            self.assertEqual(out.error_type, "handshake")

    # ---- D06：401 Basic 挑战，带凭证重试 confirmed；无凭证 auth_required ----
    def test_d06_basic_challenge_retry(self):
        ep = FixtureDav(self.certs, label="B", auth_required=True)
        self.addCleanup(ep.close)
        # 无凭证：保持 auth_required，不冒充 confirmed
        anon = CM.probe_webdav(
            ep.port, tls=self._cfg(with_cert=False), timeout=5)
        self.assertEqual(anon.status, "auth_required", anon.status)
        # 带凭证：Basic 重试转 confirmed
        ok = CM.probe_webdav(
            ep.port, username=H.FIXTURE_USER, password=H.FIXTURE_PASSWORD,
            tls=self._cfg(with_cert=False), timeout=5)
        self.assertEqual(ok.status, "confirmed",
                         f"带凭证重试应 confirmed: {ok.status}/{ok.error_type}")


if __name__ == "__main__":
    unittest.main()
