#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米智能存储 Finder 挂载助手 - 图形界面"""
import os
import sys
import time
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import tkinter as tk
from tkinter import ttk

from app_core import AppCore

ORANGE = "#ff6900"
GREEN = "#2ecc71"
RED = "#e74c3c"
GRAY = "#8e8e93"
BG = "#f5f5f7"
CARD = "#ffffff"
TEXT = "#1d1d1f"
SUBTEXT = "#6e6e73"


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "—"


class App(tk.Tk):
    def __init__(self, core):
        super().__init__()
        self.core = core
        self.title("小米智能存储 Finder 挂载助手")
        self.geometry("380x580")
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
                                           bg=ORANGE, fg="white", relief="flat", bd=0, padx=10, pady=5,
                                           activebackground="#e05e00", activeforeground="white",
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

        # 设置
        settings = tk.Frame(container, bg=BG)
        settings.pack(fill="x", pady=(2, 0))
        self.auto_mount_var = tk.BooleanVar(value=self.core.config.get("auto_mount", True))
        self.auto_start_var = tk.BooleanVar(value=self.core.config.get("auto_start", False))
        tk.Checkbutton(settings, text="连接后自动挂载", variable=self.auto_mount_var, bg=BG, fg=TEXT,
                       font=self.font_small, activebackground=BG, selectcolor=BG,
                       command=self._save_settings).pack(side="left")
        tk.Checkbutton(settings, text="登录时自动启动", variable=self.auto_start_var, bg=BG, fg=TEXT,
                       font=self.font_small, activebackground=BG, selectcolor=BG,
                       command=self._save_settings).pack(side="left", padx=(14, 0))

        # 底部状态
        self.log_label = tk.Label(container, text="", font=("PingFang SC", 9), bg=BG, fg=SUBTEXT,
                                  justify="left", wraplength=340)
        self.log_label.pack(side="bottom", anchor="w", pady=(8, 0))

    # ---------- 操作 ----------
    def _save_settings(self):
        self.core.config["auto_mount"] = self.auto_mount_var.get()
        self.core.set_auto_start(self.auto_start_var.get())
        self.core.save_config()

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
        # 状态点
        color = GREEN if st["connected"] else (RED if st["error"] else GRAY)
        self.status_dot.itemconfig(self.dot, fill=color)
        self.status_label.config(text="在线" if st["connected"] else "离线")

        # 连接信息
        if st["connected"]:
            mode = "P2P 隧道（跨网络访问）" if st["mode"] == "p2p" else "局域网直连"
            self.conn_text.config(text=f"已连接 · {mode}\nWebDAV 隧道端口 {st['webdav_port']}")
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
            self.storage_text.config(text="存储信息不可用（不影响挂载）")

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


def main():
    core = AppCore()
    app = App(core)
    app.mainloop()
    core.stop()


if __name__ == "__main__":
    main()
