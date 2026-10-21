#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · ResidentService 注入 fakes 与 IPC 客户端（审查者独占，非 owner 交付物）。

纪律：全部内存 fake + 本地回环；不连真实 NAS、不挂载、不碰真实路径/二进制、
不用真实凭证；仅经构造参数注入，生产 IPC 不提供 test-mode。
"""
import importlib.util
import json
import os
import socket
import sys
import threading
from pathlib import Path

# 让 service.py 的 `import conn_manager / ipc / state_store` 可解析（仅测试进程内）
_REPO = Path(__file__).resolve().parent.parent
_MOUNTAPP = _REPO / "mountapp"
if str(_MOUNTAPP) not in sys.path:
    sys.path.insert(0, str(_MOUNTAPP))

FIXTURE_USER = "fixture-user"
FIXTURE_PASSWORD = "fixture-password-not-a-real-credential-0000000000"

# 加载生产 service 模块以构造真实 DiscoveryView dataclass（不用 SimpleNamespace）
_svc_spec = importlib.util.spec_from_file_location("acc_service_mod", _MOUNTAPP / "service.py")
_svc_mod = importlib.util.module_from_spec(_svc_spec)
_svc_spec.loader.exec_module(_svc_mod)


# ------------------------- Discovery backend -------------------------

class ScriptedDiscovery:
    """脚本化 DiscoveryBackend：refresh 按队列返回，poll_storage 返回固定存储。"""

    def __init__(self, views, storage=None):
        self._views = list(views)
        self._idx = 0
        self.storage = storage or [{"total": 100, "used": 30}]
        self.calls = 0

    def refresh(self, force=False):
        self.calls += 1
        v = self._views[min(self._idx, len(self._views) - 1)]
        self._idx += 1
        return v

    def poll_storage(self):
        return self.storage


def view(connected, port, mode="p2p", error=None, storage=None):
    """构造真实 service.DiscoveryView dataclass（字段对齐 §8.5）。"""
    return _svc_mod.DiscoveryView(
        connected=connected, mode=mode, webdav_port=port,
        username=FIXTURE_USER if connected else "",
        password=FIXTURE_PASSWORD if connected else "",
        storage=storage or [{"total": 100, "used": 30}],
        error=error or "")


# ------------------------- Mount backend -------------------------

class RecordingMount:
    """内存 MountBackend：记录 mount/unmount/own_mountpoint/open_finder 意图。"""

    def __init__(self):
        self.mounts = []
        self.unmounts = []
        self.opened = []
        self._mounted = None

    def ensure_capable(self):
        return True

    def mount(self, url, mountpoint, username, password):
        self.mounts.append({"url": url, "mountpoint": mountpoint,
                            "user": username, "has_pwd": bool(password)})
        self._mounted = mountpoint
        return True, ""

    def unmount(self, mountpoint):
        self.unmounts.append(mountpoint)
        self._mounted = None
        return True, ""

    def own_mountpoint(self, expected):
        return self._mounted

    def open_finder(self):
        self.opened.append(self._mounted)
        return (True, self._mounted) if self._mounted else (False, "未挂载")


# ------------------------- Proxy factory product -------------------------

class RecordingProxy:
    """实现 §8.6 ProxyLike：start/stop/is_running/adopt；记录采纳序列。"""

    def __init__(self, port):
        self.port = port
        self.adopted = []
        self.started = 0
        self.stopped = 0
        self._running = False

    def start(self):
        self.started += 1
        self._running = True
        return self.port

    def stop(self, timeout=5.0):
        self.stopped += 1
        self._running = False

    def is_running(self):
        return self._running

    def adopt(self, snap):
        self.adopted.append({
            "host": getattr(snap, "host", None),
            "port": getattr(snap, "port", None),
            "generation": getattr(snap, "generation", None),
        })
        return True


# ------------------------- IPC 客户端 -------------------------

class IpcClient:
    """单行 JSON Unix socket 客户端（测试侧）。"""

    def __init__(self, socket_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(socket_path)
        self.sock.settimeout(5)

    def call(self, cmd, args=None, req_id=1):
        req = {"cmd": cmd, "id": req_id, "args": args or {}}
        self.sock.sendall((json.dumps(req) + "\n").encode())
        line = self.sock.recv(65536)
        while b"\n" not in line:
            line += self.sock.recv(65536)
        return json.loads(line.split(b"\n", 1)[0].decode())

    def raw(self, text):
        self.sock.sendall((text + "\n").encode())
        return self.sock.recv(65536).decode()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def wait_until(predicate, timeout=5.0, interval=0.05):
    """轮询直到 predicate() 为真；超时抛 AssertionError。"""
    deadline = threading.Event()
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError("wait_until 超时：条件未达成")
