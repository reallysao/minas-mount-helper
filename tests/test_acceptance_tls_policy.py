#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · T11/SR-3 TLS 无降级策略（审查者独占 test_acceptance_*）。

正向：正确 CA + 显式 server_name → CERT_REQUIRED + check_hostname=True，
     真实回环 TLS 握手成功；
反向：缺 server_name / 缺 CA / 客户端证书成对缺失 / CA 不可用 → TlsConfigError
     fail-closed；错主机名 / 不信任链 → 校验失败；mTLS 端点无客户端证书被拒。
全程本地临时自签证书，不触网。
"""
import os
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_helpers as H
from acceptance_helpers import make_ca_tree, FixtureDav, load_tls_transport


class TlsPolicyUnitTest(unittest.TestCase):
    def setUp(self):
        self.tls = load_tls_transport()
        self.tmp = tempfile.TemporaryDirectory()
        self.certs = make_ca_tree(Path(self.tmp.name) / "ca", "fixture-nas.test")

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self, **over):
        kw = dict(ca_file=self.certs["ca_cert"],
                  cert_file=self.certs.get("client_cert"),
                  key_file=self.certs.get("client_key"),
                  server_name="fixture-nas.test")
        kw.update(over)
        return self.tls.TlsConfig(**kw)

    # ---- 正向：合格配置强制校验且可建上下文 ----
    def test_good_context_is_required_and_hostname_checked(self):
        ctx = self.tls.build_client_context(self._cfg())
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    # ---- 反向：缺 server_name fail-closed（禁止 127.0.0.1 冒充身份）----
    def test_missing_server_name_raises_config_error(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(self._cfg(server_name=None))

    # ---- 反向：缺 CA fail-closed（不回退系统信任库静默放行）----
    def test_missing_ca_raises_config_error(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(self._cfg(ca_file=None))

    def test_unreadable_ca_raises_config_error(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(
                self._cfg(ca_file=os.path.join(self.tmp.name, "nope.pem")))

    # ---- 反向：客户端证书/私钥必须成对，缺一半 fail-closed ----
    def test_client_cert_without_key_raises(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(self._cfg(key_file=None))

    def test_key_without_client_cert_raises(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(self._cfg(cert_file=None))

    def test_bad_client_cert_pair_raises(self):
        with self.assertRaises(self.tls.TlsConfigError):
            self.tls.build_client_context(
                self._cfg(cert_file=self.certs["ca_cert"],
                          key_file=self.certs["server_key"]))

    # ---- 闭锁：失败计数达阈值锁定，成功复位，诊断不含密码 ----
    def test_lockout_locks_and_resets(self):
        lock = self.tls.TlsLockout(cooldown=30, max_failures=3)
        err = lock.record_failure(self.tls.TlsVerifyError("bad chain"))
        self.assertEqual(err.code, "verify")
        lock.record_failure(self.tls.TlsHandshakeError("eof"))
        lock.record_failure(self.tls.TlsHandshakeError("eof"))
        self.assertTrue(lock.locked())
        status = lock.status()
        self.assertEqual(status["attempts"], 3)
        self.assertNotIn(H.FIXTURE_PASSWORD, status["last_diagnostic"] or "")
        lock.record_success()
        self.assertFalse(lock.locked())
        self.assertEqual(lock.status()["attempts"], 0)

    # ---- 分类：握手/配置/校验互不混淆 ----
    def test_error_classification(self):
        self.assertIsInstance(
            self.tls.classify_error(FileNotFoundError("x")), self.tls.TlsConfigError)
        # 已分类的 TlsVerifyError 原样透传，不被降级为握手错误
        self.assertIsInstance(
            self.tls.classify_error(self.tls.TlsVerifyError("chain")),
            self.tls.TlsVerifyError)
        self.assertIsInstance(
            self.tls.classify_error(ConnectionRefusedError()),
            self.tls.TlsHandshakeError)


class TlsLoopbackHandshakeTest(unittest.TestCase):
    """真实回环 TLS：正确链+身份成功；错身份/错链/无客户端证书被拒。"""

    def setUp(self):
        self.tls = load_tls_transport()
        self.tmp = tempfile.TemporaryDirectory()
        self.certs = make_ca_tree(Path(self.tmp.name) / "ca", "fixture-nas.test")

    def tearDown(self):
        self.tmp.cleanup()

    def _connect(self, endpoint, cfg):
        # 原始 socket + wrap_socket(server_hostname=...)，避免 HTTPSConnection
        # 用 127.0.0.1 做主机名而绕过显式期望身份校验。
        ctx = self.tls.build_client_context(cfg)
        raw = socket.create_connection(("127.0.0.1", endpoint.port), timeout=3)
        try:
            ss = ctx.wrap_socket(raw, server_hostname=cfg.server_name)
            req = (f"OPTIONS /pool0/data/ HTTP/1.1\r\n"
                   f"Host: {cfg.server_name}\r\nConnection: close\r\n\r\n")
            ss.sendall(req.encode())
            chunks = []
            while True:
                chunk = ss.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            # 握手成功关闭 SSLSocket（连带关闭底层 raw）；失败未建 ss 才关 raw。
            try:
                if "ss" in locals():
                    ss.close()
                else:
                    raw.close()
            except OSError:
                pass
        status = int(data.split(b"\r\n", 1)[0].split()[1])
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        # 去掉尾部 chunked/长度残留，仅保留响应实体
        body = body.strip()
        return status, body

    def test_valid_chain_and_server_name_succeeds(self):
        ep = FixtureDav(self.certs, label="B")
        self.addCleanup(ep.close)
        cfg = self.tls.TlsConfig(
            ca_file=self.certs["ca_cert"],
            cert_file=self.certs["client_cert"],
            key_file=self.certs["client_key"],
            server_name="fixture-nas.test")
        status, body = self._connect(ep, cfg)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"B")

    def test_wrong_server_name_rejected(self):
        ep = FixtureDav(self.certs, label="B")
        self.addCleanup(ep.close)
        cfg = self.tls.TlsConfig(
            ca_file=self.certs["ca_cert"], server_name="other-device.invalid")
        with self.assertRaises(ssl.SSLError):
            self._connect(ep, cfg)

    def test_untrusted_ca_rejected(self):
        other = make_ca_tree(Path(self.tmp.name) / "ca2", "fixture-nas.test")
        ep = FixtureDav(other, label="B")
        self.addCleanup(ep.close)
        cfg = self.tls.TlsConfig(
            ca_file=self.certs["ca_cert"], server_name="fixture-nas.test")
        with self.assertRaises(ssl.SSLError):
            self._connect(ep, cfg)

    def test_mtls_endpoint_rejects_absent_client_cert(self):
        ep = FixtureDav(self.certs, label="B", mtls=True)
        self.addCleanup(ep.close)
        cfg = self.tls.TlsConfig(
            ca_file=self.certs["ca_cert"], server_name="fixture-nas.test")
        with self.assertRaises(ssl.SSLError):
            self._connect(ep, cfg)
        self.assertTrue(ep.server.requests == [])


if __name__ == "__main__":
    unittest.main()
