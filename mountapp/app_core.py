#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
应用核心：编排 连接发现 -> 代理 -> Finder 挂载 -> 自动重连
"""
import os
import sys
import time
import json
import shutil
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import conn_manager as cm
import mwdav_proxy as mp
from mwdav_proxy import is_port_in_use

APP_SUPPORT = os.path.expanduser("~/Library/Application Support/小米智能存储挂载助手")
CONFIG_PATH = os.path.join(APP_SUPPORT, "config.json")
NETFS_MOUNT = os.path.join(BASE_DIR, "netfs_mount")
PROXY_PORT = 18445
MOUNT_URL = f"http://127.0.0.1:{PROXY_PORT}/"

DEFAULT_CONFIG = {
    "auto_mount": True,       # 连接后自动挂载
    "auto_start": False,      # 登录时自动启动
    "mount_dir": "",          # 空 = 挂载到 /Volumes（Finder 侧边栏可见）
}


class AppCore:
    def __init__(self):
        self.conn = cm.ConnManager()
        self.conn.exclude_port = PROXY_PORT
        self.proxy_proc = None
        self.config = self._load_config()
        self.lock = threading.Lock()
        self.mounted = False
        self.mount_path = None
        self.status_text = "初始化中…"
        self.refresh_interval = 20
        self._stop = threading.Event()
        self._thread = None
        self._dead_streak = 0  # 连续探活失败次数（用于卸载死挂载）

    # ---------- 配置 ----------
    def _load_config(self):
        try:
            os.makedirs(APP_SUPPORT, exist_ok=True)
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                for k in DEFAULT_CONFIG:
                    cfg.setdefault(k, DEFAULT_CONFIG[k])
                return cfg
        except Exception:
            pass
        return dict(DEFAULT_CONFIG)

    def save_config(self):
        try:
            os.makedirs(APP_SUPPORT, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 连接 ----------
    def refresh_connection(self, force=False):
        return self.conn.refresh(force=force)

    # ---------- 代理（独立子进程，随 App 退出仍存活，避免已挂载卷失效） ----------
    @staticmethod
    def _proxy_pid_on(port):
        """返回占用端口的进程 PID（任意）"""
        try:
            r = subprocess.run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"],
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
        """确保本地代理进程在跑（复用已有 mwdav_proxy 进程，否则拉起新进程）"""
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
            if is_port_in_use(PROXY_PORT):
                return True
            time.sleep(0.3)
        self.status_text = "代理进程已启动但端口未就绪，请稍候"
        return False

    def update_proxy_target(self):
        st = self.conn.snapshot()
        if st["connected"] and st["webdav_port"] and st["username"] and st["password"]:
            mp.UP.update(port=st["webdav_port"], username=st["username"],
                         password=st["password"], connected=True)
            return True
        mp.UP.update(connected=False)
        return False

    # ---------- 挂载 ----------
    def find_mount_path(self):
        """WebDAV 挂载点通常为 /Volumes/127.0.0.1"""
        if self.config.get("mount_dir"):
            return self.config["mount_dir"]
        for name in os.listdir("/Volumes"):
            p = os.path.join("/Volumes", name)
            if os.path.ismount(p):
                out = subprocess.run(["/sbin/mount"], capture_output=True, text=True).stdout or ""
                for line in out.splitlines():
                    if p in line and "webdav" in line:
                        return p
        return None

    def is_mounted(self):
        p = self.find_mount_path()
        if p:
            self.mount_path = p
            return True
        self.mount_path = None
        return False

    def mount(self):
        """执行挂载（NetFS，带凭证，无弹窗）"""
        st = self.conn.snapshot()
        if not (st["connected"] and st["webdav_port"] and st["username"] and st["password"]):
            self.status_text = "无法挂载：连接或凭证不可用"
            return False, "连接不可用"
        if not self.update_proxy_target():
            return False, "代理目标未更新"
        if not self.ensure_proxy():
            return False, "代理未启动"
        if self.is_mounted():
            self.status_text = "已挂载"
            return True, "已挂载"
        # 若存在残留挂载点目录（上次异常退出遗留），先清理
        if os.path.exists("/Volumes/127.0.0.1") and not self.find_mount_path():
            self.unmount()
        cmd = [NETFS_MOUNT, "mount", MOUNT_URL, st["username"], st["password"]]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            out = (r.stdout or "") + (r.stderr or "")
            if "MOUNT_OK" in out:
                self.mounted = True
                self.status_text = "挂载成功"
                return True, "挂载成功"
            # err=16/17（已挂载/资源忙）→ 再次确认挂载表，视为已挂载
            if "MOUNT_FAIL" in out and any(e in out for e in ("err=16", "err=17")):
                p = self.find_mount_path()
                if p:
                    self.mounted = True
                    self.mount_path = p
                    self.status_text = "已挂载（复用现有挂载）"
                    return True, "已挂载"
            self.status_text = f"挂载失败: {out.strip()[:100]}"
            return False, out.strip()[:200]
        except Exception as e:
            self.status_text = f"挂载失败: {e}"
            return False, str(e)

    def unmount(self):
        p = self.find_mount_path()
        if not p:
            self.mounted = False
            self.status_text = "未挂载"
            return True, "未挂载"
        r = subprocess.run([NETFS_MOUNT, "unmount", p], capture_output=True, text=True, timeout=30)
        out = (r.stdout or "") + (r.stderr or "")
        ok = "UNMOUNT_OK" in out
        if ok:
            self.mounted = False
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

    # ---------- 挂载健康自愈 ----------
    def proxy_alive(self, timeout=3):
        """通过本地代理探测上游：代理 200/207 说明隧道+凭证正常。
        返回 True=正常，False=上游不可达（挂载将失效）"""
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", str(timeout), "-u",
                 "%s:%s" % (mp.UP.snapshot()["username"], mp.UP.snapshot()["password"]),
                 "-X", "OPTIONS", "-o", "/dev/null", "-w", "%{http_code}", MOUNT_URL],
                capture_output=True, text=True, timeout=timeout + 2)
            code = (r.stdout or "").strip()
            return code in ("200", "207")
        except Exception:
            return False

    def ensure_mount_health(self):
        """自愈：挂载存在但上游已死（连续 N 次）→ 卸载清理死挂载（消除 macOS
        「重新连接」弹窗）；上游恢复且未挂载 → 自动重新挂载。
        凭证未就绪时跳过（避免应用启动瞬间误判误卸）。"""
        up = mp.UP.snapshot()
        if not (up["username"] and up["password"]):
            return
        mounted = self.is_mounted()
        alive = self.proxy_alive()
        if alive:
            self._dead_streak = 0
            if not mounted and self.config.get("auto_mount") and self.conn.snapshot()["connected"]:
                self.mount()
            return
        # 上游不可达
        self._dead_streak += 1
        if mounted and self._dead_streak >= 2:
            self.status_text = "隧道波动，已清理失效挂载，恢复后自动重连"
            self.unmount()
            self._dead_streak = 0

    # ---------- 后台循环 ----------
    def _loop(self):
        while not self._stop.is_set():
            try:
                st = self.refresh_connection()
                if st["connected"]:
                    self.ensure_proxy()
                    self.update_proxy_target()
                    if not self.status_text or "初始化" in self.status_text:
                        self.status_text = "已就绪"
                else:
                    mp.UP.update(connected=False)
                # 挂载自愈：上游死则清理失效挂载（消除 macOS 弹窗），恢复则自动重挂
                self.ensure_mount_health()
            except Exception as e:
                self.status_text = f"后台错误: {e}"
            self._stop.wait(self.refresh_interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ---------- 系统集成 ----------
    def set_auto_start(self, enabled):
        """配置登录自启动（LaunchAgent）"""
        self.config["auto_start"] = enabled
        self.save_config()
        agent_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(agent_dir, exist_ok=True)
        plist = os.path.join(agent_dir, "com.minas.mount.plist")
        if not enabled:
            subprocess.run(["launchctl", "unload", plist], capture_output=True)
            try:
                os.remove(plist)
            except Exception:
                pass
            return
        launcher = os.path.join(BASE_DIR, "launcher.sh")
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.minas.mount</string>
  <key>ProgramArguments</key>
  <array><string>{launcher}</string><string>--hidden</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
</dict></plist>
"""
        with open(plist, "w", encoding="utf-8") as f:
            f.write(content)
        subprocess.run(["launchctl", "unload", plist], capture_output=True)
        subprocess.run(["launchctl", "load", plist], capture_output=True)


if __name__ == "__main__":
    core = AppCore()
    core.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        core.stop()
