#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
受保护本地 IPC 传输层（runtime owner）。

- 仅本机 Unix domain socket；目录 0700、socket 文件 0600；不监听 TCP、无匿名远程面。
- newline-delimited 单行 JSON：请求 {"cmd","id","args"}，响应 {"ok","id","error","data"}。
- 服务端单锁串行执行命令（按到达顺序，无交错写/并发 CAS）；只读 status 不触发写。
- 协议层不认识任何凭证；脱敏由服务端 status_view 保证，本层只做传输与结构校验。
"""
import json
import os
import socket
import threading

MAX_LINE = 64 * 1024

# 只读命令：不持全局/写锁，避免被慢写命令（如 NetFS 挂载）阻塞；
# 写命令仍在全局锁内串行执行，杜绝交错写/并发 CAS。
READONLY_CMDS = frozenset({"status", "get_config"})


class IpcProtocolError(Exception):
    """结构非法（非 JSON / 缺 cmd / 类型错 / 超长），零副作用。"""

    def __init__(self, message, code="bad_request"):
        super().__init__(message)
        self.code = code


class IpcServer:
    """串行化命令的 Unix socket 服务端。

    dispatcher(cmd: str, args: dict) -> dict：由 ResidentService 提供并返回脱敏 data。
    """

    def __init__(self, socket_path, dispatcher, *, dir_mode=0o700, mode=0o600):
        self.socket_path = socket_path
        self._dispatch = dispatcher
        self._dir_mode = dir_mode
        self._mode = mode
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------
    def start(self):
        directory = os.path.dirname(self.socket_path)
        os.makedirs(directory, exist_ok=True)
        os.chmod(directory, self._dir_mode)
        # 清理陈旧 socket 文件（存活持锁者由单实例锁另行保护）
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(self.socket_path)
        os.chmod(self.socket_path, self._mode)
        sock.listen(16)
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(2)
            self._thread = None

    # ---------- 接收 ----------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle_conn, args=(conn,), daemon=True)
            t.start()

    def _handle_conn(self, conn):
        buf = b""
        try:
            conn.settimeout(10)
            while not self._stop.is_set():
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self._serve_line(conn, line)
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _serve_line(self, conn, line):
        request_id = None
        try:
            if len(line) > MAX_LINE:
                raise IpcProtocolError("请求超过长度上限", "too_large")
            try:
                req = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise IpcProtocolError(f"非法 JSON：{e}", "bad_json")
            if not isinstance(req, dict):
                raise IpcProtocolError("请求必须是 JSON 对象", "bad_shape")
            request_id = req.get("id")
            cmd = req.get("cmd")
            args = req.get("args") or {}
            if not isinstance(cmd, str) or not cmd:
                raise IpcProtocolError("缺少字符串命令 cmd", "missing_cmd")
            if not isinstance(args, dict):
                raise IpcProtocolError("args 必须是对象", "bad_args")
            # 只读命令不持全局锁，慢写命令（NetFS 挂载）不阻塞 status/get_config；
            # 写命令仍在全局锁内串行执行，杜绝交错写/并发 CAS。
            if cmd not in READONLY_CMDS:
                with self._lock:
                    data = self._dispatch(cmd, args)
            else:
                data = self._dispatch(cmd, args)
            self._write(conn, {"ok": True, "id": request_id,
                               "error": None, "data": data})
        except IpcProtocolError as e:
            self._write(conn, {"ok": False, "id": request_id,
                               "error": f"[{e.code}] {e}", "data": None})
        except Exception as e:  # 命令执行异常不崩服务，结构化回错
            self._write(conn, {"ok": False, "id": request_id,
                               "error": f"{type(e).__name__}: {e}", "data": None})

    @staticmethod
    def _write(conn, obj):
        try:
            conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
