#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
常驻用户服务（runtime owner）：单进程持有发现、CAS 发布、挂载协调与内嵌 ProxyServer。

架构（docs/specs/runtime.md v4 §8 冻结）：
- 一个常驻进程；GUI 仅经受保护 Unix socket 发命令、读脱敏 status，不持有发现/发布/自愈线程。
- GUI 关闭服务继续 A→B 发现、CAS 发布与健康恢复；stop 为显式语义。
- 构造注入 discovery / mount_registry / proxy_factory；生产 IPC 不暴露 test-mode、
  不允许设置任意上游目标/凭证。
- 单实例 flock；命令串行；status 脱敏；凭证不进 argv、不进 IPC 回显。
"""
import dataclasses
import fcntl
import os
import signal
import threading
import time

import conn_manager as cm
import app_core as ac
from ipc import READONLY_CMDS

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 18445
MOUNT_URL = f"http://{PROXY_HOST}:{PROXY_PORT}/"
DEFAULT_MOUNT_POINT = "/Volumes/127.0.0.1"
WEBDAV_BASE = "/pool0/data"
DEFAULT_INTERVAL = 20.0


class InstanceAlreadyRunning(RuntimeError):
    """第二实例抢单实例锁失败；携带已有 pid，不绑定端口、不起代理。"""

    def __init__(self, pid):
        self.pid = pid
        super().__init__(f"已有服务实例运行（pid={pid}）")


def _default_state_path() -> str:
    try:
        import state_store
        return state_store.default_state_path()
    except Exception:
        return os.path.expanduser(
            "~/Library/Application Support/小米智能存储挂载助手/state.json")


def default_socket_path() -> str:
    return os.path.join(os.path.dirname(_default_state_path()), "mwdav.sock")


@dataclasses.dataclass(frozen=True)
class DiscoveryView:
    connected: bool
    mode: str
    webdav_port: int | None
    username: str
    password: str
    storage: list
    error: str


# ======================= 默认适配（仅生产 macOS 路径；测试一律注入 fake） =======================

class ConnDiscovery:
    """把真实 ConnManager 适配为 DiscoveryBackend。"""

    def __init__(self, conn: cm.ConnManager):
        self.conn = conn

    def refresh(self, force=False) -> DiscoveryView:
        st = self.conn.refresh(force=force)
        return DiscoveryView(
            connected=bool(st.get("connected")),
            mode=st.get("mode") or "",
            webdav_port=st.get("webdav_port"),
            username=st.get("username") or "",
            password=st.get("password") or "",
            storage=st.get("storage") or [],
            error=st.get("error") or "")

    def poll_storage(self) -> list:
        st = self.conn.poll_storage()
        return st.get("storage") or []


class SystemMount:
    """把真实 AppCore（netfs stdin + 挂载表身份）适配为 MountBackend。"""

    def __init__(self, core):
        self.core = core

    def ensure_capable(self) -> bool:
        return self.core._helper_supported()

    def mount(self, url, mountpoint, username, password):
        """使用 ResidentService 已发现的凭证挂载，不读取 core.conn 的独立空状态。"""
        return self.core.mount_with_credentials(
            url, mountpoint, username, password)

    def unmount(self, mountpoint):
        return self.core.unmount()

    def own_mountpoint(self, expected):
        return self.core.find_mount_path()

    def alive(self, expected):
        return self.core.mount_point_alive()

    def open_finder(self):
        return self.core.open_in_finder()


# ======================= 常驻服务 =======================

class ResidentService:
    def __init__(self, config: dict | None = None, *,
                 config_path: str | None = None,
                 state_path: str | None = None,
                 socket_dir: str | None = None,
                 discovery=None,
                 mount_registry=None,
                 proxy_factory=None,
                 adopt_target=None,
                 auto_start_setter=None,
                 proxy_port: int = PROXY_PORT,
                 stop_timeout: float = 5.0,
                 interval: float = DEFAULT_INTERVAL):
        # 生产无参启动读取真实配置；测试注入 config 且不传 config_path 时保持纯内存，
        # 避免离线测试读写用户目录或操作真实 LaunchAgent。
        self.config_path = (config_path if config_path is not None
                            else (ac.CONFIG_PATH if config is None else None))
        if self.config_path is not None:
            self.config = ac.load_app_config(self.config_path, config)
        else:
            self.config = dict(ac.DEFAULT_CONFIG)
            self.config.update(config or {})
        if auto_start_setter is not None:
            self._auto_start_setter = auto_start_setter
        elif self.config_path is not None:
            self._auto_start_setter = self._default_auto_start_setter
        else:
            self._auto_start_setter = lambda _enabled: None
        self.proxy_port = proxy_port
        self.stop_timeout = stop_timeout
        self.interval = interval

        # 状态文件 / socket 目录（测试传 tmpdir；缺省用共享默认）
        self.state_path = state_path
        self.socket_dir = socket_dir or os.path.dirname(
            self._default_state_path())
        self.socket_path = os.path.join(self.socket_dir, "mwdav.sock")
        self.lock_path = os.path.join(self.socket_dir, "service.lock")

        # TLS（fail-closed）；缺配置记 tls_error，不降级、不伪造身份
        self.tls = None
        self.tls_error = ""
        try:
            self.tls = cm.load_tls_config(self.config)
        except cm.TlsConfigError as e:
            self.tls_error = e.diagnostic

        # 注入或默认适配
        self.discovery = discovery or self._default_discovery()
        self.mount = mount_registry or self._default_mount()
        self._proxy_factory = proxy_factory
        self._proxy = None
        # 目标采纳：默认委托全局单例 UP.adopt（一个 service 仅一个 UP）；测试注入记录 fake。
        # ProxyServer 只提供生命周期 API，不提供每实例 adopt，避免多实例全局混乱。
        self._adopt_target = adopt_target or self._default_adopt_target

        # 运行时内存状态（健康只在内存，不落盘）
        self._view = DiscoveryView(False, "", None, "", "", [], "")
        self._generation = None
        self._mounted = False
        self._mount_path = None
        self._user_unmounted = False
        self._dead_streak = 0
        self._stale_streak = 0

        self._stop = threading.Event()
        self._loop_thread = None
        self._storage_thread = None
        self._ipc = None
        self._lock_fd = None
        self._command_lock = threading.Lock()

    # ---------- 默认装配 ----------
    @staticmethod
    def _default_state_path():
        try:
            import state_store
            return state_store.default_state_path()
        except Exception:
            return os.path.expanduser(
                "~/Library/Application Support/小米智能存储挂载助手/state.json")

    def _default_discovery(self) -> ConnDiscovery:
        conn = cm.ConnManager(tls=self.tls)
        conn.exclude_port = self.proxy_port
        return ConnDiscovery(conn)

    @staticmethod
    def _default_adopt_target(snap) -> bool:
        """默认委托进程内唯一全局 UP.adopt；service 不新建第二个上游单例。"""
        import mwdav_proxy
        return mwdav_proxy.UP.adopt(snap)

    def _default_mount(self) -> SystemMount:
        core = ac.AppCore(self.config)
        core.tls = self.tls
        core.conn.exclude_port = self.proxy_port
        return SystemMount(core)

    @staticmethod
    def _launcher_path():
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "launcher.sh")

    @staticmethod
    def _agent_dir():
        return os.path.expanduser("~/Library/LaunchAgents")

    def _default_auto_start_setter(self, enabled):
        import launch_agent
        if enabled:
            launch_agent.install(self._launcher_path(), self._agent_dir())
        else:
            launch_agent.uninstall(self._agent_dir())

    def _save_config(self):
        if self.config_path is not None:
            ac.save_app_config(self.config, self.config_path)

    # ---------- 单实例锁 ----------
    def _acquire_lock(self):
        os.makedirs(self.socket_dir, exist_ok=True)
        os.chmod(self.socket_dir, 0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            existing = self._read_lock_pid()
            raise InstanceAlreadyRunning(existing)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def _read_lock_pid(self) -> int | None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                pid = int(f.read().strip())
            return pid if pid > 0 else None
        except (OSError, ValueError):
            return None

    def _release_lock(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None

    # ---------- 内嵌代理 ----------
    def _start_proxy(self):
        if self._proxy is not None and self._proxy_is_running():
            return True
        if self._proxy_factory is not None:
            self._proxy = self._proxy_factory(self.proxy_port)
        else:
            import mwdav_proxy
            self._proxy = mwdav_proxy.ProxyServer(
                self.proxy_port, enable_https=False)
        try:
            self._proxy.start()
        except OSError as e:
            self._proxy = None
            self._view = dataclasses.replace(
                self._view, error=f"本地代理端口 {self.proxy_port} 占用，未启动: {e}")
            return False
        return self._proxy_is_running()

    def _proxy_is_running(self) -> bool:
        return bool(self._proxy and getattr(self._proxy, "is_running", lambda: False)())

    # ---------- CAS 发布 + 内存采纳 ----------
    def _state_store(self):
        try:
            import state_store
            return state_store
        except Exception:
            return None

    def _publish(self, view: DiscoveryView):
        ss = self._state_store()
        if ss is None:
            self.tls_error = "共享状态模块 state_store 未就绪，目标未发布"
            return False
        tls = None
        if self.tls is not None:
            tls = ss.TlsTarget(
                ca_file=self.tls.ca_file, cert_file=self.tls.cert_file,
                key_file=self.tls.key_file, server_name=self.tls.server_name)
        try:
            kwargs = dict(host=PROXY_HOST, port=view.webdav_port,
                          username=view.username, password=view.password,
                          base=WEBDAV_BASE, tls=tls,
                          expected_generation=self._generation)
            if self.state_path:
                kwargs["path"] = self.state_path
            snap = ss.publish_target(**kwargs)
        except Exception as e:
            self._view = dataclasses.replace(self._view, error=f"目标发布失败: {e}")
            return False
        self._generation = getattr(snap, "generation", self._generation)
        try:
            self._adopt_target(snap)
        except Exception:
            pass
        return True

    # ---------- 挂载协调 / 自愈 ----------
    def _expected_mountpoint(self):
        return self.config.get("mount_dir") or DEFAULT_MOUNT_POINT

    def _refresh_mount_state(self):
        mp = self.mount.own_mountpoint(self._expected_mountpoint())
        self._mounted = bool(mp)
        self._mount_path = mp

    def _mount_health(self, view: DiscoveryView):
        self._refresh_mount_state()
        if not view.connected:
            self._stale_streak = 0
            self._dead_streak += 1
            if self._mounted and self._dead_streak >= 2:
                # 系统自愈卸载不能覆盖用户意图；仅显式 IPC unmount 才设置 _user_unmounted。
                self.mount.unmount(self._expected_mountpoint())
                self._refresh_mount_state()
                self._dead_streak = 0
            return
        self._dead_streak = 0
        if not self._proxy_is_running():
            return
        if not self._mounted:
            self._stale_streak = 0
            if (self.config.get("auto_mount", True) and not self._user_unmounted
                    and view.webdav_port and view.username and view.password):
                self.mount.mount(MOUNT_URL, self._expected_mountpoint(),
                                  view.username, view.password)
                self._refresh_mount_state()
            return
        alive = getattr(self.mount, "alive", lambda _expected: True)(
            self._expected_mountpoint())
        if not alive:
            self._stale_streak += 1
            if self._stale_streak >= 2:
                self.mount.unmount(self._expected_mountpoint())
                self._refresh_mount_state()
                if (self.config.get("auto_mount", True) and not self._user_unmounted
                        and view.webdav_port and view.username and view.password):
                    self.mount.mount(MOUNT_URL, self._expected_mountpoint(),
                                      view.username, view.password)
                    self._refresh_mount_state()
                self._stale_streak = 0
        else:
            self._stale_streak = 0

    # ---------- 后台循环 ----------
    def _loop(self):
        while not self._stop.is_set():
            try:
                view = self.discovery.refresh(force=False)
                self._view = view
                if view.connected and view.webdav_port:
                    if (self.tls is not None and not self.tls.server_name
                            and view.webdav_port):
                        import conn_manager as _cm
                        cn = _cm.discover_server_name(view.webdav_port)
                        if cn:
                            self.tls = dataclasses.replace(
                                self.tls, server_name=cn)
                    self._start_proxy()
                    self._publish(view)
                self._mount_health(view)
            except Exception as e:
                self._view = dataclasses.replace(
                    self._view, error=f"后台错误: {type(e).__name__}: {e}")
            self._stop.wait(self.interval)

    def _storage_loop(self):
        while not self._stop.is_set():
            try:
                storage = self.discovery.poll_storage()
                if storage:
                    self._view = dataclasses.replace(self._view, storage=storage)
            except Exception:
                pass
            self._stop.wait(60)

    # ---------- 生命周期 ----------
    def start(self):
        self._acquire_lock()
        try:
            if self._ipc is None:
                from ipc import IpcServer
                self._ipc = IpcServer(self.socket_path, self.dispatch)
                self._ipc.start()
            self._stop.clear()
            self._loop_thread = threading.Thread(target=self._loop, daemon=True)
            self._loop_thread.start()
            self._storage_thread = threading.Thread(target=self._storage_loop, daemon=True)
            self._storage_thread.start()
        except Exception:
            if self._ipc is not None:
                try:
                    self._ipc.stop()
                except Exception:
                    pass
                self._ipc = None
            self._release_lock()
            raise

    def serve(self) -> int:
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        finally:
            self.stop()
        return 0

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        for t in (self._loop_thread, self._storage_thread):
            if t is not None:
                t.join(self.stop_timeout)
        if self._ipc is not None:
            self._ipc.stop()
            self._ipc = None
        if self._proxy is not None:
            try:
                self._proxy.stop(self.stop_timeout)
            except Exception:
                pass
        self._release_lock()

    def last_generation(self):
        return self._generation

    # ---------- 脱敏 status（白名单，绝不含凭证） ----------
    def status_view(self) -> dict:
        # 纯内存快照：不在这里探测/重写 mounted/path，避免读路径绕锁造成新竞争；
        # mounted/path 仅由自愈 loop 与 mount/unmount 尾部刷新。
        return {
            "service_pid": os.getpid(),
            "connected": self._view.connected,
            "mode": self._view.mode,
            "webdav_port": self._view.webdav_port,
            "mounted": self._mounted,
            "mount_path": self._mount_path,
            "storage": self._view.storage,
            "error": self._view.error or self.tls_error,
            "generation": self._generation or 0,
        }

    def config_view(self) -> dict:
        return {
            "auto_mount": bool(self.config.get("auto_mount", True)),
            "auto_start": bool(self.config.get("auto_start", False)),
            "mount_dir": self.config.get("mount_dir", ""),
        }

    # ---------- IPC 命令（白名单 + 校验 + 串行） ----------
    def dispatch(self, cmd: str, args: dict) -> dict:
        handler = {
            "status": self._cmd_status,
            "get_config": self._cmd_get_config,
            "mount": self._cmd_mount,
            "unmount": self._cmd_unmount,
            "open_finder": self._cmd_open_finder,
            "refresh": self._cmd_refresh,
            "set_config": self._cmd_set_config,
            "stop": self._cmd_stop,
        }.get(cmd)
        if handler is None:
            raise ValueError(f"unknown command: {cmd}")
        # 只读命令不持写锁，避免被慢写命令（NetFS 挂载）阻塞；写命令仍串行。
        if cmd in READONLY_CMDS:
            return handler(args)
        with self._command_lock:
            return handler(args)

    def _cmd_status(self, args):
        return self.status_view()

    def _cmd_get_config(self, args):
        return self.config_view()

    def _cmd_mount(self, args):
        view = self._view
        if not (view.connected and view.webdav_port
                and view.username and view.password):
            raise ValueError("连接或凭证不可用，无法挂载")
        # 代理未启动（端口被占/启动失败）时拒绝进入 NetFS 挂载，
        # 避免把 NAS 凭证发往非自有 127.0.0.1 监听者。
        if not self._start_proxy():
            raise ValueError("本地代理未启动，拒绝挂载以避免凭证发往非自有端口")
        self._publish(view)
        ok, msg = self.mount.mount(MOUNT_URL, self._expected_mountpoint(),
                                   view.username, view.password)
        if not ok:
            raise ValueError(msg or "挂载失败")
        self._user_unmounted = False
        self._refresh_mount_state()
        return {"mounted": self._mounted, "mount_path": self._mount_path}

    def _cmd_unmount(self, args):
        ok, msg = self.mount.unmount(self._expected_mountpoint())
        if not ok:
            raise ValueError(msg or "卸载失败")
        self._user_unmounted = True
        self._refresh_mount_state()
        return {"mounted": False, "mount_path": None}

    def _cmd_open_finder(self, args):
        ok, payload = self.mount.open_finder()
        if not ok:
            raise ValueError(payload or "未挂载")
        return {"opened": True, "mount_path": payload}

    def _cmd_refresh(self, args):
        view = self.discovery.refresh(force=True)
        self._view = view
        if view.connected and view.webdav_port:
            self._start_proxy()
            self._publish(view)
        return {"connected": view.connected, "webdav_port": view.webdav_port}

    def _cmd_set_config(self, args):
        # 仅允许声明键；未知键拒绝，且全部校验通过前不写任何配置（失败零写）
        allowed = {"auto_mount", "auto_start", "mount_dir"}
        unknown = set(args) - allowed
        if unknown:
            raise ValueError(f"未知配置键: {sorted(unknown)}")
        if "auto_mount" in args and not isinstance(args["auto_mount"], bool):
            raise ValueError("auto_mount 必须是布尔")
        if "auto_start" in args and not isinstance(args["auto_start"], bool):
            raise ValueError("auto_start 必须是布尔")
        if "mount_dir" in args:
            md = args["mount_dir"]
            if not isinstance(md, str) or not md.strip():
                raise ValueError("mount_dir 必须是非空字符串")
            raw_parts = md.split(os.sep)
            norm = os.path.normpath(md)
            # 仅接受 /Volumes 下归一化绝对路径；拒绝 .. 逃逸与非绝对路径
            if ".." in raw_parts or not norm.startswith("/Volumes"):
                raise ValueError("mount_dir 须为 /Volumes 下的归一化绝对路径，拒绝逃逸")
            pending_mount_dir = norm
        pending = dict(self.config)
        if "auto_mount" in args:
            pending["auto_mount"] = args["auto_mount"]
        if "auto_start" in args:
            pending["auto_start"] = args["auto_start"]
        if "mount_dir" in args:
            pending["mount_dir"] = pending_mount_dir

        old = dict(self.config)
        self.config = pending
        try:
            self._save_config()
            if "auto_start" in args and old.get("auto_start", False) != pending["auto_start"]:
                try:
                    self._auto_start_setter(pending["auto_start"])
                except Exception:
                    # LaunchAgent 可能已部分变更，尽力恢复旧布尔状态再回滚配置。
                    try:
                        self._auto_start_setter(old.get("auto_start", False))
                    except Exception:
                        pass
                    raise
        except Exception:
            self.config = old
            # 尽力把磁盘恢复为旧值；原始异常继续抛给 IPC，不能假装成功。
            try:
                self._save_config()
            except Exception:
                pass
            raise
        return {"auto_mount": self.config.get("auto_mount", True),
                "auto_start": self.config.get("auto_start", False),
                "mount_dir": self.config.get("mount_dir", "")}

    def _cmd_stop(self, args):
        self.stop()
        return {"stopped": True}
