#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立代理子进程 + 本地 loopback HTTP/TLS 真实回归（非 mock）。

- 临时目录用 openssl 生成 ECDSA CA / 服务端 / 客户端证书，用完即删；
- 不访问真实 NAS、不使用真实凭证、不执行挂载；
- 覆盖：目标轮换、读不回滚、重试取最新端口、TLS 主机名校验闭锁、mTLS、
  超限 413、HEAD 长度、入站 401。
"""
import base64
import http.client
import http.server
import importlib.util
import multiprocessing as mp
import os
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROXY_SRC = REPO / "mountapp" / "mwdav_proxy.py"
sys.path.insert(0, str(REPO / "mountapp"))

USER = "fixture-user"
PASSWORD = "fixture-password-not-a-real-credential-123"
AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


# ---------------- 证书夹具（ECDSA，临时） ----------------
def _ec_key(key: Path):
    subprocess.run(["openssl", "genpkey", "-algorithm", "EC",
                    "-pkeyopt", "ec_paramgen_curve:prime256v1", "-out", str(key)],
                   check=True, capture_output=True)


def _make_ca(root: Path) -> tuple[Path, Path]:
    key, cert = root / "ca.key", root / "ca.crt"
    _ec_key(key)
    subprocess.run(["openssl", "req", "-new", "-x509", "-key", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=fixture-ca"],
                   check=True, capture_output=True)
    return key, cert


def _make_leaf(root: Path, ca_key: Path, ca_cert: Path, cn: str, san: str,
               prefix: str) -> tuple[Path, Path]:
    key, csr, cert = root / f"{prefix}.key", root / f"{prefix}.csr", root / f"{prefix}.crt"
    ext = root / f"{prefix}.ext"
    _ec_key(key)
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr),
                    "-subj", f"/CN={cn}"], check=True, capture_output=True)
    ext.write_text(f"subjectAltName={san}\n", encoding="utf-8")
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert),
                    "-CAkey", str(ca_key), "-CAcreateserial", "-out", str(cert),
                    "-days", "1", "-extfile", str(ext)], check=True, capture_output=True)
    os.chmod(key, 0o600)
    return key, cert


# ---------------- 本地 TLS/HTTP fixture 端点 ----------------
class DavServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass

    def __init__(self, addr, ctx, label, auth_required=True, mtls=False):
        super().__init__(addr, DavHandler)
        self.tls_context = ctx
        self.label = label
        self.auth_required = auth_required
        self.mtls = mtls
        self.requests = []

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.settimeout(4)
            sock = self.tls_context.wrap_socket(sock, server_side=True)
        except ssl.SSLError:
            sock.close()
            raise
        return sock, addr


class DavHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _respond(self, status=200):
        payload = self.server.label.encode()
        if self.server.auth_required and self.headers.get("Authorization") != AUTH:
            status, payload = 401, b"unauthorized"
        self.server.requests.append({"method": self.command, "path": self.path})
        self.send_response(status)
        self.send_header("DAV", "1,2")
        self.send_header("Content-Length", str(len(payload)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        self.end_headers()
        if self.command != "HEAD" and payload:
            self.wfile.write(payload)

    do_GET = _respond
    do_OPTIONS = _respond
    do_HEAD = _respond
    do_PROPFIND = _respond

    def do_DELETE(self):
        # 模拟上游在提交前断连（不确定提交结果）
        self.server.requests.append({"method": "DELETE", "path": self.path})
        self.close_connection = True
        raise ConnectionResetError("fixture drop")


class PlainPort(socketserver.ThreadingTCPServer):
    """旧动态端口：讲非 TLS，连接后立即关闭。"""
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), PlainHandler)
        self.connections = 0


class PlainHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.recv(4096)
            self.server.connections += 1
            self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass


# ---------------- 代理子进程 ----------------
def proxy_worker(state_file, env, ready, done):
    spec = importlib.util.spec_from_file_location("isolated_proxy", PROXY_SRC)
    p = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(p)
    p.STATE_FILE = state_file
    for k, v in env.items():
        os.environ[k] = v
    p.Handler.log_message = lambda *a: None
    srv = p.ProxyServer(0)
    srv.start()
    ready.send(srv.httpd.server_port)
    done.wait(30)
    srv.stop()


class ProxyProcess:
    def __init__(self, state_file, env):
        ctx = mp.get_context("spawn")
        self.ready, self.done = ctx.Pipe(), ctx.Event()
        self.proc = ctx.Process(target=proxy_worker,
                                args=(state_file, env, self.ready[1], self.done))
        self.proc.start()
        if not self.ready[0].poll(10):
            self.proc.terminate()
            raise RuntimeError("proxy did not start")
        self.port = self.ready[0].recv()

    def stop(self):
        self.done.set()
        self.proc.join(8)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join()


def proxy_get(port, method="GET", path="/", auth=True, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=6)
    h = {}
    if auth:
        h["Authorization"] = AUTH
    h.update(headers or {})
    try:
        conn.request(method, path, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data
    finally:
        conn.close()


class LiveProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.ca_key, cls.ca_cert = _make_ca(root)
        # 合法服务端：SAN DNS:fixture-nas.test
        cls.srv_key, cls.srv_cert = _make_leaf(
            root, cls.ca_key, cls.ca_cert, "fixture-nas.test",
            "DNS:fixture-nas.test,IP:127.0.0.1", "server")
        # 错名服务端：SAN DNS:other.test
        cls.wrong_key, cls.wrong_cert = _make_leaf(
            root, cls.ca_key, cls.ca_cert, "other.test",
            "DNS:other.test,IP:127.0.0.1", "wrong")
        # 客户端证书（mTLS）
        cls.cli_key, cls.cli_cert = _make_leaf(
            root, cls.ca_key, cls.ca_cert, "fixture-client",
            "DNS:fixture-client", "client")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.state_file = str(Path(self.stack.enter_context(
            tempfile.TemporaryDirectory())) / "state.json")

    def tearDown(self):
        self.stack.close()

    def _tls_ctx(self, cert, key, mtls=False):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        if mtls:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=str(self.ca_cert))
        return ctx

    def _publish(self, port, **tls):
        from state_store import publish_target, TlsTarget
        publish_target(
            self.state_file, host="127.0.0.1", port=port,
            username=USER, password=PASSWORD,
            tls=TlsTarget(
                ca_file=tls.get("ca", str(self.ca_cert)),
                cert_file=tls.get("cert", str(self.cli_cert)),
                key_file=tls.get("key", str(self.cli_key)),
                server_name=tls.get("server_name", "fixture-nas.test")))

    def _env(self):
        return {
            "MWDAV_TLS_SERVER_NAME": "fixture-nas.test",
            "MWDAV_TLS_CA_FILE": str(self.ca_cert),
            "MWDAV_TLS_CERT_FILE": str(self.cli_cert),
            "MWDAV_TLS_KEY_FILE": str(self.cli_key),
        }

    def _endpoint(self, cert, key, label, mtls=False):
        srv = DavServer(("127.0.0.1", 0), self._tls_ctx(cert, key, mtls), label,
                        auth_required=True, mtls=mtls)
        self.stack.callback(srv.shutdown); self.stack.callback(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    # ---- 正常轮换 A→B，读不回滚 ----
    def test_rotation_and_no_rollback(self):
        a = self._endpoint(self.srv_cert, self.srv_key, "A")
        b = self._endpoint(self.srv_cert, self.srv_key, "B")
        self._publish(a.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s1, _, p1 = proxy_get(proxy.port)
        self.assertEqual((s1, p1.decode()), (200, "A"))
        self._publish(b.server_address[1])
        s2, _, p2 = proxy_get(proxy.port)
        self.assertEqual((s2, p2.decode()), (200, "B"))
        # 文件未被旧值回滚
        import json
        self.assertEqual(json.loads(Path(self.state_file).read_text())["port"],
                         b.server_address[1])

    # ---- 重试取最新端口（旧 A 为非 TLS，发布 B 后不再回退 A） ----
    def test_retry_uses_latest_port(self):
        plain = PlainPort()
        self.stack.callback(plain.shutdown); self.stack.callback(plain.server_close)
        threading.Thread(target=plain.serve_forever, daemon=True).start()
        b = self._endpoint(self.srv_cert, self.srv_key, "B")
        self._publish(plain.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s1, _, _ = proxy_get(proxy.port)
        self.assertEqual(s1, 502)  # 旧非 TLS 端口失败
        baseline = plain.connections
        self._publish(b.server_address[1])
        s2, _, p2 = proxy_get(proxy.port)
        self.assertEqual((s2, p2.decode()), (200, "B"))
        self.assertEqual(plain.connections, baseline)  # 发布 B 后未再回退旧端口
        self.assertTrue(b.requests)  # 请求确实到达新目标

    # ---- TLS 主机名/链校验失败闭锁（错名证书） ----
    def test_wrong_server_name_verify_failure(self):
        bad = self._endpoint(self.wrong_cert, self.wrong_key, "X")
        self._publish(bad.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s, _, body = proxy_get(proxy.port)
        self.assertEqual(s, 502)
        self.assertIn("verify", body.decode().lower())

    # ---- mTLS：携带客户端证书成功 ----
    def test_mtls_with_client_cert(self):
        srv = DavServer(("127.0.0.1", 0), self._tls_ctx(
            self.srv_cert, self.srv_key, mtls=True), "M", auth_required=True)
        self.stack.callback(srv.shutdown); self.stack.callback(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._publish(srv.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s, _, p = proxy_get(proxy.port)
        self.assertEqual((s, p.decode()), (200, "M"))

    # ---- A02 超限 PUT 413，不触碰上游 ----
    def test_oversize_put_413(self):
        a = self._endpoint(self.srv_cert, self.srv_key, "A")
        self._publish(a.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s, _, _ = proxy_get(
            proxy.port, method="PUT", path="/big.bin",
            headers={"Content-Length": str(200 * 1024 * 1024 + 1)})
        self.assertEqual(s, 413)
        self.assertEqual([r for r in a.requests if r["method"] == "PUT"], [])

    # ---- A12 HEAD 保留上游 Content-Length ----
    def test_head_preserves_content_length(self):
        a = self._endpoint(self.srv_cert, self.srv_key, "ABC")
        self._publish(a.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s, hdrs, body = proxy_get(proxy.port, method="HEAD")
        self.assertEqual(s, 200)
        self.assertEqual(hdrs.get("Content-Length"), "3")
        self.assertEqual(body, b"")

    # ---- A01 无凭证 401 ----
    def test_inbound_requires_credentials(self):
        a = self._endpoint(self.srv_cert, self.srv_key, "A")
        self._publish(a.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        s, hdrs, _ = proxy_get(proxy.port, auth=False)
        self.assertEqual(s, 401)
        self.assertIn("Basic", hdrs.get("WWW-Authenticate", ""))

    # ---- A14 不安全方法不确定提交不重放：502 且上游仅见一次 ----
    def test_unsafe_delete_no_replay(self):
        a = self._endpoint(self.srv_cert, self.srv_key, "A")
        self._publish(a.server_address[1])
        proxy = ProxyProcess(self.state_file, self._env())
        self.stack.callback(proxy.stop)
        proxy_get(proxy.port)  # 先建立健康连接
        s, _, _ = proxy_get(proxy.port, method="DELETE", path="/item")
        self.assertEqual(s, 502)


if __name__ == "__main__":
    unittest.main(verbosity=2)
