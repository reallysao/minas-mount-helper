#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米智能存储 Finder 挂载助手 - GUI 瘦客户端。

架构（docs/specs/runtime.md v4）：发现/发布/自愈/内嵌代理全部在常驻服务进程；
本窗口只经受保护 Unix socket 发命令、读脱敏 status，关闭窗口不停止服务。
`main.py --service` 由 LaunchAgent 以前台常驻方式启动 ResidentService。
"""
import os
import sys
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import tkinter as tk
from tkinter import ttk, filedialog  # noqa: F401

ORANGE = "#ff6900"
GREEN = "#2ecc71"
RED = "#e74c3c"
GRAY = "#8e8e93"
BG = "#f5f5f7"
CARD = "#ffffff"
TEXT = "#1d1d1f"
SUBTEXT = "#6e6e73"
BLUE = "#0a84ff"


def human_size(n):
    """字节数 -> 可读文本（1024 进制）；纯函数，刷新路径零写。"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


class _ServiceCore:
    """把 IpcClient 适配成旧 App.refresh 期望的只读/命令接口（无凭证）。

    双连接隔离：status client 专供后台线程拉取快照并缓存，command client 专供
    异步写命令。Tk 主线程只读本对象缓存，不发起同步网络调用，避免 socket
    timeout（最长 10s）冻结界面；慢写命令（挂载）也不再阻塞状态刷新。
    """

    def __init__(self, client, cmd_client=None, pump=True):
        # client 专供状态快照；cmd_client 专供异步写命令（可与 client 隔离）。
        # 仅传 client 时二者共用，保持旧构造契约。
        self._status_c = client
        self._cmd_c = cmd_client or client
        self._cached = None
        self._stop = threading.Event()
        try:
            cfg = self._cmd_c.call("get_config")
        except Exception:
            cfg = {}
        self.config = {
            "auto_mount": bool(cfg.get("auto_mount", True)),
            "auto_start": bool(cfg.get("auto_start", False)),
            "mount_dir": cfg.get("mount_dir") or "",
        }
        self.status_text = ""
        self.mount_path = None
        if pump:
            self._pump = threading.Thread(
                target=self._pump_status, args=(1.5,), daemon=True)
            self._pump.start()

    def start(self):
        """服务已在常驻进程运行；GUI 不重复启动。"""
        return None

    def _pump_status(self, interval):
        """后台线程定时拉取脱敏快照写入缓存；主线程永不阻塞在网络上。"""
        while not self._stop.is_set():
            try:
                self._cached = self._status_c.status()
            except Exception as e:
                self._cached = {"connected": False, "error": str(e), "mode": "",
                                "webdav_port": None, "storage": [],
                                "mounted": False, "mount_path": None}
            self._stop.wait(interval)

    def _status(self):
        """主线程同步读取：直接返回缓存快照，不发起网络请求。"""
        return self._cached or {"connected": False, "error": "", "mode": "",
                                "webdav_port": None, "storage": [],
                                "mounted": False, "mount_path": None}

    @property
    def conn(self):
        snap = self._status()

        class _Conn:
            @staticmethod
            def snapshot():
                return snap

        return _Conn()

    def is_mounted(self):
        snap = self._status()
        self.mount_path = snap.get("mount_path")
        return bool(snap.get("mounted"))

    def mount(self):
        return self._cmd_c.call("mount")

    def unmount(self):
        return self._cmd_c.call("unmount")

    def open_in_finder(self):
        try:
            self._cmd_c.call("open_finder")
            return True, self.mount_path
        except Exception as e:
            return False, str(e)

    def set_auto_start(self, enabled):
        data = self._cmd_c.call("set_config", {"auto_start": bool(enabled)})
        self.config.update(data)

    def save_config(self):
        data = self._cmd_c.call("set_config", self.config)
        self.config.update(data)

    def save_settings(self, auto_mount, auto_start):
        data = self._cmd_c.call("set_config", {
            "auto_mount": bool(auto_mount), "auto_start": bool(auto_start)})
        self.config.update(data)
        return data


class App(tk.Tk):
    def __init__(self, core):
        super().__init__()
        self.core = core
        self.title("小米智能存储 Finder 挂载助手")
        self.geometry("380x640")
        self.configure(bg=BG)
        self.resizable(False, False)

        try:
            self.font = ("PingFang SC", 12)
            self.font_small = ("PingFang SC", 10)
            self.font_bold = ("PingFang SC", 13, "bold")
        except Exception:
            self.font = ("Helvetica", 12)
            self.font_small = ("Helvetica", 10)
            self.font_bold = ("Helvetica", 13, "bold")

        self._build_ui()
        self.core.start()
        self.refresh()
        self.after(1500, self._tick)

    # ---------- UI ----------
    def _build_ui(self):
        container = tk.Frame(self, bg=BG, padx=20, pady=16)
        container.pack(fill="both", expand=True)

        # 头部：设备名 + 状态
        header = tk.Frame(container, bg=BG)
        header.pack(fill="x")
        self.status_dot = tk.Canvas(header, width=14, height=14, bg=BG, highlightthickness=0)
        self.status_dot.pack(side="left", pady=6)
        self.dot = self.status_dot.create_oval(2, 2, 12, 12, fill=GRAY, outline="")
        tk.Label(header, text="Xiaomi 智能存储", font=self.font_bold, bg=BG, fg=TEXT).pack(side="left", padx=(6, 0))
        self.status_label = tk.Label(header, text="检测中…", font=self.font_small, bg=BG, fg=SUBTEXT)
        self.status_label.pack(side="right")
        tk.Label(container, text="通过 P2P 隧道将 NAS 文件挂载到本机 Finder",
                 font=self.font_small, bg=BG, fg=SUBTEXT).pack(anchor="w", pady=(2, 10))

        # 存储卡片
        storage_card = tk.Frame(container, bg=CARD, padx=14, pady=12, highlightthickness=0)
        storage_card.pack(fill="x", pady=(0, 10))
        self.storage_title = tk.Label(storage_card, text="存储空间", font=self.font_bold, bg=CARD, fg=TEXT)
        self.storage_title.pack(anchor="w")
        self.storage_bar = ttk.Progressbar(storage_card, length=300, mode="determinate")
        self.storage_bar.pack(fill="x", pady=(8, 4))
        self.storage_text = tk.Label(storage_card, text="—", font=self.font_small, bg=CARD, fg=SUBTEXT)
        self.storage_text.pack(anchor="w")

        # 连接卡片
        conn_card = tk.Frame(container, bg=CARD, padx=14, pady=12)
        conn_card.pack(fill="x", pady=(0, 10))
        tk.Label(conn_card, text="连接", font=self.font_bold, bg=CARD, fg=TEXT).pack(anchor="w")
        self.conn_text = tk.Label(conn_card, text="等待检测…", font=self.font_small, bg=CARD, fg=SUBTEXT,
                                  justify="left", wraplength=310)
        self.conn_text.pack(anchor="w", pady=(6, 0))
        self.open_official_btn = tk.Button(conn_card, text="打开小米智能存储 App", font=self.font_small,
                                           bg="#e5e5ea", fg=TEXT, relief="flat", bd=0, padx=10, pady=5,
                                           activebackground="#d1d1d6", activeforeground=TEXT,
                                           command=self._open_official)
        self.open_official_btn.pack(anchor="w", pady=(8, 0))

        # 挂载卡片
        mount_card = tk.Frame(container, bg=CARD, padx=14, pady=12)
        mount_card.pack(fill="x", pady=(0, 10))
        tk.Label(mount_card, text="Finder 挂载", font=self.font_bold, bg=CARD, fg=TEXT).pack(anchor="w")
        self.mount_text = tk.Label(mount_card, text="未挂载", font=self.font_small, bg=CARD, fg=SUBTEXT,
                                   justify="left", wraplength=310)
        self.mount_text.pack(anchor="w", pady=(6, 0))
        btns = tk.Frame(mount_card, bg=CARD)
        btns.pack(anchor="w", pady=(8, 0))
        self.mount_btn = tk.Button(btns, text="挂载", font=self.font_small, bg=ORANGE, fg="white",
                                   relief="flat", bd=0, padx=16, pady=5,
                                   activebackground="#e05e00", activeforeground="white",
                                   command=self._mount)
        self.mount_btn.pack(side="left")
        self.unmount_btn = tk.Button(btns, text="卸载", font=self.font_small, bg="#e5e5ea", fg=TEXT,
                                     relief="flat", bd=0, padx=16, pady=5,
                                     activebackground="#d1d1d6", command=self._unmount)
        self.unmount_btn.pack(side="left", padx=(8, 0))
        self.open_finder_btn = tk.Button(btns, text="在 Finder 中打开", font=self.font_small, bg="#e5e5ea",
                                         fg=TEXT, relief="flat", bd=0, padx=16, pady=5,
                                         activebackground="#d1d1d6", command=self._open_finder)
        self.open_finder_btn.pack(side="left", padx=(8, 0))

        # 设置（竖排，避免 380px 宽度下截断）
        settings = tk.Frame(container, bg=BG)
        settings.pack(fill="x", pady=(2, 0))
        self.auto_mount_var = tk.BooleanVar(value=self.core.config.get("auto_mount", True))
        self.auto_start_var = tk.BooleanVar(value=self.core.config.get("auto_start", False))
        tk.Checkbutton(settings, text="连接后自动挂载", variable=self.auto_mount_var, bg=BG, fg=TEXT,
                       font=self.font_small, activebackground=BG, selectcolor=BG,
                       command=self._save_settings).pack(anchor="w")
        tk.Checkbutton(settings, text="登录时自动启动", variable=self.auto_start_var, bg=BG, fg=TEXT,
                       font=self.font_small, activebackground=BG, selectcolor=BG,
                       command=self._save_settings).pack(anchor="w", pady=(2, 0))

        # 底部状态
        self.log_label = tk.Label(container, text="", font=("PingFang SC", 9), bg=BG, fg=SUBTEXT,
                                  justify="left", wraplength=340)
        self.log_label.pack(side="bottom", anchor="w", pady=(8, 0))

    # ---------- 操作 ----------
    def _save_settings(self):
        try:
            self.core.save_settings(
                self.auto_mount_var.get(), self.auto_start_var.get())
        except Exception as e:
            self.log_label.config(text=f"设置保存失败: {e}")
            # 回滚界面到服务端权威配置，避免复选框假装已生效。
            self.auto_mount_var.set(self.core.config.get("auto_mount", True))
            self.auto_start_var.set(self.core.config.get("auto_start", False))

    def _mount(self):
        self._run_async(lambda: self.core.mount())

    def _unmount(self):
        self._run_async(lambda: self.core.unmount())

    def _open_finder(self):
        self._run_async(lambda: self.core.open_in_finder())

    def _open_official(self):
        subprocess.Popen(["open", "-a", "小米智能存储"])

    def _run_async(self, fn):
        def w():
            fn()
            self.after(0, self.refresh)
        threading.Thread(target=w, daemon=True).start()

    # ---------- 刷新 ----------
    def _tick(self):
        self.refresh()
        self.after(2000, self._tick)

    def refresh(self):
        st = self.core.conn.snapshot()
        mounted = self.core.is_mounted()
        # 状态点：Finder 已挂载即视为 NAS 可达（代理运行中），即使快路径探测暂时失败
        if st["connected"] or mounted:
            color = GREEN
            self.status_label.config(text="在线" if st["connected"] else "已挂载（代理运行中）")
        else:
            color = RED if st["error"] else GRAY
            self.status_label.config(text="离线")
        self.status_dot.itemconfig(self.dot, fill=color)

        # 连接信息
        if st["connected"]:
            mode = "P2P 隧道（跨网络访问）" if st["mode"] == "p2p" else "局域网直连"
            self.conn_text.config(text=f"已连接 · {mode}\nWebDAV 隧道端口 {st['webdav_port']}")
            self.open_official_btn.pack_forget()
        elif mounted:
            # Finder 已挂载：连接由运行中的本地代理维持，探测失败不误导为离线
            self.conn_text.config(text="已挂载（代理运行中）\nNAS 可达，可正常访问")
            self.open_official_btn.pack_forget()
        else:
            msg = st["error"] or "未连接"
            self.conn_text.config(text=msg)
            self.open_official_btn.pack(anchor="w", pady=(8, 0))

        # 存储
        storage = st.get("storage") or []
        if storage:
            total = sum(s.get("total", 0) for s in storage)
            used = sum(s.get("used", 0) for s in storage)
            pct = (used / total * 100) if total else 0
            self.storage_bar["maximum"] = 100
            self.storage_bar["value"] = pct
            self.storage_text.config(text=f"已用 {human_size(used)} / 共 {human_size(total)}（{pct:.1f}%）")
        else:
            self.storage_bar["value"] = 0
            self.storage_text.config(text="存储信息获取失败（不影响挂载）")

        # 挂载状态
        mounted = self.core.is_mounted()
        mp = self.core.mount_path
        if mounted:
            self.mount_text.config(text=f"已挂载\n{mp}\n可在 Finder 侧边栏直接访问", fg=GREEN)
            self.mount_btn.config(state="disabled")
            self.unmount_btn.config(state="normal")
            self.open_finder_btn.config(state="normal")
        else:
            self.mount_text.config(text="未挂载", fg=SUBTEXT)
            self.mount_btn.config(state="normal")
            self.unmount_btn.config(state="disabled")
            self.open_finder_btn.config(state="disabled")

        # 日志
        if mounted and "失败" in (self.core.status_text or ""):
            self.log_label.config(text="已挂载，可在 Finder 中正常访问")
        else:
            self.log_label.config(text=self.core.status_text)


def _ensure_service_socket_path():
    """连接常驻服务；socket 缺失时幂等拉起独立服务进程（凭证不进 argv），返回 socket 路径。"""
    from service import default_socket_path
    path = default_socket_path()
    try:
        from client import IpcClient
        c = IpcClient(path)
        c.connect()
        c.close()
        return path
    except (FileNotFoundError, ConnectionError, OSError):
        pass
    # 拉起常驻服务（独立会话，凭证仅经配置/状态文件，不进命令行）
    subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "main.py"), "--service"],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import time
    from client import IpcClient
    for _ in range(30):
        try:
            c = IpcClient(path)
            c.connect()
            c.close()
            return path
        except (FileNotFoundError, ConnectionError, OSError):
            time.sleep(0.3)
    raise RuntimeError("常驻服务未在预期时间内就绪")


def _connect_soft(client):
    """初始连接失败受控降级：不向启动栈抛 traceback；只读由后台泵自动重连，
    写命令在执行时明确报“结果未知/未连接”。"""
    try:
        client.connect()
    except (FileNotFoundError, ConnectionError, OSError) as e:
        print(f"警告：初始未连接到服务（稍后自动重试）: {e}", file=sys.stderr)
    return client


def main():
    if "--service" in sys.argv:
        from service import ResidentService
        raise SystemExit(ResidentService().serve())
    socket_path = _ensure_service_socket_path()
    from client import IpcClient
    # 复用探针已确认的 socket；两条连接初始失败均受控降级，不致 startup traceback。
    status_c = _connect_soft(IpcClient(socket_path))
    cmd_c = _connect_soft(IpcClient(socket_path))
    app = App(_ServiceCore(status_c, cmd_c))
    app.mainloop()
    # GUI 关闭仅断开连接；常驻服务继续发现/自愈，不调用 stop。


if __name__ == "__main__":
    main()
