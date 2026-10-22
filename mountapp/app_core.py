#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
控制面兼容模块：配置、目标发布、挂载身份与 Finder 操作。

最终架构由 ResidentService 作为唯一运行时所有者；AppCore 提供可注入的发现/挂载适配能力，
不得被 GUI 作为第二个后台控制面启动。目标经 state_store.publish_target 的 CAS 发布，代理只读；
挂载只认领本地代理 127.0.0.1:18445，凭证经 stdin；常驻服务内嵌 ProxyServer，GUI 退出后
发现、轮换与自愈继续运行。
"""
import base64
import http.client
import json
import os
import re
import subprocess
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import conn_manager as cm

APP_SUPPORT = os.path.expanduser("~/Library/Application Support/小米智能存储挂载助手")
CONFIG_PATH = os.path.join(APP_SUPPORT, "config.json")
NETFS_MOUNT = os.path.join(BASE_DIR, "netfs_mount")
PROXY_PORT = 18445
PROXY_HOST = "127.0.0.1"
MOUNT_URL = f"http://{PROXY_HOST}:{PROXY_PORT}/"
DEFAULT_MOUNT_POINT = "/Volumes/127.0.0.1"

# NetFS helper 能力版本标记（与 netfs_mount.m --protocol 对齐）；旧二进制无标记一律拒绝。
PROTOCOL_MARKER = "MWDAV_MOUNT_PROTOCOL=2"

DEFAULT_CONFIG = {
    "auto_mount": True,
    "auto_start": False,
    "mount_dir": "",
}


def load_app_config(path=CONFIG_PATH, override=None):
    """读取应用配置：已保存值覆盖默认值，未知但合法的 TLS 键予以保留。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update(saved)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        pass
    if isinstance(override, dict):
        cfg.update(override)
    return cfg


def save_app_config(config, path=CONFIG_PATH):
    """以 0600 原子保存配置；失败向调用者抛出，不能让 UI 假装已保存。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    tmp = path + ".tmp.%d.%d" % (os.getpid(), threading.get_ident())
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ======================= 挂载身份（纯函数，可单测） =======================

def parse_mount_table(lines, host=PROXY_HOST, port=PROXY_PORT):
    """解析 /sbin/mount 输出，返回 [{source,host,port,mountpoint}]，不做认领。"""
    entries = []
    for line in lines or []:
        if " on " not in line:
            continue
        src, rest = line.split(" on ", 1)
        m = re.match(r"\s*(\S+?)\s+\((.*)\)\s*$", rest.strip())
        mp = m.group(1) if m else rest.split()[0]
        netloc = src
        if "://" in netloc:
            # WebDAV/FTP 风格 URL 源：http://host:port/path
            netloc = netloc.split("://", 1)[1].split("/", 1)[0]
        elif netloc.startswith("//"):
            netloc = netloc[2:]
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]
        h, _, p = netloc.partition(":")
        p = p.rstrip("/")
        entries.append({
            "source": src.strip(),
            "host": h.strip(),
            "port": int(p) if p.isdigit() else None,
            "mountpoint": mp.strip(),
            "raw": line.rstrip(),
        })
    return entries


def _port_in_use(port, host=PROXY_HOST, timeout=0.5):
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class AppCore:
    def __init__(self, config=None):
        self.config = self._load_config(config)
        # 严格 TLS 配置（fail-closed）；缺 server_name/CA 在发现层阻断
        try:
            self.tls = cm.load_tls_config(self.config)
        except cm.TlsConfigError as e:
            self.tls = None
            self._tls_error = e.diagnostic
        self.conn = cm.ConnManager(tls=getattr(self, "tls", None))
        self.conn.exclude_port = PROXY_PORT
        self.proxy_proc = None
        # 常驻服务内嵌代理钩子：非 None 时 ensure_proxy 直接复用进程内代理，不再 fork 子进程
        self.embedded_proxy = None
        self.lock = threading.Lock()
        self.mounted = False
        self.mount_path = None
        self.status_text = "初始化中…"
        self.refresh_interval = 20
        self._stop = threading.Event()
        self._thread = None
        self._storage_thread = None
        self._generation = None
        self._dead_streak = 0
        self._stale_streak = 0
        self._user_unmounted = False  # 用户主动卸载意图，下一轮不自动挂回

    # ---------- 配置 ----------
    def _load_config(self, override=None):
        return load_app_config(CONFIG_PATH, override)

    def save_config(self):
        save_app_config(self.config, CONFIG_PATH)

    # ---------- 状态模块（延迟导入；未就绪不本地另写实现） ----------
    def _state_store(self):
        try:
            import state_store
            return state_store
        except Exception:
            return None

    def _snapshot(self):
        ss = self._state_store()
        if ss is None:
            return None
        try:
            return ss.load_snapshot()
        except Exception:
            return None

    def _publish_target(self, port, username, password):
        ss = self._state_store()
        if ss is None:
            self.status_text = "共享状态模块 state_store 未就绪，目标未发布"
            return False
        tls = None
        if self.tls is not None:
            tls = ss.TlsTarget(ca_file=self.tls.ca_file, cert_file=self.tls.cert_file,
                               key_file=self.tls.key_file,
                               server_name=self.tls.server_name)
        try:
            snap = ss.publish_target(
                host=PROXY_HOST, port=port, username=username, password=password,
                base="/pool0/data", tls=tls,
                expected_generation=self._generation)
        except Exception as e:
            self.status_text = f"目标发布失败: {e}"
            return False
        self._generation = getattr(snap, "generation", None)
        return True

    # ---------- 连接 ----------
    def refresh_connection(self, force=False):
        return self.conn.refresh(force=force)

    # ---------- 代理进程（独立子进程；凭证只经状态文件，不进 argv） ----------
    @staticmethod
    def _proxy_pid_on(port):
        try:
            r = subprocess.run(["lsof", "-nP", "-a", "-iTCP:%d" % port, "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=5)
            for line in (r.stdout or "").splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1])
        except Exception:
            pass
        return None

    @staticmethod
    def _cmdline(pid):
        try:
            r = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                               capture_output=True, text=True, timeout=3)
            return r.stdout or ""
        except Exception:
            return ""

    def ensure_proxy(self):
        # 常驻内嵌模式：复用进程内 ProxyServer，不再拉起独立代理子进程
        if self.embedded_proxy is not None:
            if not getattr(self.embedded_proxy, "is_running", lambda: False)():
                self.embedded_proxy.start()
            return True
        pid = self._proxy_pid_on(PROXY_PORT)
        if pid:
            cl = self._cmdline(pid)
            if "mwdav_proxy" in cl or "main.py" in cl or "app_core" in cl:
                self.proxy_proc = pid
                return True
            self.status_text = f"端口 {PROXY_PORT} 被其他程序占用（PID {pid}），请关闭后重试"
            return False
        st = self.conn.snapshot()
        if not (st["webdav_port"] and st["username"] and st["password"]):
            self.status_text = "代理未启动：连接或凭证不可用"
            return False
        log = open(os.path.join(APP_SUPPORT, "proxy.log"), "ab")
        try:
            self.proxy_proc = subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, "mwdav_proxy.py"),
                 "--port", str(PROXY_PORT), "--upstream", str(st["webdav_port"])],
                stdout=log, stderr=log, start_new_session=True)
        except Exception as e:
            self.status_text = f"代理启动失败: {e}"
            return False
        for _ in range(20):
            if _port_in_use(PROXY_PORT):
                return True
            time.sleep(0.3)
        self.status_text = "代理进程已启动但端口未就绪，请稍候"
        return False

    # ---------- 挂载身份与认领（T07/T08） ----------
    def _expected_mountpoint(self):
        return self.config.get("mount_dir") or DEFAULT_MOUNT_POINT

    def _own_mount_entry(self):
        """仅认领源为本代理回环端点、且挂载点匹配期望的条目。"""
        try:
            out = subprocess.run(["/sbin/mount"], capture_output=True, text=True)
            lines = (out.stdout or "").splitlines()
        except Exception:
            return None
        expected = self._expected_mountpoint()
        for e in parse_mount_table(lines, PROXY_HOST, PROXY_PORT):
            if e["host"] == PROXY_HOST and e["port"] == PROXY_PORT \
                    and os.path.normpath(e["mountpoint"]) == os.path.normpath(expected):
                return e
        return None

    def find_mount_path(self):
        entry = self._own_mount_entry()
        return entry["mountpoint"] if entry else None

    def is_mounted(self):
        p = self.find_mount_path()
        if p:
            self.mount_path = p
            self.mounted = True
            return True
        self.mount_path = None
        self.mounted = False
        return False

    # ---------- NetFS helper 能力握手（拒绝旧二进制） ----------
    def _helper_protocol(self):
        try:
            r = subprocess.run([NETFS_MOUNT, "--protocol"],
                               capture_output=True, text=True, timeout=5)
            return r.stdout or ""
        except Exception:
            return ""

    def _helper_supported(self):
        return PROTOCOL_MARKER in self._helper_protocol()

    # ---------- 挂载（凭证走 stdin，不进 argv） ----------
    def mount_with_credentials(self, url, mountpoint, username, password):
        """用调用方已确认的目标执行 NetFS 挂载。

        常驻服务是发现状态的唯一所有者，因此生产 MountBackend 必须把服务当前
        DiscoveryView 中的凭证显式传入，不能再从一个新建 AppCore 的空 ConnManager
        快照取值。默认 /Volumes/127.0.0.1 由 NetFS 自行选择，不作为显式 mountpoint
        传入，以保持 Finder 可浏览行为；自定义 /Volumes 路径才传给 helper。
        """
        if not (url and username and password):
            self.status_text = "无法挂载：连接或凭证不可用"
            return False, "连接不可用"
        if self.is_mounted():
            self.status_text = "已挂载"
            return True, "已挂载"
        if not self._helper_supported():
            self.status_text = "挂载组件版本过旧：请在 macOS 重新编译 netfs_mount"
            return False, "helper 协议过旧，需重新编译"

        requested = mountpoint or ""
        explicit = "" if os.path.normpath(requested or DEFAULT_MOUNT_POINT) \
            == os.path.normpath(DEFAULT_MOUNT_POINT) else requested
        args = [NETFS_MOUNT, "mount", url]
        if explicit:
            args.append(explicit)
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)
            out, err = proc.communicate(
                input=f"{username}\n{password}\n".encode(), timeout=40)
            text = (out or b"").decode("utf-8", "replace") + \
                (err or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            # communicate 超时不会自动终止子进程；必须 kill 后再次 communicate 以回收，
            # 避免 helper 在后台继续执行一次迟到的挂载。
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.communicate()
            except Exception:
                pass
            self.status_text = "挂载失败: netfs_mount 超时，已终止并回收"
            return False, "挂载超时，helper 已终止"
        except Exception as e:
            self.status_text = f"挂载失败: {e}"
            return False, str(e)

        if "MOUNT_OK" in text:
            self._user_unmounted = False
            self.mounted = True
            self.status_text = "挂载成功"
            return True, "挂载成功"
        if "MOUNT_FAIL" in text and any(x in text for x in ("err=16", "err=17")):
            if self.is_mounted():
                self._user_unmounted = False
                self.status_text = "已挂载（复用现有挂载）"
                return True, "已挂载"
        self.status_text = f"挂载失败: {text.strip()[:100]}"
        return False, text.strip()[:200]

    def mount(self):
        """GUI 会话兼容入口：从本会话 ConnManager 取目标后调用显式凭证挂载。"""
        st = self.conn.snapshot()
        if not (st["connected"] and st["webdav_port"] and st["username"] and st["password"]):
            self.status_text = "无法挂载：连接或凭证不可用"
            return False, "连接不可用"
        if not self._publish_target(st["webdav_port"], st["username"], st["password"]):
            return False, "代理目标未发布"
        if not self.ensure_proxy():
            return False, "代理未启动"
        return self.mount_with_credentials(
            MOUNT_URL, self._expected_mountpoint(), st["username"], st["password"])

    def unmount(self):
        entry = self._own_mount_entry()
        if not entry:
            self.mounted = False
            self.mount_path = None
            self.status_text = "未挂载"
            return True, "未挂载"
        p = entry["mountpoint"]
        try:
            r = subprocess.run([NETFS_MOUNT, "unmount", p],
                               capture_output=True, text=True, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return False, str(e)
        ok = "UNMOUNT_OK" in out
        if ok:
            self.mounted = False
            self.mount_path = None
            self._user_unmounted = True  # 保留用户意图，下一轮不自动挂回
            self.status_text = "已卸载"
        else:
            self.status_text = f"卸载失败: {out.strip()[:80]}"
        return ok, out.strip()[:200]

    def open_in_finder(self):
        p = self.find_mount_path()
        if not p:
            return False, "未挂载"
        subprocess.Popen(["open", p])
        return True, p

    # ---------- 健康探活（凭证从快照读取，不进 argv） ----------
    def _basic(self):
        snap = self._snapshot()
        if snap is None:
            return None
        return getattr(snap, "username", ""), getattr(snap, "password", "")

    def proxy_alive(self, timeout=3):
        cred = self._basic()
        if not cred or not cred[0]:
            return False
        try:
            conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=timeout)
            token = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
            conn.request("OPTIONS", "/", headers={"Authorization": f"Basic {token}"})
            resp = conn.getresponse()
            resp.read()
            code = resp.status
            conn.close()
            return code in (200, 207)
        except Exception:
            return False

    def mount_point_alive(self, timeout=8):
        p = self.find_mount_path()
        if not p:
            return False
        try:
            r = subprocess.run(["ls", "-1A", p], capture_output=True,
                               text=True, timeout=timeout)
            if r.returncode != 0:
                return False
            local = {x for x in r.stdout.splitlines()
                     if x and not x.startswith(".")}
        except Exception:
            return False
        cred = self._basic()
        remote = set()
        if cred and cred[0]:
            try:
                conn = http.client.HTTPConnection(PROXY_HOST, PROXY_PORT, timeout=timeout)
                token = base64.b64encode(f"{cred[0]}:{cred[1]}".encode()).decode()
                conn.request("PROPFIND", "/", headers={
                    "Authorization": f"Basic {token}", "Depth": "1"})
                resp = conn.getresponse()
                body = resp.read().decode("utf-8", "replace")
                conn.close()
                remote = {x for x in re.findall(
                    r"<[A-Za-z_][\w.-]*:?displayname>([^<]+)</[A-Za-z_][\w.-]*:?displayname>",
                    body) if x and not x.startswith(".")}
            except Exception:
                remote = set()
        if not remote:
            return True
        return bool(local & remote)

    def ensure_mount_health(self):
        cred = self._basic()
        if not cred or not cred[0]:
            return
        mounted = self.is_mounted()
        alive = self.proxy_alive()
        if alive:
            self._dead_streak = 0
            if not mounted:
                self._stale_streak = 0
                if self.config.get("auto_mount") and not self._user_unmounted \
                        and self.conn.snapshot()["connected"]:
                    self.mount()
                return
            if not self.mount_point_alive():
                self._stale_streak += 1
                if self._stale_streak >= 2:
                    self.status_text = "检测到挂载失效，正在重新挂载…"
                    self.unmount()
                    time.sleep(1)
                    ok, _ = self.mount()
                    self.status_text = "已自动重新挂载" if ok else "重新挂载失败，稍后重试"
                    self._stale_streak = 0
            else:
                self._stale_streak = 0
            return
        self._stale_streak = 0
        self._dead_streak += 1
        if mounted and self._dead_streak >= 2:
            self.status_text = "隧道波动，已清理失效挂载，恢复后自动重连"
            self.unmount()
            self._dead_streak = 0

    # ---------- 后台循环（快路径；慢存储独立线程，不阻塞发布） ----------
    def _loop(self):
        while not self._stop.is_set():
            try:
                st = self.refresh_connection()
                if st["connected"]:
                    self.ensure_proxy()
                    self._publish_target(st["webdav_port"], st["username"], st["password"])
                    if not self.status_text or "初始化" in self.status_text:
                        self.status_text = "已就绪"
                self.ensure_mount_health()
            except Exception as e:
                self.status_text = f"后台错误: {e}"
            self._stop.wait(self.refresh_interval)

    def _storage_loop(self):
        while not self._stop.is_set():
            try:
                self.conn.poll_storage()
            except Exception:
                pass
            self._stop.wait(60)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self._storage_thread = threading.Thread(target=self._storage_loop, daemon=True)
        self._storage_thread.start()

    def stop(self):
        self._stop.set()

    # ---------- 系统集成（委托可测 launch_agent；runner 接收 argv 列表，测试不写真实系统） ----------
    def set_auto_start(self, enabled, runner=None, agent_dir=None, uid=None):
        self.config["auto_start"] = enabled
        self.save_config()
        import launch_agent as la
        runner = runner or (lambda argv: subprocess.run(argv, check=True))
        agent_dir = agent_dir or os.path.expanduser("~/Library/LaunchAgents")
        launcher = os.path.join(BASE_DIR, "launcher.sh")
        if not enabled:
            la.uninstall(agent_dir, runner=runner, uid=uid)
            return
        la.install(launcher, agent_dir, runner=runner, uid=uid)


if __name__ == "__main__":
    core = AppCore()
    core.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        core.stop()
