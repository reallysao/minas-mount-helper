#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiNAS WebDAV 反向代理核心模块（性能优化版）
把「小米智能存储」P2P 隧道暴露的 NAS WebDAV(https://127.0.0.1:动态端口/pool0/data)
转发为本地稳定的 HTTP WebDAV 端点，供 Finder 原生挂载。

性能优化（v2）：
- 上游连接复用（每线程持久 HTTPS 连接，避免重复 TLS 握手）
- Finder 元数据请求快速响应（._* / .DS_Store / .Spotlight 等直接 404，不转发上游）
- PROPFIND 响应缓存（大目录短时间内重复请求直接返回缓存）
- 上游并发限制（信号量，避免 Finder 大目录同时打爆上游）
- 上游超时缩短（12s，减少 Finder 卡死时间）
- 请求体读取异常容错（避免异常 Content-Length 导致线程崩溃）
"""
import http.server
import http.client
import ssl
import threading
import time
import json
import os
import re
import base64
import socket
import sys
import urllib.parse

DEFAULT_LISTEN_PORT = 18445
DEFAULT_UPSTREAM_PORT = 49559
UPSTREAM_BASE = "/pool0/data"
CERTS = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
STATE_FILE = os.environ.get(
    "MWDAV_STATE",
    os.path.expanduser("~/Library/Application Support/小米智能存储挂载助手/proxy_state.json"))

# ---------- 性能调优参数 ----------
UPSTREAM_TIMEOUT = 12          # 上游请求超时（秒），从 30s 缩短，减少 Finder 卡死
MAX_CONCURRENT_UPSTREAM = 12   # 上游最大并发请求数，避免大目录同时打爆
PROPFIND_CACHE_TTL = 8         # PROPFIND 响应缓存时长（秒）
PROPFIND_CACHE_MAX_ENTRIES = 200  # 缓存最大条目数
MAX_BODY_SIZE = 200 * 1024 * 1024  # 最大请求体 200MB（保护内存）

# ---------- Finder 元数据请求模式（直接 404，不转发上游） ----------
# Finder 列出目录后会对每个文件请求资源叉/元数据，这些在 NAS 上不存在，
# 每次白跑一趟上游，大目录（几百个视频/图片）会累积巨大延迟。
_META_PATTERNS = [
    re.compile(r'/\._[^/]+$'),              # AppleDouble 资源叉（._文件名）
    re.compile(r'/.DS_Store$'),              # Finder 目录元数据
    re.compile(r'/.Spotlight-V100(/|$)'),   # Spotlight 索引
    re.compile(r'/.fseventsd(/|$)'),         # 文件系统事件
    re.compile(r'/.Trashes(/|$)'),           # 回收站
    re.compile(r'/.VolumeIcon.icns$'),       # 卷图标
    re.compile(r'/\.hidden$'),                # 隐藏文件列表
]


def _is_meta_request(path):
    """判断是否是 Finder 元数据请求（直接 404，不转发上游）"""
    for p in _META_PATTERNS:
        if p.search(path):
            return True
    return False


def _valid_password(pwd):
    """NAS 生成的密码不含引号/反斜杠；含这些字符视为被污染的脏数据"""
    return isinstance(pwd, str) and 20 <= len(pwd) <= 200 and '"' not in pwd and "\\" not in pwd


def _persist_state():
    """把当前上游配置写入共享状态文件，供同机器上的其他代理进程读取。
    密码为空或可疑（含引号）时跳过写入，避免覆盖掉已有有效凭证。"""
    up = UP.snapshot()
    if not up["password"] or not _valid_password(up["password"]):
        return
    try:
        d = os.path.dirname(STATE_FILE)
        if d:
            os.makedirs(d, exist_ok=True, mode=0o700)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(up, f, ensure_ascii=False)
        try:
            os.chmod(STATE_FILE, 0o600)
            os.chmod(d, 0o700)
        except Exception:
            pass
    except Exception:
        pass


def _load_shared_state():
    """读取共享状态文件（若存在），覆盖当前进程内的上游配置。
    只接受结构合法的数据；密码可疑（含引号/反斜杠）时忽略整个文件，
    避免被污染的 JSON 注入错误凭证导致全量 401。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        port = data.get("port")
        pwd = data.get("password")
        if not (isinstance(port, int) and 1 <= port <= 65535):
            return
        if not _valid_password(pwd or ""):
            return
        UP.update(port=port, username=data.get("username"),
                  password=pwd, connected=bool(data.get("connected", False)))
    except Exception:
        pass


def _check_incoming_auth(headers):
    """入站认证校验：防止本机其他用户账户/本地进程无凭证访问代理。
    只有携带与当前 NAS 凭证一致的 Basic 认证才放行（Finder/NetFS 挂载时自动携带）。"""
    up = UP.snapshot()
    if not (up["username"] and up["password"]):
        return True
    auth = headers.get("Authorization", "") or ""
    if auth.startswith("Basic "):
        try:
            dec = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            u, _, p = dec.partition(":")
            if u == up["username"] and p == up["password"]:
                return True
        except Exception:
            return False
    return False

# ---------- 上游状态（由连接管理器更新） ----------
class Upstream:
    def __init__(self):
        self.lock = threading.Lock()
        self.port = DEFAULT_UPSTREAM_PORT
        self.username = "u2855650711"
        self.password = ""
        self.host = "127.0.0.1"
        self.base = UPSTREAM_BASE
        self.connected = False
        self.last_error = ""

    def update(self, port=None, username=None, password=None, connected=None, error=""):
        with self.lock:
            if port: self.port = port
            if username: self.username = username
            if password is not None: self.password = password
            if connected is not None: self.connected = connected
            self.last_error = error
        _persist_state()
        # 端口变化时关闭所有缓存连接（连接池中的连接指向旧端口）
        _connection_pool_reset()

    def snapshot(self):
        with self.lock:
            return dict(port=self.port, username=self.username,
                        password=self.password, host=self.host, base=self.base,
                        connected=self.connected, last_error=self.last_error)

UP = Upstream()

# ---------- 上游连接池（每线程持久连接，复用 TLS 会话） ----------
_thread_local = threading.local()
_pool_lock = threading.Lock()
_pool_version = 0  # 端口变化时递增，使旧连接失效


def _connection_pool_reset():
    """端口变化时调用，标记所有缓存连接失效"""
    global _pool_version
    with _pool_lock:
        _pool_version += 1


def _make_upstream_ctx():
    """创建上游 TLS 上下文（跳过证书校验，用客户端证书双向认证）"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if os.path.exists(CERTS):
        try:
            for f in os.listdir(CERTS):
                if f.endswith("_cert.pem"):
                    cert = os.path.join(CERTS, f)
                    key = os.path.join(CERTS, f.replace("_cert.pem", "_private_key.pem"))
                    if os.path.exists(key):
                        ctx.load_cert_chain(certfile=cert, keyfile=key)
                        break
            ca = os.path.join(CERTS, "ca_chain.pem")
            if os.path.exists(ca):
                ctx.load_verify_locations(cafile=ca)
        except Exception:
            pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get_upstream_conn(host, port):
    """获取当前线程的持久上游连接。
    如果连接已失效（端口变化、连接断开），则新建连接。
    连接复用避免了每次请求重复 TLS 握手，是大目录性能提升的关键。"""
    conn = getattr(_thread_local, 'conn', None)
    ver = getattr(_thread_local, 'version', -1)
    saved_host = getattr(_thread_local, 'host', None)
    saved_port = getattr(_thread_local, 'port', None)

    # 检查连接是否仍然有效
    if (conn is not None and ver == _pool_version
            and saved_host == host and saved_port == port):
        try:
            # 用 sock 状态快速判断连接是否还活着
            if conn.sock is not None:
                # 尝试设置非阻塞读取来检测连接是否已关闭
                # 不实际发请求，避免额外开销
                return conn
        except Exception:
            pass
        # 连接可能已关闭，尝试关闭后重建
        try:
            conn.close()
        except Exception:
            pass

    # 新建连接
    ctx = _make_upstream_ctx()
    conn = http.client.HTTPSConnection(host, port, timeout=UPSTREAM_TIMEOUT, context=ctx)
    _thread_local.conn = conn
    _thread_local.version = _pool_version
    _thread_local.host = host
    _thread_local.port = port
    return conn


# ---------- 上游并发限制（信号量） ----------
_upstream_semaphore = threading.Semaphore(MAX_CONCURRENT_UPSTREAM)


def _send_upstream(method, path, headers, body):
    """转发单个请求到上游 WebDAV，返回 (status, resp_headers, body_bytes)。
    使用持久连接复用 + 并发限制 + 缩短超时。"""
    _load_shared_state()
    up = UP.snapshot()
    conn = _get_upstream_conn(up["host"], up["port"])

    # 构造请求头
    out_headers = {}
    skip = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
    for k, v in headers.items():
        if k.lower() in skip:
            continue
        out_headers[k] = v
    # 注入认证
    creds = f'{up["username"]}:{up["password"]}'
    out_headers["Authorization"] = "Basic " + base64.b64encode(creds.encode()).decode()

    # 转换 Destination 头（MOVE/COPY 请求）：把本地代理地址替换成上游地址 + base
    # Finder 发的 Destination 是 http://127.0.0.1:18445/路径/，NAS 期望 https://上游:端口/pool0/data/路径/
    # 不转换的话 NAS 返回 400，导致 Finder 提示"请尝试使用字符较少，或不含标点符号的名称"
    dest_key = None
    for k in out_headers:
        if k.lower() == "destination":
            dest_key = k
            break
    if dest_key:
        dest = out_headers[dest_key]
        try:
            parsed = urllib.parse.urlparse(dest)
            # 重新构造上游 URL：https://host:port + base + path
            upstream_dest = f"https://{up['host']}:{up['port']}{up['base']}{parsed.path}"
            if parsed.query:
                upstream_dest += "?" + parsed.query
            if parsed.fragment:
                upstream_dest += "#" + parsed.fragment
            del out_headers[dest_key]
            out_headers["Destination"] = upstream_dest
        except Exception:
            pass

    # 路径：代理根路径 -> 上游 base
    if path == "/" or path == "":
        full = up["base"] + "/"
    else:
        full = up["base"] + (path if path.startswith("/") else "/" + path)
    full = urllib.parse.quote(full, safe="/:%@!$&'()*+,;=-._~[]%")

    acquired = False
    try:
        # 并发限制：避免 Finder 大目录同时发几百个请求打爆上游
        _upstream_semaphore.acquire()
        acquired = True

        try:
            conn.request(method, full, body=body, headers=out_headers)
            resp = conn.getresponse()
            data = resp.read()
            hdrs = {k: v for k, v in resp.getheaders()}
            return resp.status, hdrs, data
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, OSError):
            # 连接失效（上游端口变化、隧道抖动），关闭旧连接，重试一次
            try:
                conn.close()
            except Exception:
                pass
            _thread_local.conn = None
            conn = _get_upstream_conn(up["host"], up["port"])
            conn.request(method, full, body=body, headers=out_headers)
            resp = conn.getresponse()
            data = resp.read()
            hdrs = {k: v for k, v in resp.getheaders()}
            return resp.status, hdrs, data
    except Exception as e:
        # 请求失败，标记连接失效（下次重建）
        try:
            conn.close()
        except Exception:
            pass
        _thread_local.conn = None
        raise
    finally:
        if acquired:
            _upstream_semaphore.release()

# ---------- PROPFIND 响应缓存 ----------
_propfind_cache = {}
_propfind_cache_lock = threading.Lock()


def _get_propfind_cache(path):
    """获取 PROPFIND 响应缓存（未过期则返回）"""
    with _propfind_cache_lock:
        entry = _propfind_cache.get(path)
        if entry and time.time() - entry[0] < PROPFIND_CACHE_TTL:
            return entry[1]
        return None


def _set_propfind_cache(path, data):
    """设置 PROPFIND 响应缓存，并清理过期条目"""
    with _propfind_cache_lock:
        _propfind_cache[path] = (time.time(), data)
        # 缓存过大时清理过期条目
        if len(_propfind_cache) > PROPFIND_CACHE_MAX_ENTRIES:
            now = time.time()
            expired = [k for k, v in _propfind_cache.items() if now - v[0] >= PROPFIND_CACHE_TTL]
            for k in expired:
                del _propfind_cache[k]


def _invalidate_propfind_cache(path):
    """修改操作后，清除相关路径的 PROPFIND 缓存（自身及父目录）"""
    with _propfind_cache_lock:
        keys = list(_propfind_cache.keys())
        # 归一化路径：去尾斜杠
        norm = path.rstrip("/")
        for k in keys:
            kn = k.rstrip("/")
            # 清除：自身、父目录（目录内容变化后父目录列表也可能变化）
            if kn == norm or norm.startswith(kn + "/") or kn.startswith(norm + "/"):
                del _propfind_cache[k]

# ---------- PROPFIND 响应净化（解决"母文件夹出现在子文件夹中"） ----------
_RESP_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?response>.*?</(?:[A-Za-z_][\w.-]*:)?response>", re.S)
_HREF_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?href>\s*([^<]+?)\s*</(?:[A-Za-z_][\w.-]*:)?href>")


def _norm_href(href):
    """href 归一化：去掉 base 前缀与转义，返回去尾斜杠的相对路径"""
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
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


def _strip_self_entry(body, req_path):
    """移除 multistatus 中「目录自身」的 response。
    macOS 的 WebDAV 客户端会把 PROPFIND 响应里的自引用条目渲染成可见子目录。"""
    target = _norm_href(req_path)
    text = body.decode("utf-8", "replace")
    blocks = list(_RESP_RE.finditer(text))
    if len(blocks) <= 1:
        return body
    kept = []
    changed = False
    for m in blocks:
        block = m.group(0)
        hm = _HREF_RE.search(block)
        if hm and _norm_href(hm.group(1)) == target:
            changed = True
            continue
        kept.append(block)
    if not changed:
        return body
    head = text[:blocks[0].start()]
    tail = text[blocks[-1].end():]
    return (head + "".join(kept) + tail).encode("utf-8")

# ---------- 本地 HTTP 服务器 ----------
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 静默日志

    def _send_simple(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        """快速发送简单响应（用于元数据 404、错误等）"""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _handle(self):
        _load_shared_state()

        # 1. 安全读取请求体（容错：异常 Content-Length / 客户端断开）
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > MAX_BODY_SIZE:
                length = 0
            body = self.rfile.read(length) if length else None
        except (OSError, ValueError, ConnectionError):
            body = None

        # 2. 入站认证
        if not _check_incoming_auth(self.headers):
            self._send_simple(401, b"Authentication required")
            self.send_header("WWW-Authenticate", 'Basic realm="MiNAS"')
            return

        # 3. Finder 元数据请求快速响应（不转发上游，直接 404）
        #    这是大目录性能提升的关键：Finder 对每个文件请求 ._文件名 等，
        #    大目录几百个文件就是几百个无效上游往返。
        if _is_meta_request(self.path):
            self._send_simple(404)
            return

        # 4. PROPFIND 缓存查询（大目录短时间内重复请求直接返回缓存）
        is_propfind = (self.command == "PROPFIND")
        cached = None
        if is_propfind:
            cached = _get_propfind_cache(self.path)

        if cached is not None:
            # 命中缓存，直接返回（已剥离自引用条目）
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(cached)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cached)
            return

        # 5. 转发到上游
        try:
            status, hdrs, data = _send_upstream(
                self.command, self.path,
                {k: v for k, v in self.headers.items()}, body
            )

            # 6. PROPFIND 列表响应：剥离自引用条目 + 写入缓存
            if is_propfind and data:
                ctype = (hdrs.get("Content-Type") or "").lower()
                if "xml" in ctype:
                    data = _strip_self_entry(data, self.path)
                    # 只缓存多条目响应（目录列表），不缓存单条目（Depth:0）
                    if data.count(b"<response") > 1:
                        _set_propfind_cache(self.path, data)

            # 7. 修改操作后使 PROPFIND 缓存失效
            if self.command in ("PUT", "DELETE", "MKCOL", "COPY", "MOVE", "PROPPATCH", "LOCK", "UNLOCK"):
                _invalidate_propfind_cache(self.path)

            # 8. 回传响应
            self.send_response(status)
            for k, v in hdrs.items():
                if k.lower() in ("content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        except Exception as e:
            UP.update(error=str(e))
            self._send_simple(502,
                ("代理无法连接 NAS WebDAV：%s\n请确认「小米智能存储」App 正在运行且设备在线。"
                 % str(e)).encode("utf-8"))

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_MKCOL = _handle
    do_COPY = _handle
    do_MOVE = _handle
    do_PROPFIND = _handle
    do_PROPPATCH = _handle
    do_LOCK = _handle
    do_UNLOCK = _handle
    do_OPTIONS = _handle


class ProxyServer:
    def __init__(self, port=DEFAULT_LISTEN_PORT):
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        # 设置 TCP keepalive 和 backlog，改善高并发下的连接处理
        http.server.ThreadingHTTPServer.request_queue_size = 64
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def is_running(self):
        return self.httpd is not None


def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_LISTEN_PORT)
    ap.add_argument("--upstream", type=int, default=DEFAULT_UPSTREAM_PORT)
    ap.add_argument("--username", default="")
    ap.add_argument("--password", default="")
    args = ap.parse_args()
    UP.update(port=args.upstream, username=args.username or None,
              password=(args.password if args.password else None))
    srv = ProxyServer(args.port)
    srv.start()
    print(f"代理已启动(v2优化版): http://127.0.0.1:{args.port}  ->  https://127.0.0.1:{args.upstream}{UPSTREAM_BASE}")
    print(f"  连接复用: 每线程持久连接 | 并发限制: {MAX_CONCURRENT_UPSTREAM} | 超时: {UPSTREAM_TIMEOUT}s | PROPFIND缓存: {PROPFIND_CACHE_TTL}s")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
