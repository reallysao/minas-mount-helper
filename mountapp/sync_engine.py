#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步引擎：将本地文件夹（桌面/文稿/下载等）按计划同步到小米智能存储。

- 多同步计划（source -> NAS 目标，目标默认在「我的文档」下，可自定义）
- 模式：mirror（镜像，本地删除则 NAS 同步删除）/ backup（备份，成功后删除本地已备份文件）
- 调度：manual（手动）/ interval（定时，分钟）/ watch（源目录变化自动触发）
- 进度：dry-run 统计需传输文件数 -> rsync -i 逐行解析 -> 进度百分比 + 速度
- 完成/失败：macOS 通知中心提醒
"""
import os
import re
import sys
import time
import json
import uuid
import shutil
import threading
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 需要排除的应用/系统数据（不属于用户文件，避免误传大量日志与缓存）
DEFAULT_EXCLUDES = [
    ".DS_Store", ".localized", ".Trash*", ".fseventsd", ".Spotlight-V100",
    ".TemporaryItems", ".background*", ".UTSystemConfig", ".beacon",
    ".policy", ".qimei", ".yk", "Adobe", "Codex", "HSLog", "mijiaNas",
    "XMMacPlayer_CrashLogs", "utmc_store.sqlite", "Microsoft*", "OneDrive*",
]

# NAS 根目录下可选的同步目标位置（相对 NAS 根）
NAS_TARGET_OPTIONS = [
    ("我的文档", "我的文档"),
    ("我的照片", "我的照片"),
    ("根目录", ""),
    ("自定义…", "__custom__"),
]

# openrsync 的 -i 输出为 9~11 字符变更码 + 空格 + 路径（如 ">f+++++++ 文件.txt"）
ITEMIZE_RE = re.compile(r"^(\S+) (.+)$")


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


def notify(title, msg):
    """macOS 通知中心提醒（静默失败）"""
    try:
        script = 'display notification "%s" with title "%s"' % (
            msg.replace('"', "'"), title.replace('"', "'"))
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class SyncPlan:
    def __init__(self, **kw):
        self.id = kw.get("id") or uuid.uuid4().hex[:8]
        self.name = kw.get("name", "未命名计划")
        self.source = kw.get("source", "")
        # dest_rel: 相对 NAS 根目录的路径，如 "我的文档/Mac同步/桌面"
        self.dest_rel = kw.get("dest_rel", "")
        self.mode = kw.get("mode", "mirror")      # mirror / backup
        self.schedule = kw.get("schedule", "manual")  # manual / interval / watch
        self.interval_min = int(kw.get("interval_min", 60) or 60)
        self.enabled = bool(kw.get("enabled", True))
        self.last_run = kw.get("last_run", 0)     # 上次运行时间戳
        self.last_status = kw.get("last_status", "")  # 上次结果
        self._mtime_cache = None                  # watch 用：源目录指纹

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "source": self.source,
            "dest_rel": self.dest_rel, "mode": self.mode,
            "schedule": self.schedule, "interval_min": self.interval_min,
            "enabled": self.enabled, "last_run": self.last_run,
            "last_status": self.last_status,
        }


class SyncEngine:
    """多计划同步调度器（单线程串行执行，避免并发拖垮 WebDAV）"""

    def __init__(self, core):
        self.core = core            # AppCore 实例（提供 mount_path）
        self.lock = threading.Lock()
        self.plans = []
        self.progress = {}          # plan_id -> dict(total, done, deleting, speed, msg)
        self.current = None         # 正在运行的 plan_id
        self._stop = threading.Event()
        self._worker = None
        self.load_plans()

    # ---------- 配置持久化 ----------
    def load_plans(self):
        raw = self.core.config.get("sync_plans") or []
        self.plans = [SyncPlan(**p) for p in raw if isinstance(p, dict)]

    def save_plans(self):
        self.core.config["sync_plans"] = [p.to_dict() for p in self.plans]
        self.core.save_config()

    def get_plan(self, pid):
        for p in self.plans:
            if p.id == pid:
                return p
        return None

    def add_plan(self, **kw):
        p = SyncPlan(**kw)
        with self.lock:
            self.plans.append(p)
        self.save_plans()
        return p

    def update_plan(self, pid, **kw):
        p = self.get_plan(pid)
        if not p:
            return None
        for k, v in kw.items():
            if hasattr(p, k) and k not in ("id",):
                setattr(p, k, v)
        self.save_plans()
        return p

    def remove_plan(self, pid):
        with self.lock:
            self.plans = [p for p in self.plans if p.id != pid]
        self.save_plans()

    # ---------- 路径 ----------
    def nas_root(self):
        return self.core.mount_path or "/Volumes/127.0.0.1"

    def dest_path(self, plan):
        root = self.nas_root()
        rel = (plan.dest_rel or "").strip("/")
        if not rel:
            return root
        return os.path.join(root, *rel.split("/"))

    # ---------- 调度（由 App 每秒 tick 调用） ----------
    def tick(self):
        if self._stop.is_set():
            return
        for p in self.plans:
            if not p.enabled or p.id == self.current:
                continue
            if p.schedule == "interval":
                if time.time() - p.last_run >= p.interval_min * 60:
                    self.start_sync(p.id, reason="定时")
            elif p.schedule == "watch":
                sig = self._dir_sig(p.source)
                if sig is not None:
                    if p._mtime_cache is not None and sig != p._mtime_cache:
                        self.start_sync(p.id, reason="文件变化")
                    p._mtime_cache = sig

    def _dir_sig(self, src):
        """源目录指纹：总文件数 + 最新 mtime（轻量，避免频繁遍历大目录）"""
        try:
            if not os.path.isdir(src):
                return None
            latest = 0.0
            count = 0
            for entry in os.scandir(src):
                count += 1
                try:
                    latest = max(latest, os.stat(entry.path).st_mtime)
                except Exception:
                    pass
            return (count, int(latest))
        except Exception:
            return None

    # ---------- 源目录可读性检查（TCC） ----------
    def check_source_accessible(self, src):
        """返回 (ok, msg)。TCC 拒绝时 listdir 为空或抛异常。"""
        if not os.path.exists(src):
            return False, "源文件夹不存在"
        try:
            items = list(os.scandir(src))
        except PermissionError:
            return False, "无访问权限"
        except Exception as e:
            return False, f"读取失败: {e}"
        # 受保护目录（桌面/文稿/下载）被 TCC 拒绝时 scandir 通常返回空
        if not items and self._dir_looks_nonempty(src):
            return False, "无访问权限：请在「系统设置 → 隐私与安全性 → 完全磁盘访问」中勾选本 App"
        return True, ""

    @staticmethod
    def _dir_looks_nonempty(src):
        # 目录条目数>0 但 scandir 拿不到 -> 大概率 TCC 拦截
        try:
            r = subprocess.run(["ls", "-A", src], capture_output=True, text=True, timeout=5)
            return bool((r.stdout or "").strip())
        except Exception:
            return False

    # ---------- 同步执行 ----------
    def start_sync(self, plan_id, reason="手动"):
        """排队启动一个计划（同一时间只同步一个，其余排队）"""
        with self.lock:
            if self.current == plan_id:
                return False, "该计划正在同步"
            if self.current is not None:
                return False, "已有同步任务在运行，请等待完成"
        threading.Thread(target=self._worker_main, args=(plan_id, reason),
                         daemon=True).start()
        return True, "已开始"

    def _worker_main(self, plan_id, reason):
        with self.lock:
            self.current = plan_id
        plan = self.get_plan(plan_id)
        if not plan:
            with self.lock:
                self.current = None
            return
        try:
            self._run_plan(plan)
        except Exception as e:
            self._set_progress(plan_id, status=f"同步异常: {e}")
            plan.last_status = f"失败: {e}"
        finally:
            plan.last_run = time.time()
            self.save_plans()
            with self.lock:
                self.current = None
            self.progress.pop(plan_id, None)

    def _run_plan(self, plan):
        pid = plan.id
        src = os.path.expanduser(plan.source)
        dest = self.dest_path(plan)
        self._set_progress(pid, status="检查挂载…", total=0, done=0)

        # 1. NAS 挂载检查
        root = self.nas_root()
        if not os.path.isdir(root):
            self._set_progress(pid, status="NAS 未挂载，请先挂载", total=0, done=0)
            plan.last_status = "NAS 未挂载"
            return

        # 2. 源可读性（TCC）
        ok, msg = self.check_source_accessible(src)
        if not ok:
            self._set_progress(pid, status=msg, total=0, done=0)
            plan.last_status = msg
            notify("小米 NAS 同步失败", f"{plan.name}: {msg}")
            return

        # 3. 目标目录
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:
            self._set_progress(pid, status=f"无法创建目标目录: {e}", total=0, done=0)
            plan.last_status = f"目标目录错误: {e}"
            return

        # 4. 统计需要传输的文件数（dry-run）
        self._set_progress(pid, status="扫描文件…", total=0, done=0)
        cmd = self._build_rsync(plan, dry=True)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            total = self._count_transfers(r.stdout)
        except Exception as e:
            total = 0
        if total == 0:
            # 无可传输文件（已是最新）
            self._set_progress(pid, status="已是最新，无需同步", total=0, done=0)
            plan.last_status = f"已是最新（{time.strftime('%H:%M')}）"
            notify("小米 NAS 同步", f"{plan.name}：已是最新")
            return

        # 5. 实际同步 + 逐行解析进度
        self._set_progress(pid, status="同步中…", total=total, done=0,
                           extra=f"共 {total} 项")
        cmd = self._build_rsync(plan, dry=False)
        started = time.time()
        done = 0
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1)
            for line in proc.stdout:
                m = ITEMIZE_RE.match(line.strip())
                if not m:
                    continue
                code, name = m.groups()
                if code.startswith("*deleting"):
                    continue
                if code[1:2] == "f" or code.startswith(">f"):
                    done += 1
                    elapsed = max(time.time() - started, 0.1)
                    speed = done / elapsed  # 文件/秒
                    pct = min(done / total * 100, 100) if total else 0
                    self._set_progress(
                        pid, status="同步中…", total=total, done=done,
                        pct=pct, extra="%s / %s · %d 项/秒" % (
                            human_size(self._dest_size(dest)), human_size(
                                self._estimate_total_bytes(plan)), done))
            proc.wait(timeout=3600 * 12)
            rc = proc.returncode
        except Exception as e:
            rc = 1
            self._set_progress(pid, status=f"同步进程错误: {e}")

        # 6. 结果
        if rc == 0:
            plan.last_status = f"完成（{time.strftime('%H:%M')}，{done} 项）"
            self._set_progress(pid, status="完成", total=total, done=done, pct=100)
            notify("小米 NAS 同步完成", f"{plan.name}：已同步 {done} 项")
        else:
            plan.last_status = f"失败（rsync={rc}，已传 {done} 项）"
            self._set_progress(pid, status="同步失败", total=total, done=done)
            notify("小米 NAS 同步失败", f"{plan.name}：同步失败，已传 {done} 项")

    # ---------- rsync 构建 ----------
    def _build_rsync(self, plan, dry=False):
        cmd = ["rsync", "-a", "--partial", "-i"]
        if dry:
            cmd.append("--dry-run")
        if plan.mode == "mirror":
            cmd.append("--delete")
        elif plan.mode == "backup":
            cmd.append("--remove-source-files")
        for ex in DEFAULT_EXCLUDES:
            cmd.append("--exclude=%s" % ex)
        src = os.path.expanduser(plan.source)
        cmd.append(src.rstrip("/") + "/")
        cmd.append(self.dest_path(plan).rstrip("/") + "/")
        return cmd

    @staticmethod
    def _count_transfers(text):
        """统计 dry-run 输出中的文件传输行（排除删除行）"""
        n = 0
        for line in (text or "").splitlines():
            m = ITEMIZE_RE.match(line.strip())
            if not m:
                continue
            code = m.group(1)
            if code.startswith("*deleting") or code.startswith("cd") or code.startswith(".d"):
                continue
            if "f" in code:
                n += 1
        return n

    @staticmethod
    def _estimate_total_bytes(plan):
        """估算源目录总大小（首次同步显示用，慢目录可降级）"""
        try:
            r = subprocess.run(
                ["du", "-sk", os.path.expanduser(plan.source)],
                capture_output=True, text=True, timeout=30)
            kb = int((r.stdout or "0").split()[0])
            return kb * 1024
        except Exception:
            return 0

    @staticmethod
    def _dest_size(dest):
        """目标目录已传大小（KB），失败返回 0"""
        try:
            r = subprocess.run(["du", "-sk", dest], capture_output=True,
                               text=True, timeout=10)
            return int((r.stdout or "0").split()[0]) * 1024
        except Exception:
            return 0

    # ---------- 进度 ----------
    def _set_progress(self, pid, status, total=0, done=0, pct=0, extra=""):
        with self.lock:
            self.progress[pid] = {
                "status": status, "total": total, "done": done,
                "pct": pct, "extra": extra,
            }

    def get_progress(self, pid):
        with self.lock:
            return dict(self.progress.get(pid, {}))

    # ---------- 清理 ----------
    def stop(self):
        self._stop.set()
