#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收夹具助手（审查者独占，非 owner 交付物）。

纪律：
- 仅绑定 127.0.0.1 临时端口；证书在临时目录用 openssl 现场生成；
- 不连接真实 NAS、不挂载、不读仓库真实凭证、不调用根目录历史探针；
- 不修改任何被审代码；只读加载 mountapp/ 下的生产模块。
"""
import base64
import http.server
import importlib.util
import os
import ssl
import subprocess
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MOUNTAPP = REPO_ROOT / "mountapp"

FIXTURE_USER = "fixture-user"
FIXTURE_PASSWORD = "fixture-password-not-a-real-credential-0000000000"
FIXTURE_AUTH = "Basic " + base64.b64encode(
    f"{FIXTURE_USER}:{FIXTURE_PASSWORD}".encode()).decode()


def load_file_module(path: Path, name: str):
    """按文件路径隔离加载模块（不污染 sys.modules 的常规名字）。"""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def production_proxy_path() -> Path:
    return MOUNTAPP / "mwdav_proxy.py"


def load_proxy_module():
    """加载生产数据面模块；返回 (module, path, is_hardened)。

    硬化判定：模块内存在 tls_transport 委托或 fail-closed 上下文构造，
    且不存在 CERT_NONE / check_hostname=False 字面量。
    """
    path = production_proxy_path()
    text = path.read_text(encoding="utf-8")
    hardened = ("CERT_NONE" not in text and "check_hostname = False" not in text
                and "check_hostname=False" not in text)
    module = load_file_module(path, "acceptance_proxy_isolated")
    return module, path, hardened


def load_state_store():
    return load_file_module(MOUNTAPP / "state_store.py", "acceptance_state_isolated")


def load_tls_transport():
    return load_file_module(MOUNTAPP / "tls_transport.py", "acceptance_tls_isolated")


def make_ca_tree(root: Path, server_cn: str = "fixture-nas.test",
                 with_client_cert: bool = True) -> dict:
    """现场生成自签 CA + 服务端证书（SAN DNS/IP）+ 可选客户端证书。"""
    root.mkdir(parents=True, exist_ok=True)
    # CA
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519",
                    "-out", str(root / "ca_key.pem")], check=True, capture_output=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-key", str(root / "ca_key.pem"),
        "-out", str(root / "ca_cert.pem"), "-days", "1",
        "-subj", "/CN=fixture-ca",
    ], check=True, capture_output=True)
    # server CSR + SAN
    ext = root / "server.ext"
    ext.write_text(
        f"subjectAltName=DNS:{server_cn},IP:127.0.0.1\n"
        "basicConstraints=CA:FALSE\n", encoding="utf-8")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519",
                    "-out", str(root / "server_key.pem")], check=True, capture_output=True)
    subprocess.run([
        "openssl", "req", "-new", "-key", str(root / "server_key.pem"),
        "-out", str(root / "server.csr"), "-subj", f"/CN={server_cn}",
    ], check=True, capture_output=True)
    subprocess.run([
        "openssl", "x509", "-req", "-in", str(root / "server.csr"),
        "-CA", str(root / "ca_cert.pem"), "-CAkey", str(root / "ca_key.pem"),
        "-CAcreateserial", "-out", str(root / "server_cert.pem"),
        "-days", "1", "-extfile", str(ext),
    ], check=True, capture_output=True)
    out = {
        "ca_cert": str(root / "ca_cert.pem"),
        "server_cert": str(root / "server_cert.pem"),
        "server_key": str(root / "server_key.pem"),
        "server_cn": server_cn,
    }
    if with_client_cert:
        subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519",
                        "-out", str(root / "client_key.pem")], check=True, capture_output=True)
        subprocess.run([
            "openssl", "req", "-new", "-key", str(root / "client_key.pem"),
            "-out", str(root / "client.csr"), "-subj", "/CN=fixture-client",
        ], check=True, capture_output=True)
        subprocess.run([
            "openssl", "x509", "-req", "-in", str(root / "client.csr"),
            "-CA", str(root / "ca_cert.pem"), "-CAkey", str(root / "ca_key.pem"),
            "-CAcreateserial", "-out", str(root / "client_cert.pem"),
            "-days", "1",
        ], check=True, capture_output=True)
        os.chmod(root / "client_key.pem", 0o600)
        out["client_cert"] = str(root / "client_cert.pem")
        out["client_key"] = str(root / "client_key.pem")
    return out


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _record(self):
        self.server.requests.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "depth": self.headers.get("Depth"),
            "destination": self.headers.get("Destination"),
            "content_length": self.headers.get("Content-Length"),
        })

    def _respond(self, status=200, body=b"ok", headers=None):
        self._record()
        # 慢存储夹具：进入后可阻塞发布/响应
        if getattr(self.server, "block_event", None) is not None:
            self.server.block_event.wait(self.server.block_timeout)
        self.send_response(status)
        self.send_header("DAV", "1,2")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        if self.server.auth_required and self.headers.get("Authorization") != FIXTURE_AUTH:
            self._respond(401, b"auth required",
                          {"WWW-Authenticate": 'Basic realm="fixture"'})
            return
        self._respond(200, self.server.label.encode())

    do_OPTIONS = do_GET
    do_HEAD = do_GET

    def do_PROPFIND(self):
        self._respond(207, self.server.propfind_body,
                      {"Content-Type": "application/xml; charset=utf-8"})

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.put_bodies.append(body)
        self._respond(201, b"created")

    def do_MOVE(self):
        # drop 模式：已接收并提交（计数+记录），但读完即断连、不回响应，
        # 用于模拟"提交成功而响应丢失"，供上游代理证明不安全方法不重放。
        if getattr(self.server, "drop_response", False):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.server.drop_count = getattr(self.server, "drop_count", 0) + 1
            self._record()
            self.connection.close()
            return
        self._respond(201, b"moved")

    do_COPY = do_MOVE
    do_DELETE = do_MOVE


class FixtureDav:
    """本地回环 DAV 夹具；可切换明文/TLS/mTLS/需认证，记录请求。"""

    def __init__(self, certs=None, label="B", mtls=False, auth_required=False,
                 propfind_body=b"<multistatus/>", drop=False):
        ctx = None
        if certs:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certs["server_cert"], certs["server_key"])
            if mtls:
                ctx.verify_mode = ssl.CERT_REQUIRED
                ctx.load_verify_locations(cafile=certs["ca_cert"])
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.server.tls_context = ctx
        self.server.label = label
        self.server.auth_required = auth_required
        self.server.requests = []
        self.server.put_bodies = []
        self.server.propfind_body = propfind_body
        self.server.drop_response = drop
        self.server.drop_count = 0
        self.server.block_event = None
        self.server.block_timeout = 5
        if ctx:
            self.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            self.server.socket = self.socket
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def set_block(self, event, timeout=5):
        self.server.block_event = event
        self.server.block_timeout = timeout

    def close(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(3)
