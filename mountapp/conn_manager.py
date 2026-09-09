#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
连接管理器：发现「小米智能存储」P2P 隧道、获取 WebDAV 凭证与存储信息

原理：
- 官方 App 的 MiNasClient（sso_login 原生助手）维护 P2P 隧道，
  在本机暴露一组 nginx 端口：
    * luci 网关端口  -> POST /cgi-bin/luci/filemgr/get_pool_info 成功
    * WebDAV 端口    -> OPTIONS /pool0/data/ 返回 DAV: 1,2
- 通过 luci 网关可随时获取最新的 WebDAV 用户名/密码与存储统计。
"""
import os
import re
import ssl
import json
import time
import base64
import socket
import subprocess
import urllib.request
import urllib.error

# ---------- 常量 ----------
CERTS = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
POOL_INFO_PATH = "/cgi-bin/luci/filemgr/get_pool_info"
WEBDAV_BASE = "/pool0/data"
SCAN_TIMEOUT = 6
LOCK = object()


def _run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def find_cert_files():
    """定位客户端证书目录与文件"""
    dirs = []
    if os.path.isdir(CERTS):
        dirs.append(CERTS)
    # 兜底：在用户目录下搜索
    for root in (os.path.expanduser("~/Library/Application Support"),):
        try:
            for d in os.listdir(root):
                p = os.path.join(root, d, "cert")
                if os.path.isdir(p) and os.path.exists(os.path.join(p, "ca_chain.pem")):
                    dirs.append(p)
        except Exception:
            pass
    for d in dirs:
        cert = key = ca = None
        for f in os.listdir(d):
            if f.endswith("_cert.pem"):
                cert = os.path.join(d, f)
                key = os.path.join(d, f.replace("_cert.pem", "_private_key.pem"))
            elif f == "ca_chain.pem":
                ca = os.path.join(d, f)
        if cert and key and ca and os.path.exists(key):
            return {"cert": cert, "key": key, "ca": ca, "dir": d}
    return None


def _make_ctx(cert_files):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        if cert_files:
            ctx.load_cert_chain(certfile=cert_files["cert"], keyfile=cert_files["key"])
            try:
                ctx.load_verify_locations(cafile=cert_files["ca"])
            except Exception:
                pass
    except Exception:
        pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def find_sso_login_pid():
    """找到官方 App 的原生隧道进程"""
    out = _run(["pgrep", "-f", "sso_login"])
    for line in out.splitlines():
        pid = line.strip()
        if pid.isdigit():
            return int(pid)
    return None


def find_tunnel_ports(pid):
    """扫描隧道进程监听的 TCP 端口"""
    out = _run(["lsof", "-nP", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"])
    ports = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9:
            m = re.search(r":(\d+)$", parts[8])
            if m:
                ports.add(int(m.group(1)))
    return sorted(ports)


def probe_luci(port, cert_files, timeout=SCAN_TIMEOUT):
    """POST get_pool_info，成功则返回解析结果"""
    ctx = _make_ctx(cert_files)
    req = urllib.request.Request(
        f"https://127.0.0.1:{port}{POOL_INFO_PATH}",
        data=b"{}", method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None


def probe_webdav(port, timeout=SCAN_TIMEOUT):
    """OPTIONS /pool0/data/，返回是否 DAV 服务器"""
    ctx = _make_ctx(None)
    req = urllib.request.Request(f"https://127.0.0.1:{port}{WEBDAV_BASE}/",
                                 method="OPTIONS")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
            dav = resp.headers.get("DAV")
            return bool(dav)
    except Exception:
        return False


def _parallel_first(items, fn, max_workers=16):
    """并发探测，返回第一个成功结果（fn 返回真值即成功）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(fn, it) for it in items]
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                return r
    return None


_ED25519_PY = None  # 模块级缓存：已确认支持 Ed25519 的解释器


def _find_ed25519_python():
    """查找支持 Ed25519 客户端证书的解释器（优先 Doubao 运行时，其次当前解释器）"""
    global _ED25519_PY
    if _ED25519_PY:
        return _ED25519_PY
    import glob as _glob
    cands = []
    try:
        cands += _glob.glob(os.path.expanduser(
            "~/Library/Application Support/Doubao/sandbox_runtime/bases/*/bin/python3"))
    except Exception:
        pass
    for py in cands:
        if not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", (
                "import ssl;c=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);"
                "c.load_cert_chain(r'%s',r'%s');print('OK')"
            ) % (CERTS + "/2855650711_1191211642_cert.pem",
                 CERTS + "/2855650711_1191211642_private_key.pem")],
                capture_output=True, timeout=8)
            if b"OK" in r.stdout:
                _ED25519_PY = py
                return py
        except Exception:
            continue
    return None


def probe_pool_info_via_helper(port, timeout=10):
    """用支持 Ed25519 的解释器（子进程）调 get_pool_info，返回解析结果或 None"""
    py = _find_ed25519_python()
    if not py:
        return None
    script = (
        "import ssl,urllib.request,json,sys\n"
        "c=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)\n"
        "c.load_cert_chain(r'%s',r'%s')\n"
        "c.load_verify_locations(cafile=r'%s')\n"
        "c.check_hostname=False;c.verify_mode=ssl.CERT_NONE\n"
        "r=urllib.request.Request('https://127.0.0.1:%d/cgi-bin/luci/filemgr/get_pool_info',"
        "data=b'{}',method='POST',headers={'Content-Type':'application/json'})\n"
        "try:\n"
        " d=urllib.request.urlopen(r,context=c,timeout=%d).read()\n"
        " print(d.decode('utf-8','replace'))\n"
        "except Exception as e:\n"
        " sys.stderr.write(str(e))\n"
    ) % (CERTS + "/2855650711_1191211642_cert.pem",
         CERTS + "/2855650711_1191211642_private_key.pem",
         CERTS + "/ca_chain.pem", port, timeout)
    try:
        r = subprocess.run([py, "-c", script], capture_output=True, timeout=timeout + 5)
        out = r.stdout.decode("utf-8", "replace").strip()
        if out:
            return json.loads(out)
    except Exception:
        return None
    return None


def _parse_pool_info(info):
    """从 get_pool_info 结果提取凭证与存储统计"""
    if not info or info.get("code") != 0:
        return None
    data = info.get("data") or {}
    webdav = data.get("webDAV") or {}
    pools = data.get("internal_pool") or []
    storage = []
    for p in pools:
        try:
            total = int(p.get("total_size") or 0)
            used = int(p.get("used_size") or 0)
            storage.append({
                "name": p.get("name") or "存储空间",
                "total": total,
                "used": used,
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


LEVELDB_DIR = os.path.expanduser(
    "~/Library/Application Support/小米智能存储/Partitions/session/Local Storage/leveldb")


def read_webdav_from_leveldb():
    """兜底：从官方 App 的 localStorage(LevelDB) 读取缓存的 WebDAV 密码。
    官方 App 会把 {username, password, port, uri} 写进 localStorage，
    密码字段在 Snappy 压缩块中可能被打散，但密码本体保持连续可打印。
    返回 (username, password) 或 (None, None)。"""
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
        # 密码：形如 <x>assword":"<40-90位可打印串> （x 可能被噪声替换）
        # 注意：结尾引号可能被 Snappy 压缩块吃掉，导致匹配越过闭合引号，
        # 吞进后续 JSON（如 "port_2":58313）。故在首个双引号/反斜杠处截断，
        # 并排除含 JSON 字段特征的候选。
        best = None
        for m in re.finditer(rb'[ -~]?assword":"([ -~]{30,90})', data):
            cand = m.group(1).decode("utf-8", "replace")
            # 密码本体遇到引号/反斜杠即截断（NAS 生成的密码不含引号）
            cand = re.split(r'["\\]', cand)[0]
            # 过滤明显不是密码的长串（JSON/base64 内容）
            if cand.startswith(("{", "data:", "http")):
                continue
            if "port" in cand.lower() or "fid" in cand.lower():
                continue
            specials = sum(1 for ch in cand if not ch.isalnum() and ch not in "._-/")
            if specials >= 3 and any(ch.isdigit() for ch in cand):
                if best is None or len(cand) > len(best):
                    best = cand
        # 用户名：u + uid（uid 从证书文件名取）
        user = None
        cf = find_cert_files()
        if cf:
            m = re.search(r"(\d+)_", os.path.basename(cf["cert"]))
            if m:
                user = "u" + m.group(1)
        if user and best:
            return user, best
        return None, None
    except Exception:
        return None, None


class ConnManager:
    """连接管理：隧道发现 -> 端口识别 -> 凭证获取，并持续刷新"""

    def __init__(self):
        self.state = {
            "connected": False,        # 隧道可用
            "mode": "",                # "p2p" / "lan" / ""
            "luci_port": None,         # luci 网关端口
            "webdav_port": None,       # NAS WebDAV 隧道端口
            "username": "",
            "password": "",
            "storage": [],             # 存储统计
            "uid": "",
            "error": "",
            "last_check": 0,
        }
        self.cert_files = find_cert_files()
        # 测试当前解释器能否加载 Ed25519 客户端证书（系统 Python 3.9 不支持）
        self.cert_ok = False
        cf = self.cert_files
        if cf:
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.load_cert_chain(certfile=cf["cert"], keyfile=cf["key"])
                self.cert_ok = True
            except Exception:
                self.cert_ok = False

    def snapshot(self):
        return dict(self.state)

    def refresh(self, force=False):
        """执行一轮完整探测；间隔小于 15 秒且非强制则复用缓存。

        快路径（每次刷新都走）：隧道 PID -> 端口 -> LevelDB 凭证 -> WebDAV 端口
        慢路径（存储统计，仅在缓存缺失/过期时）：get_pool_info（Ed25519 子进程）
        """
        now = time.time()
        if not force and now - self.state["last_check"] < 15:
            return self.state
        st = self.state
        st["last_check"] = now
        st["error"] = ""

        # ---- 快路径 ----
        pid = find_sso_login_pid()
        if not pid:
            st["connected"] = False
            st["mode"] = ""
            st["luci_port"] = st["webdav_port"] = None
            st["error"] = "未检测到「小米智能存储」App 的隧道进程（sso_login），请确认 App 正在运行"
            return st

        ports = find_tunnel_ports(pid)
        if not ports:
            st["error"] = "隧道进程存在，但未发现隧道端口"
            return st
        own_port = getattr(self, "exclude_port", None)
        if own_port:
            ports = [p for p in ports if p != own_port]

        # 凭证：官方 App 缓存（秒级、稳定）
        user, pwd = read_webdav_from_leveldb()
        if user and pwd:
            st["username"] = user
            st["password"] = pwd
        else:
            st["connected"] = False
            st["error"] = "未获取到 WebDAV 凭证（缓存缺失），请先登录小米智能存储 App"
            return st

        # WebDAV 端口：优先缓存，其次并行扫描
        wd_port = st.get("webdav_port")
        if not (wd_port and wd_port in ports and probe_webdav(wd_port, timeout=2)):
            wd_port = _parallel_first(ports, lambda p: p if probe_webdav(p, timeout=2) else None)
        if not wd_port:
            st["connected"] = False
            st["error"] = "未发现 WebDAV 隧道端口"
            return st
        st["connected"] = True
        st["mode"] = "p2p"
        st["webdav_port"] = wd_port

        # ---- 慢路径：存储统计（可选增强，失败不影响挂载）----
        need_pool = (not st["storage"]) or (now - st.get("pool_check", 0) > 300) or force
        if need_pool:
            pool = None
            luci = st.get("luci_port")
            # 优先相邻端口（luci 网关通常就在 webdav 端口旁边）
            for cand in ([luci] if luci else []) + [wd_port - 1, wd_port + 1]:
                if cand and cand in ports and cand != wd_port:
                    pool = self._probe_pool_once(cand)
                    if pool is not None:
                        luci = cand
                        break
            if pool is None:
                rest = [p for p in ports if p not in (wd_port, luci)]

                def _try_pool(p):
                    r = self._probe_pool_once(p)
                    return (p, r) if r is not None else None

                res = _parallel_first(rest, _try_pool)
                if res:
                    luci, pool = res
            if pool is not None:
                parsed = _parse_pool_info(pool)
                if parsed:
                    st["luci_port"] = luci
                    st["pool_check"] = now
                    if parsed["storage"]:
                        st["storage"] = parsed["storage"]
                    if parsed["username"]:
                        st["username"] = parsed["username"]
                    if parsed["password"]:
                        st["password"] = parsed["password"]
        m = re.search(r"(\d+)_", (self.cert_files or {}).get("cert", "") or "")
        if m:
            st["uid"] = m.group(1)
        return st

    def _probe_pool_once(self, port, timeout=5):
        """单端口调 get_pool_info：当前解释器支持则直连，否则走子进程解释器。
        只返回 code==0 且可解析的结果；其余情况返回 None。"""
        pool = None
        if self.cert_ok:
            pool = probe_luci(port, self.cert_files, timeout=timeout)
        else:
            pool = probe_pool_info_via_helper(port, timeout=timeout)
        if pool is not None and _parse_pool_info(pool) is not None:
            return pool
        return None


if __name__ == "__main__":
    cm = ConnManager()
    st = cm.refresh(force=True)
    print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
