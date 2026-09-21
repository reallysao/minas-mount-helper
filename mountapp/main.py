#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米智能存储 Finder 挂载助手 - 图形界面（含 NAS 文件夹同步）"""
import os
import sys
import time
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import tkinter as tk
from tkinter import ttk, filedialog

from app_core import AppCore
from sync_engine import SyncEngine, SyncPlan, human_size, NAS_TARGET_OPTIONS, notify

ORANGE = "#ff6900"
GREEN = "#2ecc71"
RED = "#e74c3c"
GRAY = "#8e8e93"
BG = "#f5f5f7"
CARD = "#ffffff"
TEXT = "#1d1d1f"
SUBTEXT = "#6e6e73"
BLUE = "#0a84ff"


class PlanDialog(tk.Toplevel):
    """添加 / 编辑同步计划表单"""
    def __init__(self, master, engine, plan=None):
        super().__init__(master)
        self.engine = engine
        self.plan = plan
        self.title("编辑同步计划" if plan else "添加同步计划")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.font = master.font
        self.font_small = master.font_small
        self.font_bold = master.font_bold
        self._build()
        self.grab_set()
        self.after(100, self._center)

    def _center(self):
        try:
            x = self.master.winfo_rootx() + 60
            y = self.master.winfo_rooty() + 60
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _row(self, parent, label, col=0):
        tk.Label(parent, text=label, font=self.font_small, bg=CARD, fg=TEXT).grid(
            row=col, column=0, sticky="w", pady=(8, 2))
        box = tk.Frame(parent, bg=CARD)
        box.grid(row=col, column=1, sticky="ew", pady=(8, 2))
        parent.columnconfigure(1, weight=1)
        return box

    def _build(self):
        f = tk.Frame(self, bg=CARD, padx=18, pady=16)
        f.pack(fill="both", expand=True)

        p = self.plan
        # 计划名
        box = self._row(f, "计划名称")
        self.name_var = tk.StringVar(value=p.name if p else "")
        tk.Entry(box, textvariable=self.name_var, font=self.font, relief="flat",
                 highlightthickness=1, highlightbackground="#d1d1d6",
                 highlightcolor=ORANGE).pack(fill="x")

        # 源文件夹
        box = self._row(f, "源文件夹（本机）")
        srow = tk.Frame(box, bg=CARD)
        srow.pack(fill="x")
        self.src_var = tk.StringVar(value=p.source if p else os.path.expanduser("~/Desktop"))
        tk.Entry(srow, textvariable=self.src_var, font=self.font_small, relief="flat",
                 highlightthickness=1, highlightbackground="#d1d1d6",
                 highlightcolor=ORANGE).pack(side="left", fill="x", expand=True)
        tk.Button(srow, text="浏览…", font=self.font_small, bg="#e5e5ea", fg=TEXT,
                  relief="flat", bd=0, padx=10, pady=3, activebackground="#d1d1d6",
                  command=self._browse_src).pack(side="left", padx=(6, 0))

        # 目标位置（相对 NAS）
        box = self._row(f, "同步到（NAS）")
        tro = tk.Frame(box, bg=CARD)
        tro.pack(fill="x")
        self.dest_var = tk.StringVar()
        dests = [o[1] for o in NAS_TARGET_OPTIONS if o[1] != "__custom__"]
        self.dest_cb = ttk.Combobox(tro, textvariable=self.dest_var, values=dests,
                                    font=self.font_small, state="readonly")
        self.dest_cb.pack(side="left", fill="x", expand=True)
        self.dest_cb.bind("<<ComboboxSelected>>", self._dest_changed)
        self.dest_entry = tk.Entry(tro, font=self.font_small, relief="flat",
                                   highlightthickness=1, highlightbackground="#d1d1d6",
                                   highlightcolor=ORANGE)
        # 解析已有 dest_rel：第一段为根目录选项
        if p and p.dest_rel:
            parts = p.dest_rel.split("/")
            base = parts[0]
            if base in dests:
                self.dest_var.set(base)
                rest = "/".join(parts[1:])
            else:
                self.dest_var.set("自定义…")
                self.dest_entry.pack(side="left", fill="x", expand=True)
                rest = p.dest_rel
        else:
            self.dest_var.set("我的文档")
            rest = ""
        self.dest_sub_var = tk.StringVar(value=rest)
        self.dest_entry.configure(textvariable=self.dest_sub_var)
        tk.Label(box, text="子文件夹（留空则直接放根目录）", font=("PingFang SC", 9),
                 bg=CARD, fg=SUBTEXT).pack(anchor="w", pady=(4, 0))

        # 模式
        box = self._row(f, "同步模式")
        self.mode_var = tk.StringVar(value=p.mode if p else "mirror")
        for val, txt in (("mirror", "镜像（本机删除 → NAS 同步删除）"),
                         ("backup", "备份（传完删除本机已备份文件）")):
            tk.Radiobutton(box, text=txt, value=val, variable=self.mode_var,
                           font=("PingFang SC", 10), bg=CARD, fg=TEXT,
                           activebackground=CARD, selectcolor=CARD,
                           anchor="w").pack(anchor="w")

        # 调度
        box = self._row(f, "触发方式")
        self.sched_var = tk.StringVar(value=p.schedule if p else "manual")
        for val, txt in (("manual", "手动（点击“立即同步”）"),
                         ("interval", "定时（每隔指定分钟）"),
                         ("watch", "文件变化（源文件夹内容变化时）")):
            tk.Radiobutton(box, text=txt, value=val, variable=self.sched_var,
                           font=("PingFang SC", 10), bg=CARD, fg=TEXT,
                           activebackground=CARD, selectcolor=CARD,
                           anchor="w").pack(anchor="w")
        icol = tk.Frame(box, bg=CARD)
        icol.pack(anchor="w", pady=(6, 0))
        tk.Label(icol, text="间隔（分钟）", font=("PingFang SC", 10), bg=CARD,
                 fg=SUBTEXT).pack(side="left")
        self.interval_var = tk.StringVar(value=str(p.interval_min if p else 60))
        tk.Spinbox(icol, from_=1, to=10080, textvariable=self.interval_var,
                   width=6, font=self.font_small, relief="flat",
                   highlightthickness=1, highlightbackground="#d1d1d6").pack(
                       side="left", padx=(8, 0))

        # 按钮
        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", padx=18, pady=(0, 14))
        tk.Button(btns, text="取消", font=self.font_small, bg="#e5e5ea", fg=TEXT,
                  relief="flat", bd=0, padx=18, pady=6, activebackground="#d1d1d6",
                  command=self.destroy).pack(side="right")
        tk.Button(btns, text="保存", font=self.font_small, bg=ORANGE, fg="white",
                  relief="flat", bd=0, padx=18, pady=6, activebackground="#e05e00",
                  command=self._save).pack(side="right", padx=(0, 10))

    def _browse_src(self):
        d = filedialog.askdirectory(title="选择要同步的文件夹",
                                    initialdir=os.path.expanduser("~"))
        if d:
            self.src_var.set(d)

    def _dest_changed(self, _=None):
        if self.dest_var.get() == "自定义…":
            self.dest_entry.pack(side="left", fill="x", expand=True)
            self.dest_sub_var.set("我的文档/Mac同步/桌面")
        else:
            self.dest_entry.pack_forget()

    def _save(self):
        name = self.name_var.get().strip()
        src = self.src_var.get().strip()
        if not name or not src:
            return
        base = self.dest_var.get()
        sub = self.dest_sub_var.get().strip().strip("/")
        if base == "自定义…":
            dest_rel = sub
        else:
            dest_rel = "/".join(x for x in (base, sub) if x)
        data = dict(
            name=name, source=os.path.expanduser(src), dest_rel=dest_rel,
            mode=self.mode_var.get(), schedule=self.sched_var.get(),
            interval_min=int(self.interval_var.get() or 60), enabled=True,
        )
        if self.plan:
            self.engine.update_plan(self.plan.id, **data)
        else:
            self.engine.add_plan(**data)
        self.destroy()


class SyncPanel(tk.Toplevel):
    """同步面板：计划列表 + 进度条 + 通知"""
    def __init__(self, master, engine):
        super().__init__(master)
        self.master_app = master
        self.engine = engine
        self.title("NAS 文件夹同步")
        self.geometry("440x560")
        self.configure(bg=BG)
        self.font = master.font
        self.font_small = master.font_small
        self.font_bold = master.font_bold
        self.cards = {}  # plan_id -> dict(widgets)
        self._build()
        self._tick()

    def _build(self):
        container = tk.Frame(self, bg=BG, padx=16, pady=14)
        container.pack(fill="both", expand=True)

        head = tk.Frame(container, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="文件夹同步", font=self.font_bold, bg=BG, fg=TEXT).pack(side="left")
        tk.Label(head, text="将本机文件夹同步到小米智能存储", font=self.font_small,
                 bg=BG, fg=SUBTEXT).pack(side="right")

        # 计划列表（滚动）
        listwrap = tk.Frame(container, bg=BG)
        listwrap.pack(fill="both", expand=True, pady=(10, 6))
        self.canvas = tk.Canvas(listwrap, bg=BG, highlightthickness=0)
        self.scroll = ttk.Scrollbar(listwrap, orient="vertical", command=self.canvas.yview)
        self.plans_frame = tk.Frame(self.canvas, bg=BG)
        self.plans_frame.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.plans_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        # 底部
        foot = tk.Frame(container, bg=BG)
        foot.pack(fill="x", pady=(4, 0))
        tk.Button(foot, text="＋ 添加同步计划", font=self.font_small, bg=ORANGE, fg="white",
                  relief="flat", bd=0, padx=14, pady=6, activebackground="#e05e00",
                  command=self._add_plan).pack(side="left")
        tk.Label(foot, text="提示：跨网络（WebDAV 隧道）传输速度有限，\n大文件夹首次同步需要一些时间",
                 font=("PingFang SC", 9), bg=BG, fg=SUBTEXT, justify="left").pack(
                     side="right")

    # ---------- 计划卡片 ----------
    def _rebuild(self):
        for w in self.plans_frame.winfo_children():
            w.destroy()
        self.cards = {}
        if not self.engine.plans:
            tk.Label(self.plans_frame, text="还没有同步计划\n点击「＋ 添加同步计划」创建",
                     font=self.font_small, bg=BG, fg=SUBTEXT, justify="center").pack(
                         pady=30)
            return
        for p in self.engine.plans:
            self._plan_card(p)

    def _plan_card(self, p):
        card = tk.Frame(self.plans_frame, bg=CARD, padx=12, pady=10)
        card.pack(fill="x", pady=(0, 8))

        top = tk.Frame(card, bg=CARD)
        top.pack(fill="x")
        tk.Label(top, text=p.name, font=self.font_bold, bg=CARD, fg=TEXT).pack(side="left")
        mode_txt = "镜像" if p.mode == "mirror" else "备份"
        sched_txt = {"manual": "手动", "interval": f"每 {p.interval_min} 分钟",
                     "watch": "文件变化"}.get(p.schedule, "手动")
        tk.Label(top, text=f"{mode_txt} · {sched_txt}", font=("PingFang SC", 9),
                 bg=CARD, fg=SUBTEXT).pack(side="right")

        tk.Label(card, text=f"{p.source}  →  /{p.dest_rel}",
                 font=("PingFang SC", 9), bg=CARD, fg=SUBTEXT, wraplength=390,
                 justify="left").pack(anchor="w", pady=(4, 0))

        # 进度条 + 状态
        bar = ttk.Progressbar(card, maximum=100, mode="determinate")
        bar.pack(fill="x", pady=(8, 2))
        status = tk.Label(card, text=p.last_status or "等待同步", font=("PingFang SC", 9),
                          bg=CARD, fg=SUBTEXT, anchor="w")
        status.pack(fill="x")

        ops = tk.Frame(card, bg=CARD)
        ops.pack(fill="x", pady=(6, 0))
        tk.Button(ops, text="立即同步", font=("PingFang SC", 10), bg=ORANGE, fg="white",
                  relief="flat", bd=0, padx=12, pady=4, activebackground="#e05e00",
                  command=lambda pid=p.id: self._start(pid)).pack(side="left")
        tk.Button(ops, text="编辑", font=("PingFang SC", 10), bg="#e5e5ea", fg=TEXT,
                  relief="flat", bd=0, padx=12, pady=4, activebackground="#d1d1d6",
                  command=lambda pid=p.id: self._edit(pid)).pack(side="left", padx=(6, 0))
        tk.Button(ops, text="删除", font=("PingFang SC", 10), bg="#e5e5ea", fg=RED,
                  relief="flat", bd=0, padx=12, pady=4, activebackground="#d1d1d6",
                  command=lambda pid=p.id: self._delete(pid)).pack(side="left", padx=(6, 0))
        enable_var = tk.BooleanVar(value=p.enabled)
        tk.Checkbutton(ops, text="启用", variable=enable_var, font=("PingFang SC", 10),
                       bg=CARD, fg=TEXT, activebackground=CARD, selectcolor=CARD,
                       command=lambda pid=p.id, v=enable_var: self._toggle(pid, v.get())
                       ).pack(side="right")

        self.cards[p.id] = {"bar": bar, "status": status, "enable": enable_var}

    # ---------- 操作 ----------
    def _add_plan(self):
        PlanDialog(self, self.engine)
        self.after(200, self._rebuild)

    def _edit(self, pid):
        p = self.engine.get_plan(pid)
        if p:
            PlanDialog(self, self.engine, plan=p)
            self.after(200, self._rebuild)

    def _delete(self, pid):
        p = self.engine.get_plan(pid)
        if not p:
            return
        if tk.messagebox.askyesno("删除计划", f"确定删除同步计划「{p.name}」？\n（NAS 上已同步的文件不会被删除）",
                                  parent=self):
            self.engine.remove_plan(pid)
            self._rebuild()

    def _toggle(self, pid, val):
        self.engine.update_plan(pid, enabled=val)

    def _start(self, pid):
        p = self.engine.get_plan(pid)
        if not p:
            return
        ok, msg = self.engine.start_sync(pid, reason="手动")
        if not ok:
            notify("同步", msg)

    # ---------- 状态刷新 ----------
    def _tick(self):
        try:
            self.engine.tick()
            for pid, w in self.cards.items():
                pr = self.engine.get_progress(pid)
                p = self.engine.get_plan(pid)
                if not p:
                    continue
                bar, status = w["bar"], w["status"]
                if pr:
                    status.config(text=f"{pr.get('status','')}  {pr.get('extra','')}"
                                       f"{'  ' + str(pr.get('done',0)) + '/' + str(pr.get('total',0)) if pr.get('total') else ''}",
                                  fg=TEXT if pr.get('status') in ("同步中…", "扫描文件…", "检查挂载…") else SUBTEXT)
                    bar["value"] = pr.get("pct", 0)
                    if pr.get("status") in ("完成", "同步失败") or pr.get("pct") == 100:
                        bar["value"] = 100
                else:
                    st = p.last_status or "等待同步"
                    status.config(text=st, fg=RED if st.startswith(("失败", "无法", "无")) else SUBTEXT)
                    bar["value"] = 0
        except Exception:
            pass
        if self.winfo_exists():
            self.after(1000, self._tick)


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

        self.sync = SyncEngine(core)
        self.sync_panel = None
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

        # 同步卡片
        sync_card = tk.Frame(container, bg=CARD, padx=14, pady=12)
        sync_card.pack(fill="x", pady=(0, 10))
        tk.Label(sync_card, text="文件夹同步", font=self.font_bold, bg=CARD, fg=TEXT).pack(anchor="w")
        self.sync_text = tk.Label(sync_card, text="将桌面/文稿/下载等自动同步到 NAS",
                                  font=self.font_small, bg=CARD, fg=SUBTEXT, justify="left",
                                  wraplength=310)
        self.sync_text.pack(anchor="w", pady=(6, 0))
        tk.Button(sync_card, text="打开同步面板", font=self.font_small, bg=ORANGE, fg="white",
                  relief="flat", bd=0, padx=14, pady=5, activebackground="#e05e00",
                  command=self._open_sync_panel).pack(anchor="w", pady=(8, 0))

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

    # ---------- 同步面板 ----------
    def _open_sync_panel(self):
        if self.sync_panel and self.sync_panel.winfo_exists():
            self.sync_panel.lift()
            return
        self.sync_panel = SyncPanel(self, self.sync)
        self.sync_panel.after(300, self.sync_panel._rebuild)

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
