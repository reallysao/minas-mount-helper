#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiNAS WebDAV 反向代理（硬化版）

职责：把官方 P2P 隧道暴露的 NAS WebDAV 转发为本地稳定 HTTP 端点供 Finder 挂载。

硬化要点（对应 docs/specs/proxy.md）：
- 入站始终强制 Basic 认证（无匿名/降级开关）；目标未配齐返回 503，绝不匿名转发。
- 配置（target）与健康（health）分离：读路径纯读零写副作用；代理自身永不发布。
- 上游 TLS fail-closed：校验信任链 + 期望身份，CA/server_name 显式配置，错误闭锁可诊断。
- 请求体/响应体有界：超限 413/507，chunked 显式 501，禁止空体转发。
- PROPFIND 缓存键含 Depth、命名空间无关、硬上限；MOVE/COPY 双端失效。
- HEAD 原样透传 Content-Length；不安全方法不确定提交结果时不盲重放。
"""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import state_store
import tls_transport
from state_store import (  # noqa: F401
    TargetSnapshot, TlsTarget, StateFormatError,
    load_snapshot, publish_target, read_legacy,
)
from tls_transport import (  # noqa: F401
    TlsConfig, TlsError, TlsConfigError, TlsVerifyError, TlsHandshakeError,
    TlsLockout, build_client_context, classify_error,
)

# ---------- 常量 ----------
DEFAULT_LISTEN_PORT = 18445
DEFAULT_UPSTREAM_PORT = 49559
UPSTREAM_BASE = "/pool0/data"
CERTS = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
STATE_FILE = state_store.default_state_path()

UPSTREAM_TIMEOUT = 12
MAX_CONCURRENT_UPSTREAM = 12
PROPFIND_CACHE_TTL = 8
PROPFIND_CACHE_MAX_ENTRIES = 200
MAX_REQUEST_BODY = 200 * 1024 * 1024
MAX_BODY_SIZE = MAX_REQUEST_BODY  # 历史别名
MAX_RESPONSE_BODY = 64 * 1024 * 1024  # 硬限（非并发总量）；超限 507，流式背压列阶段二

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "PROPFIND"}
WRITE_METHODS = {"PUT", "DELETE", "MKCOL", "COPY", "MOVE", "PROPPATCH", "LOCK", "UNLOCK"}
HOP_BY_HOP = {"content-length", "transfer-encoding", "connection", "server", "date",
              "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "upgrade"}

# ---------- Finder 元数据请求（直接 404，不转发上游） ----------
_META_PATTERNS = [
    re.compile(r'/\._[^/]+$'),
    re.compile(r'/.DS_Store$'),
    re.compile(r'/.Spotlight-V100(/|$)'),
    re.compile(r'/.fseventsd(/|$)'),
    re.compile(r'/.Trashes(/|$)'),
    re.compile(r'/.VolumeIcon.icns$'),
    re.compile(r'/\.hidden$'),
]


def _is_meta_request(path):
    return any(p.search(path) for p in _META_PATTERNS)


class UnknownCommitError(Exception):
    """发送后无法确认上游是否已提交；不安全方法不得重放。"""


class ResponseTooLarge(Exception):
    """上游响应超过内存上限。"""


# ---------- 上游目标（target）与健康（health）分离 ----------
class Upstream:
    """内存持有者：target 可由 client 经 update() 发布，也可由读路径纯读采纳；
    connected/last_error/lockout 是健康状态，只在内存，永不落盘、永不发布。"""

    def __init__(self):
        self._lock = threading.RLock()
        # target
        self.host = "127.0.0.1"
        self.port = DEFAULT_UPSTREAM_PORT
        self.base = UPSTREAM_BASE
        self.username = ""
        self.password = ""
        self.tls = TlsTarget()
        self.generation = 0
        # health（memory only）
        self.connected = False
        self.last_error = ""
        self.lockout = TlsLockout()

    # ---- client 配置发布路径（仅 client 调用；代理读/error 路径禁止调用） ----
    def update(self, port=None, username=None, password=None, connected=None,
               error="", host=None, base=None, tls=None):
        target_given = any(v is not None for v in (port, username, password, host, base, tls))
        with self._lock:
            if target_given:
                new_host = self.host if host is None else host
                new_port = self.port if port is None else port
                new_base = self.base if base is None else base
                new_user = self.username if username is None else username
                new_pwd = self.password if password is None else password
                new_tls = self.tls if tls is None else tls
                snap = publish_target(
                    STATE_FILE, host=new_host, port=new_port,
                    username=new_user, password=new_pwd, base=new_base, tls=new_tls)
                self._adopt(snap)
            if connected is not None:
                self.connected = connected
            if error:
                self.last_error = error
            elif connected is False:
                self.last_error = ""

    def adopt(self, snap: TargetSnapshot) -> bool:
        """纯读采纳外部已发布目标，不写盘；身份变化返回 True 并 bump generation。"""
        with self._lock:
            return self._adopt(snap)

    def _adopt(self, snap: TargetSnapshot) -> bool:
        identity = (snap.host, snap.port, snap.username, snap.password,
                    snap.tls.ca_file, snap.tls.cert_file,
                    snap.tls.key_file, snap.tls.server_name)
        current = (self.host, self.port, self.username, self.password,
                   self.tls.ca_file, self.tls.cert_file,
                   self.tls.key_file, self.tls.server_name)
        if identity == current:
            return False
        self.generation += 1
        self.host, self.port, self.base = snap.host, snap.port, snap.base
        self.username, self.password = snap.username, snap.password
        self.tls = snap.tls
        return True

    def set_health(self, connected=None, error=""):
        with self._lock:
            if connected is not None:
                self.connected = connected
            if error:
                self.last_error = error
            elif connected is False:
                self.last_error = ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "host": self.host, "port": self.port, "base": self.base,
                "username": self.username, "password": self.password,
                "tls": self.tls, "generation": self.generation,
                "connected": self.connected, "last_error": self.last_error,
            }


UP = Upstream()


def _reload_snapshot() -> TargetSnapshot | None:
    """纯读共享状态；损坏返回 None（不回写、不抛给请求路径）。"""
    try:
        return load_snapshot(STATE_FILE)
    except StateFormatError:
        return None


# ---------- 上游连接（按目标身份分代复用，TLS fail-closed） ----------
_thread_local = threading.local()
_ctx_cache = {}


def _discover_client_cert() -> tuple[str | None, str | None]:
    """受信 App 包内客户端证书本地发现（仅路径兜底；server_name 绝不由此学习）。"""
    try:
        for f in os.listdir(CERTS):
            if f.endswith("_cert.pem"):
                cert = os.path.join(CERTS, f)
                key = os.path.join(CERTS, f.replace("_cert.pem", "_private_key.pem"))
                if os.path.exists(key):
                    return cert, key
    except OSError:
        pass
    return None, None


def _resolve_tls(t: TlsTarget) -> TlsConfig:
    """env(MWDAV_TLS_*) 优先；其次发布配置；再用受信本地证书目录兜底。
    server_name 只能来自 env/显式配置，缺失由 build_client_context fail-closed。"""
    env = lambda n: os.environ.get(n) or None
    ca = env("MWDAV_TLS_CA_FILE") or t.ca_file
    if ca is None or not os.path.isfile(ca):
        # 多路径兜底：标准 App 安装目录 + 用户支持目录（兼容非标准安装）
        _cand = [os.path.join(CERTS, "ca_chain.pem")]
        try:
            _base = os.path.expanduser("~/Library/Application Support")
            for _d in os.listdir(_base):
                _p = os.path.join(_base, _d, "cert", "ca_chain.pem")
                if os.path.isfile(_p):
                    _cand.append(_p)
        except Exception:
            pass
        ca = next((_p for _p in _cand if os.path.isfile(_p)), None)
    cert = env("MWDAV_TLS_CERT_FILE") or t.cert_file
    key = env("MWDAV_TLS_KEY_FILE") or t.key_file
    if not (cert and key):
        dc, dk = _discover_client_cert()
        cert = cert or dc
        key = key or dk
    return TlsConfig(
        ca_file=ca, cert_file=cert, key_file=key,
        server_name=env("MWDAV_TLS_SERVER_NAME") or t.server_name)


def _build_ctx(up: dict) -> ssl.SSLContext:
    cfg = _resolve_tls(up["tls"])
    key = (cfg.ca_file, cfg.cert_file, cfg.key_file, cfg.server_name)
    if key not in _ctx_cache:
        _ctx_cache[key] = build_client_context(cfg)  # fail closed
    return _ctx_cache[key]


def _close_tl_conn():
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _thread_local.conn = None


class _ServerNameHTTPSConnection(http.client.HTTPSConnection):
    """以显式受信 server_name 做 SNI 与主机名校验；不拿连接地址冒充身份。"""

    def __init__(self, host, port, timeout, context, server_name):
        super().__init__(host, port, timeout=timeout, context=context)
        self._server_name = server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._server_name)
        self.sock.settimeout(self.timeout)


def _obtain_conn(up: dict, force_new: bool = False) -> http.client.HTTPSConnection:
    tl = _thread_local
    if (not force_new and getattr(tl, "conn", None) is not None
            and getattr(tl, "gen", -1) == up["generation"]
            and getattr(tl, "host", None) == up["host"]
            and getattr(tl, "port", None) == up["port"]):
        return tl.conn
    _close_tl_conn()
    cfg = _resolve_tls(up["tls"])
    ctx = _build_ctx(up)
    conn = _ServerNameHTTPSConnection(
        up["host"], up["port"], UPSTREAM_TIMEOUT, ctx, cfg.server_name)
    tl.conn, tl.gen, tl.host, tl.port = conn, up["generation"], up["host"], up["port"]
    return conn


def _connection_pool_reset():
    """仅在目标身份实际变化时由读路径调用；关闭线程本地旧连接。"""
    _close_tl_conn()


# 同时限制“进入请求处理并可能读取请求体”的数量，以及实际上游并发。
# 前者必须在读取请求体前获取，防止大量 200MiB 请求先堆入内存再排队等上游槽位。
_request_semaphore = threading.Semaphore(MAX_CONCURRENT_UPSTREAM)
_upstream_semaphore = threading.Semaphore(MAX_CONCURRENT_UPSTREAM)


def _read_bounded(resp) -> bytes:
    chunks, total = [], 0
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BODY:
            raise ResponseTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


def _build_out_headers(up: dict, headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in HOP_BY_HOP or k.lower() == "authorization":
            continue
        out[k] = v
    creds = f'{up["username"]}:{up["password"]}'
    out["Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()
    return out


def _rewrite_destination(up: dict, dest: str) -> str:
    try:
        parsed = urllib.parse.urlparse(dest)
        upstream = f"https://{up['host']}:{up['port']}{up['base']}{parsed.path}"
        if parsed.query:
            upstream += "?" + parsed.query
        return upstream
    except Exception:
        return dest


def _send_once(up: dict, method: str, full: str, headers: dict, body):
    conn = _obtain_conn(up)
    conn.request(method, full, body=body, headers=headers)
    resp = conn.getresponse()
    data = _read_bounded(resp)
    return resp.status, dict(resp.getheaders()), data


def _build_upstream_path(base: str, request_target: str) -> str:
    """仅编码 URI path，保留 query 分隔符和已编码序列；fragment 不应出现在 HTTP request-target。"""
    parts = urllib.parse.urlsplit(request_target)
    raw_path = parts.path if parts.path.startswith("/") else "/" + parts.path
    full = base.rstrip("/") + raw_path
    quoted = urllib.parse.quote(full, safe="/:%@!$&'()*+,;=-._~[]%")
    return quoted + (("?" + parts.query) if parts.query else "")


def _send_upstream(method, path, headers, body):
    """转发；安全方法连接级失败可在重读最新快照后重试一次；
    不安全方法不确定提交结果直接抛 UnknownCommitError，绝不盲重放。"""
    if UP.lockout.locked():
        status = UP.lockout.status()
        raise TlsHandshakeError(
            "TLS 暂时闭锁（%.1fs 后重试）：%s" % (
                status["retry_after"], status["last_diagnostic"] or "连续握手失败"))
    up = UP.snapshot()
    full = _build_upstream_path(up["base"], path)
    out_headers = _build_out_headers(up, headers)

    dest_key = next((k for k in out_headers if k.lower() == "destination"), None)
    if dest_key:
        out_headers[dest_key] = _rewrite_destination(up, out_headers[dest_key])

    acquired = False
    try:
        _upstream_semaphore.acquire()
        acquired = True
        try:
            return _send_once(up, method, full, out_headers, body)
        except UnknownCommitError:
            raise
        except TlsError as e:
            UP.lockout.record_failure(e)
            _close_tl_conn()
            raise
        except ssl.SSLError as e:
            # wrap_socket/http.client 抛原生 SSL 异常；统一分类后再进入闭锁，
            # 防止证书校验失败落入通用网络重试而绕过安全策略。
            err = UP.lockout.record_failure(e)
            _close_tl_conn()
            raise err from e
        except ResponseTooLarge:
            _close_tl_conn()
            raise
        except Exception as e:
            _close_tl_conn()
            if method not in SAFE_METHODS:
                # 保守策略：不安全方法的任意传输失败（含发送前连接失败）都按"提交结果
                # 不确定"处理，不区分 connect/send 阶段；宁可让客户端显式复查，也不冒
                # 双写/双删风险（spec 允许发送前重试，此处选择更安全的不重放）。
                raise UnknownCommitError(
                    f"{method} 上游失败且提交结果不确定，禁止自动重放：{type(e).__name__}") from e
            # 安全方法：重读最新快照，重建身份后重试一次
            snap = _reload_snapshot()
            if snap is not None:
                UP.adopt(snap)
                up = UP.snapshot()
                full = _build_upstream_path(up["base"], path)
                out_headers = _build_out_headers(up, headers)
                dk = next((k for k in out_headers if k.lower() == "destination"), None)
                if dk:
                    out_headers[dk] = _rewrite_destination(up, out_headers[dk])
                return _send_once(up, method, full, out_headers, body)
            raise
    finally:
        if acquired:
            _upstream_semaphore.release()


# ---------- PROPFIND 缓存（键含 Depth、命名空间无关、硬上限、双端失效） ----------
_propfind_cache = {}
_propfind_lock = threading.Lock()


def _norm_path(p: str) -> str:
    parsed = urllib.parse.urlsplit(p)
    path = urllib.parse.unquote(parsed.path)
    path = re.sub(r"/+", "/", path)
    if path != "/":
        path = path.rstrip("/")
    return path or "/"


def _norm_depth(h: str | None) -> str:
    d = (h or "").strip().lower()
    if d == "0":
        return "0"
    if d == "1":
        return "1"
    return "inf"


def _propfind_cache_key(path: str, depth: str, generation: int, body: bytes | None) -> tuple:
    """缓存键：归一化路径 + Depth + 目标代次/身份 + PROPFIND 请求体摘要。
    不同属性查询、目标轮换、不同 Depth 互不串用。"""
    digest = hashlib.sha256(body or b"").hexdigest()[:16]
    return (path, depth, generation, digest)


def _cache_get(key: tuple):
    with _propfind_lock:
        entry = _propfind_cache.get(key)
        if entry and time.time() - entry[0] < PROPFIND_CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: tuple, data: bytes):
    with _propfind_lock:
        _propfind_cache[key] = (time.time(), data)
        while len(_propfind_cache) > PROPFIND_CACHE_MAX_ENTRIES:
            oldest = min(_propfind_cache, key=lambda k: _propfind_cache[k][0])
            del _propfind_cache[oldest]


def _invalidate_tree(norm: str):
    with _propfind_lock:
        for k in list(_propfind_cache):
            kn = k[0]
            if kn == norm or norm.startswith(kn + "/") or kn.startswith(norm + "/"):
                del _propfind_cache[k]


_RESP_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?response>.*?</(?:[A-Za-z_][\w.-]*:)?response>", re.S)
_HREF_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?href>\s*([^<]+?)\s*</(?:[A-Za-z_][\w.-]*:)?href>")


def _norm_href(href: str) -> str:
    href = href.strip()
    if href.startswith(("http://", "https://")):
        try:
            href = urllib.parse.urlsplit(href).path
        except Exception:
            pass
    if href.startswith(UPSTREAM_BASE):
        href = href[len(UPSTREAM_BASE):]
    try:
        href = urllib.parse.unquote(href)
    except Exception:
        pass
    return href.strip("/")


def _strip_self_entry(body: bytes, req_path: str) -> bytes:
    target = _norm_href(req_path)
    text = body.decode("utf-8", "replace")
    blocks = list(_RESP_RE.finditer(text))
    if len(blocks) <= 1:
        return body
    kept, changed = [], False
    for m in blocks:
        block = m.group(0)
        hm = _HREF_RE.search(block)
        if hm and _norm_href(hm.group(1)) == target:
            changed = True
            continue
        kept.append(block)
    if not changed:
        return body
    head, tail = text[:blocks[0].start()], text[blocks[-1].end():]
    return (head + "".join(kept) + tail).encode("utf-8")


def _count_responses(data: bytes) -> int:
    """命名空间无关地统计 DAV:response 条数；解析失败返回 -1（不缓存）。"""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return -1
    n = 0
    for el in root.iter():
        if el.tag.split("}")[-1] == "response":
            n += 1
    return n


# ---------- 本地 HTTP 服务器 ----------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        try:
            print(f"[PROXY] {self.address_string()} - {fmt % args}", flush=True)
        except Exception:
            pass

    def _send_simple(self, status, body=b"", content_type="text/plain; charset=utf-8",
                     close=False, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _drain_bounded(self, cap: int = 256 * 1024):
        """拒绝后受限排空声明体（配合 Connection: close），最多读 cap 字节，
        避免未消费体残留；读不动即放弃，不阻塞。"""
        try:
            remaining = cap
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ConnectionError):
            pass

    # ---- 入站认证（始终 require，无开关） ----
    def _authorized(self) -> bool:
        up = UP.snapshot()
        if not (up["username"] and up["password"]):
            return False
        auth = self.headers.get("Authorization", "") or ""
        if not auth.startswith("Basic "):
            return False
        try:
            dec = base64.b64decode(auth[6:]).decode("utf-8", "replace")
        except Exception:
            return False
        u, _, p = dec.partition(":")
        return hmac.compare_digest(u, up["username"]) and hmac.compare_digest(p, up["password"])

    def handle_expect_100(self):
        """在发送 100 Continue 前执行可判定门禁，避免鼓励未认证/超限客户端发送大体。"""
        up = UP.snapshot()
        if not (up["port"] and up["username"] and up["password"]):
            self._send_simple(503, "代理目标尚未配置：等待上游 WebDAV 目标发布。".encode(),
                              close=True)
            return False
        if not self._authorized():
            self._send_simple(401, b"Unauthorized",
                              extra_headers={"WWW-Authenticate": 'Basic realm="MiNAS"'},
                              close=True)
            return False
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te and self.command in WRITE_METHODS:
            self._send_simple(501, "暂不支持 chunked 上传，请使用 Content-Length。".encode(),
                              close=True)
            return False
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            self._send_simple(400, "非法 Content-Length。".encode(), close=True)
            return False
        if length > MAX_REQUEST_BODY:
            self._send_simple(413, "请求体超过上限，已拒绝且未转发上游。".encode(),
                              close=True)
            return False
        return super().handle_expect_100()

    def _handle(self):
        # 1. 纯读采纳最新目标（零写副作用，代理自身不发布）
        snap = _reload_snapshot()
        if snap is not None and UP.adopt(snap):
            _connection_pool_reset()
        up = UP.snapshot()

        # 2. 目标未配齐 → 503。未配置时不存在可用于比较的入站凭证，
        # 必须先报告配置未就绪，而不是把所有请求误报成认证失败。
        if not (up["port"] and up["username"] and up["password"]):
            self._send_simple(503, "代理目标尚未配置：等待上游 WebDAV 目标发布。".encode(),
                              close=True)
            return

        # 3. 入站认证：缺失/不匹配 → 401 挑战
        if not self._authorized():
            self._send_simple(
                401, b"Unauthorized",
                extra_headers={"WWW-Authenticate": 'Basic realm="MiNAS"'},
                close=True)
            # close=True 后直接返回，不按字节界排空声明体（无时间界/不按
            # Content-Length 收尾会在客户端不 EOF 时 stall）；沿 Expect/401 模式。
            return

        # 4. Finder 元数据快速 404
        if _is_meta_request(self.path):
            self._send_simple(404)
            return

        # 5. 请求体策略：chunked 显式拒绝；超限 413；短读中止，禁止空体转发
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te and self.command in WRITE_METHODS:
            self._send_simple(501, "暂不支持 chunked 上传，请使用 Content-Length。".encode(),
                              close=True)
            self._drain_bounded()
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            self._send_simple(400, "非法 Content-Length。".encode(), close=True)
            return
        if length > MAX_REQUEST_BODY:
            self._send_simple(
                413, "请求体超过上限，已拒绝且未转发上游。".encode(), close=True)
            self._drain_bounded()
            return
        body = None
        if length:
            try:
                body = self.rfile.read(length)
            except (OSError, ConnectionError):
                body = None
            if not body or len(body) != length:
                self._send_simple(400, "请求体不完整，未转发上游。".encode(), close=True)
                return

        # 6. PROPFIND 缓存（键 = 归一化路径 + Depth + 目标代次 + 请求体摘要）
        is_propfind = self.command == "PROPFIND"
        cache_key = None
        cached = None
        if is_propfind:
            cache_key = _propfind_cache_key(
                _norm_path(self.path), _norm_depth(self.headers.get("Depth")),
                up["generation"], body)
            cached = _cache_get(cache_key)
        if cached is not None:
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(cached)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cached)
            return

        # 7. 转发
        try:
            status, hdrs, data = _send_upstream(
                self.command, self.path,
                {k: v for k, v in self.headers.items()}, body)
        except UnknownCommitError as e:
            UP.set_health(connected=False, error=str(e))
            self._send_simple(502, f"上游提交结果不确定，未自动重发：{e}".encode(), close=True)
            return
        except TlsError as e:
            UP.set_health(connected=False, error=e.diagnostic)
            self._send_simple(
                502, f"TLS 上游不可用（{e.code}）：{e.diagnostic}".encode(), close=True)
            return
        except ResponseTooLarge:
            UP.set_health(connected=False, error="响应超过内存上限")
            self._send_simple(507, "上游响应超过内存上限，已中止。".encode(), close=True)
            return
        except StateFormatError as e:
            UP.set_health(connected=False, error=str(e))
            self._send_simple(503, f"共享状态损坏：{e}".encode(), close=True)
            return
        except Exception as e:
            UP.set_health(connected=False, error=f"{type(e).__name__}: {e}")
            self._send_simple(
                502, ("代理无法连接 NAS WebDAV：%s\n请确认「小米智能存储」App 正在运行且设备在线。"
                      % e).encode(), close=True)
            return

        UP.lockout.record_success()
        UP.set_health(connected=True)

        # 8. PROPFIND 列表：命名空间无关校验 + 剥离自引用 + 写缓存
        if is_propfind and data and status == 207 and cache_key is not None:
            ctype = (hdrs.get("Content-Type") or "").lower()
            if "xml" in ctype and _count_responses(data) >= 2:
                data = _strip_self_entry(data, self.path)
                _cache_set(cache_key, data)

        # 9. 修改操作失效缓存（MOVE/COPY 同时失效源与目标两端）
        if self.command in ("DELETE", "MKCOL", "COPY", "MOVE", "PROPPATCH",
                            "LOCK", "UNLOCK", "PUT"):
            _invalidate_tree(_norm_path(self.path))
            if self.command in ("MOVE", "COPY"):
                dest = next((v for k, v in self.headers.items()
                             if k.lower() == "destination"), None)
                if dest:
                    try:
                        dp = urllib.parse.urlparse(dest).path
                    except Exception:
                        dp = dest
                    _invalidate_tree(_norm_path(dp))

        # 10. 回传响应（HEAD 原样透传上游头，含 Content-Length，不写体）
        self.send_response(status)
        for k, v in hdrs.items():
            kl = k.lower()
            if kl in HOP_BY_HOP:
                # HEAD 保留上游 Content-Length；其余方法丢弃后由代理重算
                if self.command != "HEAD" or kl != "content-length":
                    continue
            self.send_header(k, v)
        if self.command == "OPTIONS":
            self.send_header("WWW-Authenticate", 'Basic realm="MiNAS"')
        if self.command != "HEAD":
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD" and data:
            self.wfile.write(data)

    def _handle_guarded(self):
        _request_semaphore.acquire()
        try:
            return self._handle()
        finally:
            _request_semaphore.release()

    do_GET = _handle_guarded
    do_HEAD = _handle_guarded
    do_POST = _handle_guarded
    do_PUT = _handle_guarded
    do_DELETE = _handle_guarded
    do_MKCOL = _handle_guarded
    do_COPY = _handle_guarded
    do_MOVE = _handle_guarded
    do_PROPFIND = _handle_guarded
    do_PROPPATCH = _handle_guarded
    do_LOCK = _handle_guarded
    do_UNLOCK = _handle_guarded
    do_OPTIONS = _handle_guarded


class ProxyServer:
    """可嵌入常驻 service 的本地 HTTP 代理服务（线程模型明确、可重复停止）。

    - start()：绑定（0=临时端口）→ 起 serve_forever 线程；端口占用抛 OSError
      （EADDRINUSE），不留半成品；返回实际监听端口。
    - stop(timeout)：shutdown → server_close → join 线程，可重复调用，清理套接字。
    - 自身不做发现/自愈、不发布目标：由嵌入的 service 持续读快照并 adopt。
    """

    def __init__(self, port: int = DEFAULT_LISTEN_PORT, enable_https: bool = False):
        self.port = port
        self._enable_https = enable_https
        self.httpd = None
        self.httpsd = None
        self.thread = None
        self.https_thread = None

    @property
    def listen_port(self) -> int | None:
        return self.httpd.server_port if self.httpd else None

    def start(self) -> int:
        http.server.ThreadingHTTPServer.request_queue_size = 64
        # 先清理前次实例，防止重复 start 泄漏套接字/线程。
        self.stop()
        try:
            self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError:
            self.httpd = None
            raise
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True, name="mwdav-http")
        self.thread.start()

        if self._enable_https:
            cert_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "localhost.crt")
            key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "localhost.key")
            if os.path.exists(cert_file) and os.path.exists(key_file):
                try:
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
                    self.httpsd = http.server.ThreadingHTTPServer(
                        ("127.0.0.1", self.port + 1), Handler)
                    self.httpsd.socket = ctx.wrap_socket(
                        self.httpsd.socket, server_side=True)
                    self.https_thread = threading.Thread(
                        target=self.httpsd.serve_forever, daemon=True,
                        name="mwdav-https")
                    self.https_thread.start()
                except Exception as e:
                    # HTTPS 旁路失败不阻断 HTTP 主面；记录并降级为仅 HTTP。
                    self.httpsd = None
                    self.https_thread = None
                    print(f"HTTPS 启用失败: {e}")
        return self.listen_port

    def stop(self, timeout: float = 5.0) -> None:
        for srv, thr in ((self.httpd, self.thread),
                         (self.httpsd, self.https_thread)):
            if srv is not None:
                try:
                    srv.shutdown()
                except Exception:
                    pass
                try:
                    srv.server_close()
                except Exception:
                    pass
            if thr is not None:
                thr.join(timeout)
        self.httpd = None
        self.httpsd = None
        self.thread = None
        self.https_thread = None

    def is_running(self) -> bool:
        return self.httpd is not None and self.thread is not None \
            and self.thread.is_alive()


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    ap.add_argument("--upstream", type=int, default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--base", default=None)
    ap.add_argument("--ca", default=None)
    ap.add_argument("--cert", default=None)
    ap.add_argument("--key", default=None)
    ap.add_argument("--server-name", default=None)
    ap.add_argument("--max-request-bytes", type=int, default=MAX_REQUEST_BODY)
    ap.add_argument("--max-response-bytes", type=int, default=MAX_RESPONSE_BODY)
    args = ap.parse_args()

    MAX_REQUEST_BODY = args.max_request_bytes
    MAX_RESPONSE_BODY = args.max_response_bytes
    snap = load_snapshot(STATE_FILE)
    if snap is None:
        ap.error("目标状态尚未发布；独立代理必须从受保护状态文件读取凭证")
    UP.adopt(snap)
    # CLI 只有显式传入时才覆盖非敏感位置/TLS；凭证只来自 0600 状态文件。
    if any(v is not None for v in (args.host, args.upstream, args.base,
                                    args.ca, args.cert, args.key, args.server_name)):
        tls = TlsTarget(
            ca_file=args.ca or snap.tls.ca_file,
            cert_file=args.cert or snap.tls.cert_file,
            key_file=args.key or snap.tls.key_file,
            server_name=args.server_name or snap.tls.server_name)
        UP.adopt(TargetSnapshot(
            generation=snap.generation, published_at=snap.published_at,
            host=args.host or snap.host,
            port=args.upstream if args.upstream is not None else snap.port,
            base=args.base or snap.base,
            username=snap.username, password=snap.password, tls=tls))
    effective = UP.snapshot()
    srv = ProxyServer(args.port)
    srv.start()
    print(f"代理已启动(硬化版): http://127.0.0.1:{args.port} -> https://{effective['host']}:{effective['port']}{effective['base']}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
