#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 loopback TLS/DAV 夹具：临时 openssl Ed25519 CA + 服务端 + 客户端证书。

不访问真实 NAS、不使用真实凭证、不执行挂载。仅供本仓库动态发现用例复用。
"""
import http.server
import os
import ssl
import subprocess
import tempfile
import threading


def _run(args):
    subprocess.run(args, check=True, capture_output=True)


def make_certificates(root):
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    # CA（自签）
    ca_key = os.path.join(root, "ca_private_key.pem")
    ca_cert = os.path.join(root, "ca_chain.pem")
    _run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", ca_key])
    _run(["openssl", "req", "-new", "-x509", "-key", ca_key, "-out", ca_cert,
          "-days", "1", "-subj", "/CN=fixture-ca"])
    # 服务端证书（CN=localhost，SAN DNS:localhost + IP:127.0.0.1）
    srv_key = os.path.join(root, "server_private_key.pem")
    srv_csr = os.path.join(root, "server.csr")
    srv_cert = os.path.join(root, "server_cert.pem")
    ext = os.path.join(root, "server.ext")
    with open(ext, "w", encoding="utf-8") as f:
        f.write("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    _run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", srv_key])
    _run(["openssl", "req", "-new", "-key", srv_key, "-out", srv_csr,
          "-subj", "/CN=localhost"])
    _run(["openssl", "x509", "-req", "-in", srv_csr, "-CA", ca_cert, "-CAkey", ca_key,
          "-CAcreateserial", "-out", srv_cert, "-days", "1", "-extfile", ext])
    # 客户端证书（mTLS）
    cli_key = os.path.join(root, "client_private_key.pem")
    cli_csr = os.path.join(root, "client.csr")
    cli_cert = os.path.join(root, "client_cert.pem")
    _run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", cli_key])
    _run(["openssl", "req", "-new", "-key", cli_key, "-out", cli_csr,
          "-subj", "/CN=fixture-client"])
    _run(["openssl", "x509", "-req", "-in", cli_csr, "-CA", ca_cert, "-CAkey", ca_key,
          "-CAcreateserial", "-out", cli_cert, "-days", "1"])
    os.chmod(cli_key, 0o600)
    return {
        "ca": ca_cert, "server_cert": srv_cert, "server_key": srv_key,
        "cert": cli_cert, "key": cli_key, "dir": root,
    }


class _DavHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("DAV", "1,2")
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        expected = self.server.expected_auth
        if not self.server.auth_required:
            return True
        return self.headers.get("Authorization") == expected

    def do_OPTIONS(self):
        if not self._authorized():
            self._respond(401, b"fixture authentication required")
            return
        self._respond(200, self.server.label.encode())

    do_GET = do_OPTIONS


class DavEndpoint:
    """回环 HTTPS DAV 端点；mtls=True 要求客户端证书，auth_required=True 要求 Basic。"""

    def __init__(self, certs, label="B", mtls=False, auth_required=False,
                 expected_auth=None):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certs["server_cert"], certs["server_key"])
        if mtls:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cafile=certs["ca"])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DavHandler)
        self.server.daemon_threads = True
        self.server.label = label
        self.server.mtls = mtls
        self.server.auth_required = auth_required
        self.server.expected_auth = expected_auth
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
            self._thread.join(3)
        except Exception:
            pass
