#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tls_transport 测试：信任链+主机名验证、fail-closed、错误分类与闭锁。

证书在临时目录用 openssl 现场生成，用完即删；无真实 NAS/凭证。
"""
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mountapp"))

from tls_transport import (
    TlsConfig, TlsError, TlsConfigError, TlsVerifyError, TlsHandshakeError,
    TlsLockout, build_client_context, classify_error,
)


def _run(args):
    subprocess.run(args, check=True, capture_output=True)


def _ec_key(key: Path):
    _run(["openssl", "genpkey", "-algorithm", "EC",
          "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(key)])


def make_ca(root: Path) -> tuple[Path, Path]:
    key, cert = root / "ca.key", root / "ca.crt"
    _ec_key(key)
    _run(["openssl", "req", "-new", "-x509", "-key", str(key), "-out", str(cert),
          "-days", "1", "-subj", "/CN=fixture-ca"])
    return key, cert


def make_leaf(root: Path, ca_key: Path, ca_cert: Path, cn: str, san: str,
              prefix: str) -> tuple[Path, Path]:
    key = root / f"{prefix}.key"
    csr = root / f"{prefix}.csr"
    cert = root / f"{prefix}.crt"
    _ec_key(key)
    _run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr),
          "-subj", f"/CN={cn}"])
    ext = root / f"{prefix}.ext"
    ext.write_text(f"subjectAltName={san}\n", encoding="utf-8")
    _run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
          "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(cert),
          "-days", "1", "-extfile", str(ext)])
    os.chmod(key, 0o600)
    return key, cert


def server_ctx(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def handshake(client_ctx: ssl.SSLContext, srv_ctx: ssl.SSLContext,
              server_hostname: str) -> tuple[list[Exception], list[Exception]]:
    """线程化 socketpair 握手，按角色分离返回 (client_errors, server_errors)。

    不使用混合列表的 errs[0]，避免线程先后造成分类取错端；双端错误都由调用方
    各自 classify_error。
    """
    a, b = socket.socketpair()
    client_errs: list[Exception] = []
    server_errs: list[Exception] = []

    def server():
        try:
            s = srv_ctx.wrap_socket(b, server_side=True)
            s.close()
        except Exception as e:
            server_errs.append(e)

    def client():
        try:
            c = client_ctx.wrap_socket(a, server_hostname=server_hostname)
            c.close()
        except Exception as e:
            client_errs.append(e)

    ts = [threading.Thread(target=server), threading.Thread(target=client)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    return client_errs, server_errs


class TlsTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ca_key, self.ca_cert = make_ca(self.root)
        # 合法服务端：SAN DNS:fixture-nas.test
        self.srv_key, self.srv_cert = make_leaf(
            self.root, self.ca_key, self.ca_cert, "fixture-nas.test",
            "DNS:fixture-nas.test,IP:127.0.0.1", "server")
        # 另一个 CA 签发的服务端（错误链）
        (self.root / "bad").mkdir(exist_ok=True)
        self.bad_ca_key, self.bad_ca_cert = make_ca(self.root / "bad")
        self.bad_key, self.bad_cert = make_leaf(
            self.root, self.bad_ca_key, self.bad_ca_cert, "fixture-nas.test",
            "DNS:fixture-nas.test,IP:127.0.0.1", "badsrv")

    def tearDown(self):
        self.tmp.cleanup()

    def _good_cfg(self, server_name="fixture-nas.test") -> TlsConfig:
        return TlsConfig(ca_file=str(self.ca_cert), server_name=server_name)

    def test_valid_chain_and_hostname_succeeds(self):
        ctx = build_client_context(self._good_cfg())
        client_errs, server_errs = handshake(
            ctx, server_ctx(self.srv_cert, self.srv_key), "fixture-nas.test")
        self.assertEqual(client_errs, [])
        self.assertEqual(server_errs, [])

    def test_wrong_ca_chain_rejected(self):
        ctx = build_client_context(self._good_cfg())
        client_errs, server_errs = handshake(
            ctx, server_ctx(self.bad_cert, self.bad_key), "fixture-nas.test")
        # 客户端本地链校验失败（SSLCertVerificationError）；服务端回 UNKNOWN_CA 告警，
        # 双端都按证书校验失败归类，不依赖 errs[0] 时序。
        self.assertTrue(client_errs)
        self.assertIsInstance(classify_error(client_errs[0]), TlsVerifyError)
        self.assertTrue(server_errs)
        self.assertIsInstance(classify_error(server_errs[0]), TlsVerifyError)

    def test_wrong_server_name_rejected(self):
        ctx = build_client_context(self._good_cfg(server_name="other.test"))
        client_errs, server_errs = handshake(
            ctx, server_ctx(self.srv_cert, self.srv_key), "other.test")
        # 客户端主机名校验失败 → verify；服务端 BAD_CERTIFICATE 告警保持 handshake，
        # 不并入 verify（与 mTLS 缺证类别一致处理）。
        self.assertTrue(client_errs)
        self.assertIsInstance(classify_error(client_errs[0]), TlsVerifyError)
        self.assertTrue(server_errs)
        self.assertIsInstance(classify_error(server_errs[0]), TlsHandshakeError)
        self.assertNotIsInstance(classify_error(server_errs[0]), TlsVerifyError)

    def test_missing_server_name_fail_closed(self):
        cfg = TlsConfig(ca_file=str(self.ca_cert), server_name=None)
        with self.assertRaises(TlsConfigError):
            build_client_context(cfg)

    def test_missing_ca_fail_closed(self):
        cfg = TlsConfig(ca_file="/no/such/ca.pem", server_name="fixture-nas.test")
        with self.assertRaises(TlsConfigError):
            build_client_context(cfg)

    def test_client_cert_pair_incomplete_fail_closed(self):
        cfg = TlsConfig(ca_file=str(self.ca_cert),
                        cert_file=str(self.srv_cert), key_file=None,
                        server_name="fixture-nas.test")
        with self.assertRaises(TlsConfigError):
            build_client_context(cfg)

    def test_mtls_missing_client_cert_rejected_by_server(self):
        # 客户端不提供证书，服务端 CERT_REQUIRED → 握手错误（fail-closed 行为可观测）。
        # 该场景保持 handshake 类别，不得被弱化成/升级为 verify。
        srv = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv.load_cert_chain(certfile=str(self.srv_cert), keyfile=str(self.srv_key))
        srv.verify_mode = ssl.CERT_REQUIRED
        srv.load_verify_locations(cafile=str(self.ca_cert))
        ctx = build_client_context(self._good_cfg())
        client_errs, server_errs = handshake(ctx, srv, "fixture-nas.test")
        self.assertTrue(server_errs)
        self.assertEqual(client_errs, [])
        classified = classify_error(server_errs[0])
        self.assertIsInstance(classified, TlsHandshakeError)
        self.assertNotIsInstance(classified, TlsVerifyError)

    def test_classify_and_lockout(self):
        lock = TlsLockout(cooldown=60, max_failures=3)
        err = lock.record_failure(TlsVerifyError("bad chain"))
        self.assertIsInstance(err, TlsVerifyError)
        self.assertFalse(lock.locked())
        lock.record_failure(TlsHandshakeError("eof"))
        lock.record_failure(TlsHandshakeError("eof"))
        self.assertTrue(lock.locked())
        st = lock.status()
        self.assertEqual(st["reason_class"], "handshake")
        lock.record_success()
        self.assertFalse(lock.locked())

    def test_classify_non_tls(self):
        self.assertIsInstance(classify_error(ConnectionResetError("x")),
                              TlsHandshakeError)


if __name__ == "__main__":
    unittest.main(verbosity=2)
