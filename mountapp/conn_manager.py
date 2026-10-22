#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
连接管理器（控制面 · 发现链路）：隧道进程定位 -> 端口枚举 -> 凭证读取 -> 分层探测。

职责边界：
- 只负责发现与只读凭证读取；**不发布目标快照**（发布由 AppCore 经 state_store 唯一完成）。
- TLS fail-closed：CERT_REQUIRED + check_hostname=True，server_name 必须来自显式受信配置；
  不提供 CERT_NONE、不提供 verify=false 降级，缺配置即阻断（SR-3/T11/D05/D06）。
- 快路径（PID -> 端口 -> 凭证 -> 确认 WebDAV 端口）成功即可被上层发布；
  存储统计（get_pool_info）为独立慢路径，绝不阻塞快路径（D07/FR-1.2）。
"""
import base64
import dataclasses
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import time

# ---------- 常量 ----------
CERTS = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
POOL_INFO_PATH = "/cgi-bin/luci/filemgr/get_pool_info"
WEBDAV_BASE = "/pool0/data"
SCAN_TIMEOUT = 6

PROXY_STATE = os.path.expanduser(
    "~/Library/Application Support/小米智能存储挂载助手/proxy_state.json")
PROXY_PORT = 18445


# ======================= TLS 配置（fail-closed，无降级） =======================
# 严格上下文委托共享模块 tls_transport（CERT_REQUIRED + check_hostname=True，无 verify=false）；
# 仅在其不可用时使用等价的本地 fail-closed 兜底，规则完全一致。
try:
    import tls_transport as _tt
    TlsConfig = _tt.TlsConfig
    TlsConfigError = _tt.TlsConfigError

    def build_strict_context(cfg):
        return _tt.build_client_context(cfg)
except Exception:  # pragma: no cover - 共享模块缺失时的等价兜底
    class TlsConfigError(Exception):
        def __init__(self, diagnostic, code="config"):
            super().__init__(diagnostic)
            self.code = code
            self.diagnostic = diagnostic

    @dataclasses.dataclass(frozen=True)
    class TlsConfig:
        ca_file: str | None = None
        cert_file: str | None = None
        key_file: str | None = None
        server_name: str | None = None

    def build_strict_context(cfg):
        if not cfg.server_name:
            raise TlsConfigError("tls_server_name 未配置，无法验证服务器身份")
        if not cfg.ca_file or not os.path.exists(cfg.ca_file):
            raise TlsConfigError("受信 CA 不可用")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cafile=cfg.ca_file)
        if cfg.cert_file:
            ctx.load_cert_chain(certfile=cfg.cert_file, keyfile=cfg.key_file)
        return ctx


# 任何关闭校验的开关都被拒绝（无 verify=false 豁免）。
_DISABLE_KEYS = ("tls_verify", "MWDAV_TLS_VERIFY")


def discover_server_name(port: int, timeout: float = 4.0) -> str | None:
    """本地回环身份探测：读取隧道端口服务器证书的 subject CN。

    - 仅连接 127.0.0.1（本地 sso_login 进程），绝不连接外部主机。
    - 证书验证保持关闭仅用于“读取名字”；最终严格校验（CERT_REQUIRED +
      check_hostname）仍在 build_strict_context 进行，身份名必须与 CA 签发的
      证书匹配才算通过，不存在绕过。
    - 返回以 nas. 开头的 CN，否则 None（不匹配小米设备证书格式）。
    """
    import subprocess as _sp
    cert_file = key_file = None
    try:
        discovered = find_cert_files() or {}
        cert_file = discovered.get("cert")
        key_file = discovered.get("key")
    except Exception:
        pass
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    setattr(ctx, "check_hostname", False)
    setattr(ctx, "verify_mode", getattr(ssl, "CERT_NONE"))
    try:
        if cert_file and key_file and os.path.exists(cert_file) and os.path.exists(key_file):
            ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
    except Exception:
        pass
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except Exception:
        return None
    try:
        tls = ctx.wrap_socket(sock, server_hostname="nas.local")
        der = tls.getpeercert(binary_form=True)
        if not der:
            return None
        p = _sp.run(["openssl", "x509", "-inform", "DER", "-noout", "-subject"],
                    input=der, capture_output=True, timeout=timeout)
        text = p.stdout.decode("utf-8", "replace")
        for part in text.replace("subject=", "").split("/"):
            if part.startswith("CN=nas."):
                return part[3:].strip()
        return None
    except Exception:
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass


def load_tls_config(config: dict | None = None) -> TlsConfig:
    """从环境变量（优先）与配置字典构造严格 TLS 配置。

    - server_name 必填（tls_server_name / MWDAV_TLS_SERVER_NAME），缺失即 TlsConfigError。
    - ca_file 显式给出，否则从本地证书目录自动发现 ca_chain.pem；缺失即错误。
    - cert_file/key_file 可选（mTLS），缺省自动发现。
    - 出现任何关闭校验的键 -> TlsConfigError（配置错误，不降级）。
    """
    cfg = config or {}
    for key in _DISABLE_KEYS:
        if os.environ.get(key) is not None or cfg.get(key) is not None:
            raise TlsConfigError(
                f"检测到关闭 TLS 校验的配置项 {key}，本版本不提供降级，按配置错误拒绝")

    server_name = (os.environ.get("MWDAV_TLS_SERVER_NAME")
                   or cfg.get("tls_server_name"))
    ca_file = (os.environ.get("MWDAV_TLS_CA_FILE")
               or cfg.get("tls_ca_file"))
    cert_file = (os.environ.get("MWDAV_TLS_CERT_FILE")
                 or cfg.get("tls_cert_file"))
    key_file = (os.environ.get("MWDAV_TLS_KEY_FILE")
                or cfg.get("tls_key_file"))

    discovered = find_cert_files() or {}
    if not server_name:
        # 未配置 server_name：由发现层在探测时自动从隧道端口服务器证书获取
        # （discover_server_name），此处返回 None 标记待解析；验证层
        # build_strict_context 仍对 None fail-closed，不存在降级。
        server_name = None
    if not ca_file:
        ca_file = discovered.get("ca")
    if not ca_file or not os.path.exists(ca_file):
        raise TlsConfigError("受信 CA（ca_chain.pem）不可用，TLS 发现阻断")
    if not cert_file:
        cert_file = discovered.get("cert")
    if not key_file:
        key_file = discovered.get("key")
    if cert_file and not os.path.exists(cert_file):
        raise TlsConfigError(f"客户端证书不可用：{cert_file}")
    if key_file and not os.path.exists(key_file):
        raise TlsConfigError(f"客户端私钥不可用：{key_file}")
    return TlsConfig(ca_file=ca_file, cert_file=cert_file,
                     key_file=key_file, server_name=server_name)


class _VerifiedConn(http.client.HTTPSConnection):
    """强制以显式 server_name 做 SNI + 主机名验证（URL 仍是回环 127.0.0.1）。"""

    def __init__(self, host, port, ctx, server_name, timeout):
        super().__init__(host, port, timeout=timeout, context=ctx)
        self._expected_name = server_name

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(
            sock, server_hostname=self._expected_name)


# ======================= 分层探测结果 =======================

@dataclasses.dataclass(frozen=True)
class ProbeOutcome:
    phase: str            # connect | tls | http | dav
    status: str           # confirmed|auth_required|mtls_required|tls_blocked|not_dav|unreachable
    http_status: int | None
    dav_capable: bool
    client_cert_loaded: bool
    error_type: str | None
    diagnostic: str       # 不含密码/私钥


def _probe_once(port, cfg: TlsConfig, username, password, timeout,
                present_cert=True) -> ProbeOutcome:
    """单端口单次 OPTIONS；严格 TLS，带可选 Basic 认证与客户端证书。

    present_cert=False 时剥离本地客户端证书（匿名 TLS 探测），用于让对端先暴露
    mTLS 要求，而不是一开始就携带证书把 mtls_required 折叠掉。
    """
    eff = cfg if present_cert else dataclasses.replace(
        cfg, cert_file=None, key_file=None)
    try:
        ctx = build_strict_context(eff)
    except TlsConfigError as e:
        return ProbeOutcome("tls", "tls_blocked", None, False, False,
                            e.code, e.diagnostic)
    conn = _VerifiedConn("127.0.0.1", port, ctx, eff.server_name, timeout)
    try:
        headers = {}
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        conn.request("OPTIONS", WEBDAV_BASE + "/", headers=headers)
        resp = conn.getresponse()
        resp.read()
        dav = bool(resp.headers.get("DAV"))
        has_cert = bool(eff.cert_file)
        if resp.status in (200, 207) and dav:
            return ProbeOutcome("dav", "confirmed", resp.status, True,
                                has_cert, "ok", "")
        if resp.status in (200, 207):
            return ProbeOutcome("dav", "not_dav", resp.status, False,
                                has_cert, "missing_dav_header",
                                "OPTIONS 成功但缺少 DAV 能力头")
        if resp.status == 401:
            auth = (resp.headers.get("WWW-Authenticate") or "").lower()
            return ProbeOutcome(
                "http", "auth_required", 401, dav, has_cert,
                "basic_challenge" if "basic" in auth else "unauthorized",
                "需要 Basic 认证（候选，不丢弃）")
        return ProbeOutcome("http", "not_dav", resp.status, dav, has_cert,
                             f"http_{resp.status}", "")
    except ssl.SSLError as e:
        msg = str(e)
        has_cert = bool(eff.cert_file)
        if "PEER_DID_NOT_RETURN_A_CERTIFICATE" in msg or "CERTIFICATE_REQUIRED" in msg.upper():
            return ProbeOutcome("tls", "mtls_required", None, False, has_cert,
                                "client_cert_required", "对端要求客户端证书")
        if "CERTIFICATE_VERIFY_FAILED" in msg or "hostname" in msg.lower():
            return ProbeOutcome("tls", "tls_blocked", None, False, has_cert,
                                "verify_failed", "服务器身份链/主机名校验失败")
        return ProbeOutcome("tls", "tls_blocked", None, False, has_cert,
                            "handshake", msg[:120])
    except (socket.timeout, TimeoutError, OSError) as e:
        return ProbeOutcome("connect", "unreachable", None, False,
                            bool(eff.cert_file), type(e).__name__, str(e)[:120])
    finally:
        try:
            conn.close()
        except Exception:
            pass


def probe_webdav(port, *, username=None, password=None,
                 tls: TlsConfig | None = None, timeout=SCAN_TIMEOUT) -> ProbeOutcome:
    """分层探测：匿名先试；401 且有凭证则带 Basic 重试。

    - confirmed：200/207 + DAV。
    - auth_required：401 Basic 挑战（候选），带凭证重试成功转 confirmed。
    - mtls_required：对端要求客户端证书（条件性，NAS 策略未知）。
    - tls_blocked：配置缺失/链/主机名失败，显式阻止。
    """
    if tls is None:
        try:
            tls = load_tls_config()
        except TlsConfigError as e:
            return ProbeOutcome("tls", "tls_blocked", None, False, False,
                                e.code, e.diagnostic)
    # server_name 未配置：先从隧道端口服务器证书自动获取受信名（本地回环探测）
    if tls.server_name is None:
        cn = discover_server_name(port, timeout=timeout)
        if cn:
            tls = dataclasses.replace(tls, server_name=cn)
        else:
            return ProbeOutcome(
                "tls", "tls_blocked", None, False, False, "no_server_name",
                "未配置 tls_server_name 且无法从隧道证书自动获取服务器身份")
    # 匿名先试（不带本地客户端证书），让对端先暴露 mTLS/401，避免把要求折叠掉
    first = _probe_once(port, tls, None, None, timeout, present_cert=False)

    # D06：匿名 401 Basic 挑战 → 带凭证重试
    if first.status == "auth_required" and username and password:
        return _probe_once(
            port, tls, username, password, timeout,
            present_cert=bool(tls.cert_file))

    # D05：对端明确要求客户端证书，或握手以 EOF/中断模糊结束（TLS1.3 下服务端
    # 拒绝无证书时常表现为 SSLEOFError/EOF violation）且本地已配证书 → 自动带证书
    # 升级一次；升级后若仍 401，再带 Basic 凭证重试。
    # verify_failed（链/主机名）绝不用换证书掩盖，不进入升级。升级失败返回原始结果，
    # 不把模糊失败冒充成 mtls_required。
    ambiguous = first.error_type in ("client_cert_required", "handshake")
    can_upgrade = bool(tls.cert_file) and ambiguous and first.status in (
        "mtls_required", "tls_blocked")
    if can_upgrade:
        upgraded = _probe_once(
            port, tls, None, None, timeout, present_cert=True)
        if upgraded.status == "confirmed":
            return upgraded
        if upgraded.status == "auth_required" and username and password:
            return _probe_once(
                port, tls, username, password, timeout, present_cert=True)
        return first
    return first


# ======================= 本地环境探测（只读） =======================

def _run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def find_cert_files():
    """定位客户端证书目录与文件（受信本地路径，不学习网络身份）。"""
    dirs = []
    if os.path.isdir(CERTS):
        dirs.append(CERTS)
    base = os.path.expanduser("~/Library/Application Support")
    try:
        for d in os.listdir(base):
            p = os.path.join(base, d, "cert")
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "ca_chain.pem")):
                dirs.append(p)
    except Exception:
        pass
    for d in dirs:
        cert = key = ca = None
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for f in names:
            if f.endswith("_cert.pem"):
                cert = os.path.join(d, f)
                key = os.path.join(d, f.replace("_cert.pem", "_private_key.pem"))
            elif f == "ca_chain.pem":
                ca = os.path.join(d, f)
        if cert and key and ca and os.path.exists(key):
            return {"cert": cert, "key": key, "ca": ca, "dir": d}
    return None


def _proxy_alive():
    try:
        r = subprocess.run(
            ["lsof", "-nP", "-a", "-iTCP:%d" % PROXY_PORT, "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=4)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def read_legacy(path=PROXY_STATE) -> dict:
    """纯读兜底：从状态文件取 {username,password,port}；零写副作用。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        port = d.get("port")
        if isinstance(port, int) and 1 <= port <= 65535 and d.get("username") and d.get("password"):
            return {"username": d["username"], "password": d["password"], "port": port}
    except Exception:
        pass
    return {}


def find_sso_login_pid():
    # 按进程名精确匹配，避免 -f 子串命中本进程参数或无关 helper。
    out = _run(["pgrep", "-x", "sso_login"])
    for line in out.splitlines():
        pid = line.strip()
        if pid.isdigit():
            return int(pid)
    return None


def find_tunnel_ports(pid):
    # lsof 的多类选择条件默认按 OR 组合；必须用 -a 将目标 PID、TCP socket
    # 与 LISTEN 状态取交集，否则会把系统其他 App 的监听端口混入候选集。
    out = _run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"])
    ports = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9:
            m = re.search(r":(\d+)$", parts[8])
            if m:
                ports.add(int(m.group(1)))
    return sorted(ports)


def _parallel_first_confirmed(items, fn, max_workers=16):
    """并发探测并尽快返回首个 confirmed；不等待无关慢探针结束。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ex = ThreadPoolExecutor(max_workers=max_workers)
    futs = {ex.submit(fn, it): it for it in items}
    try:
        for fut in as_completed(futs):
            port = futs[fut]
            try:
                result = fut.result()
            except Exception:
                result = None
            if result and result.status == "confirmed":
                for other in futs:
                    if other is not fut:
                        other.cancel()
                return port, result
        return None
    finally:
        # cancel_futures 阻止尚未启动的任务；wait=False 保证首个确认不被慢输家拖住。
        ex.shutdown(wait=False, cancel_futures=True)


# ---------- LevelDB 凭证缓存（只读兜底） ----------
LEVELDB_DIR = os.path.expanduser(
    "~/Library/Application Support/小米智能存储/Partitions/session/Local Storage/leveldb")
_LEVELDB_CACHE = {"t": 0.0, "user": None, "pwd": None}


def read_webdav_from_leveldb():
    """兜底：从官方 App localStorage 读缓存 WebDAV 凭证；120 秒缓存；纯读。"""
    now = time.time()
    if now - _LEVELDB_CACHE["t"] < 120:
        return _LEVELDB_CACHE["user"], _LEVELDB_CACHE["pwd"]
    try:
        files = [os.path.join(LEVELDB_DIR, f)
                 for f in os.listdir(LEVELDB_DIR)
                 if f.endswith((".ldb", ".log"))]
        data = b""
        for f in files:
            try:
                data += open(f, "rb").read()
            except Exception:
                pass
        best = None
        for m in re.finditer(rb'[ -~]?assword":"([ -~]{30,90})', data):
            cand = m.group(1).decode("utf-8", "replace")
            cand = re.split(r'["\\]', cand)[0]
            if cand.startswith(("{", "data:", "http")):
                continue
            if "port" in cand.lower() or "fid" in cand.lower():
                continue
            specials = sum(1 for ch in cand if not ch.isalnum() and ch not in "._-/")
            if specials >= 3 and any(ch.isdigit() for ch in cand):
                if best is None or len(cand) > len(best):
                    best = cand
        user = None
        cf = find_cert_files()
        if cf:
            m = re.search(r"(\d+)_", os.path.basename(cf["cert"]))
            if m:
                user = "u" + m.group(1)
        if user and best:
            _LEVELDB_CACHE.update(t=now, user=user, pwd=best)
            return user, best
        return None, None
    except Exception:
        return None, None


def _parse_pool_info(info):
    if not info or info.get("code") != 0:
        return None
    data = info.get("data") or {}
    webdav = data.get("webDAV") or {}
    pools = data.get("internal_pool") or []
    storage = []
    for p in pools:
        try:
            storage.append({
                "name": p.get("name") or "存储空间",
                "total": int(p.get("total_size") or 0),
                "used": int(p.get("used_size") or 0),
                "pool_id": p.get("pool_id"),
            })
        except Exception:
            continue
    return {
        "username": webdav.get("username"),
        "password": webdav.get("password"),
        "webdav_port": webdav.get("port", 5000),
        "storage": storage,
    }


class ConnManager:
    """发现：PID -> 端口 -> 凭证 -> 分层探测确认 WebDAV 端口。"""

    def __init__(self, tls: TlsConfig | None = None):
        self.state = {
            "connected": False,
            "mode": "",
            "luci_port": None,
            "webdav_port": None,
            "username": "",
            "password": "",
            "storage": [],
            "uid": "",
            "error": "",
            "last_check": 0,
            "pool_check": 0,
        }
        self.tls = tls
        self.cert_files = find_cert_files()

    def snapshot(self):
        return dict(self.state)

    # ---------- 内部 ----------
    def _reset_stale(self):
        st = self.state
        st["connected"] = False
        st["mode"] = ""
        st["luci_port"] = None
        st["webdav_port"] = None

    def _fail(self, msg):
        st = self.state
        st["connected"] = False
        st["mode"] = ""
        st["luci_port"] = None
        st["webdav_port"] = None
        st["error"] = msg
        return st

    def _ensure_tls(self) -> TlsConfig | None:
        if self.tls is not None:
            return self.tls
        try:
            self.tls = load_tls_config()
            return self.tls
        except TlsConfigError as e:
            self._fail(f"TLS 配置错误：{e.diagnostic}")
            return None

    # ---------- 快路径 ----------
    def refresh(self, force=False):
        now = time.time()
        if not force and now - self.state["last_check"] < 15:
            return self.state
        self._reset_stale()
        st = self.state
        st["last_check"] = now
        st["error"] = ""

        tls = self._ensure_tls()
        if tls is None:
            return st

        pid = find_sso_login_pid()
        if not pid:
            # 兜底：本地代理存活则从只读状态文件恢复（凭证由代理维持）
            if _proxy_alive():
                legacy = read_legacy()
                if legacy:
                    st.update(connected=True, mode="p2p",
                              username=legacy["username"],
                              password=legacy["password"],
                              webdav_port=legacy["port"], error="")
                    return st
            return self._fail("未检测到「小米智能存储」App 的隧道进程（sso_login），请确认 App 正在运行")

        ports = find_tunnel_ports(pid)
        if not ports:
            return self._fail("隧道进程存在，但未发现隧道端口")

        own_port = getattr(self, "exclude_port", None)
        if own_port:
            ports = [p for p in ports if p != own_port]

        user, pwd = read_webdav_from_leveldb()
        if not (user and pwd):
            legacy = read_legacy()
            if legacy and _proxy_alive():
                user, pwd = legacy["username"], legacy["password"]
        if not (user and pwd):
            return self._fail("未获取到 WebDAV 凭证（缓存缺失），请先登录小米智能存储 App")
        st["username"], st["password"] = user, pwd

        # 并发分层探测；只接受 confirmed，不折叠 401/tls/非DAV
        found = _parallel_first_confirmed(
            ports, lambda p: probe_webdav(p, username=user, password=pwd, tls=tls))
        if found is None:
            return self._fail("未发现通过身份校验的 WebDAV 隧道端口")

        port, _outcome = found
        st["connected"] = True
        st["mode"] = "p2p"
        st["webdav_port"] = port
        return st

    # ---------- 慢路径（可选存储统计，独立调用，不阻塞快路径） ----------
    def poll_storage(self, force=False):
        """低优先级获取存储统计；失败非致命，绝不影响已发布端口。"""
        import urllib.request as _urlreq
        st = self.state
        need = (not st["storage"]) or (time.time() - st.get("pool_check", 0) > 300) or force
        if not need:
            return st
        pool = self._probe_pool_best(_urlreq)
        if pool is not None:
            parsed = _parse_pool_info(pool)
            if parsed:
                st["pool_check"] = time.time()
                if parsed["storage"]:
                    st["storage"] = parsed["storage"]
                if parsed["username"]:
                    st["username"] = parsed["username"]
                if parsed["password"]:
                    st["password"] = parsed["password"]
        return st

    def _probe_pool_best(self, _urlreq):
        """在隧道端口中找 get_pool_info；仅慢路径使用，失败返回 None。"""
        pid = find_sso_login_pid()
        if not pid:
            return None
        ports = find_tunnel_ports(pid)
        own = getattr(self, "exclude_port", None)
        if own:
            ports = [p for p in ports if p != own]
        for p in ports:
            r = self._probe_pool_once(_urlreq, p)
            if r is not None:
                return r
        return None

    def _probe_pool_once(self, _urlreq, port, timeout=5):
        # _urlreq 保留为兼容入参；慢路径也走严格 _VerifiedConn，SNI 与主机名校验
        # 一律用显式 server_name，绝不用 URL 里的 127.0.0.1 冒充身份（P2-5）。
        cf = self.cert_files
        if not cf or self.tls is None or not self.tls.server_name:
            return None
        try:
            ctx = build_strict_context(
                TlsConfig(ca_file=cf["ca"], cert_file=cf.get("cert"),
                          key_file=cf.get("key"), server_name=self.tls.server_name))
        except TlsConfigError:
            return None
        headers = {"Content-Type": "application/json"}
        user = self.state.get("username") or ""
        pwd = self.state.get("password") or ""
        if user and pwd:
            token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        try:
            conn = _VerifiedConn("127.0.0.1", port, ctx,
                                 self.tls.server_name, timeout)
            conn.request("POST", POOL_INFO_PATH, body=b"{}", headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            data = json.loads(raw.decode("utf-8", "replace"))
            return data if _parse_pool_info(data) is not None else None
        except Exception:
            return None


if __name__ == "__main__":
    cm = ConnManager()
    st = cm.refresh(force=True)
    print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
