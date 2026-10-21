#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GUI 瘦客户端（runtime owner）：只经受保护 Unix socket 发命令、读脱敏 status。

- 不持有发现/发布/自愈线程；GUI 关闭仅断开连接，不停止服务。
- 不直连后端、不传任意上游目标/凭证；凭证永不经过客户端。
- 共享单实例被 Tk 主线程（status 轮询）与异步 worker（mount/unmount）并发使用：
  call() 整段加锁，按响应 id 精确匹配；传输层错误仅对幂等只读命令自动重试，
  写命令结果未知绝不自动重放，杜绝跨请求响应错配与重复执行。
"""
import json
import os
import socket
import threading


class IpcClientProtocolError(Exception):
    """响应帧与请求 id 错配/缺失（流错位）：当前请求失败并失效连接，
    下一请求可重新建连。"""


# 幂等只读命令：传输层中断后可安全自动重试一次。
_SAFE_RETRY = frozenset({"status", "get_config"})


class IpcClient:
    def __init__(self, socket_path, timeout=10):
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock = None
        self._reader = None
        self._seq = 0
        # 整段请求-响应串行化；RLock 允许内部重连重试重入。
        self._call_lock = threading.RLock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        if not os.path.exists(self.socket_path):
            raise FileNotFoundError(f"服务 socket 不存在：{self.socket_path}")
        # 原子构建：先建局部 socket，成功连接后再交给实例；失败关闭局部并
        # 保证 self._sock 为 None，避免部分连接状态/资源泄漏。
        local = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            local.settimeout(self.timeout)
            local.connect(self.socket_path)
        except OSError:
            try:
                local.close()
            except OSError:
                pass
            self._sock = None
            self._reader = None
            raise
        self._sock = local
        self._reader = self._sock.makefile("r", encoding="utf-8")

    def _open(self):
        """(重)建连接；复用既有 reader/socket 时先关闭。"""
        self.close()
        self.connect()

    def _ensure(self):
        """连接缺失时 best-effort 建连；失败静默，交由本次调用自然报错，
        下次调用再尝试，避免泵线程因瞬时不可用永久放弃。"""
        if self._sock is None:
            try:
                self._open()
            except OSError:
                pass

    def _exchange(self, cmd, args):
        """发送一次请求并按 id 取回匹配响应；不做重连。"""
        if self._sock is None:
            raise ConnectionError("未连接到服务，无法发送请求")
        if self._reader is None and self._sock is not None:
            self._reader = self._sock.makefile("r", encoding="utf-8")
        self._seq += 1
        seq = self._seq
        payload = {"cmd": cmd, "id": seq, "args": args or {}}
        self._sock.sendall(
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        line = self._reader.readline()
        if not line:
            raise ConnectionError("服务无响应")
        resp = json.loads(line)
        rid = resp.get("id")
        if rid == seq:
            return resp
        # 响应 id 错配/缺失即流错位：失效连接并抛专属协议错误；
        # 当前请求不重试，下一请求由 call() 起始 ensure 重建。
        self.close()
        raise IpcClientProtocolError(
            f"响应 id 错配：期望 {seq}，实际 {rid!r}")

    def call(self, cmd, args=None):
        with self._call_lock:
            self._ensure()
            if self._sock is None:
                raise ConnectionError("未连接到服务，无法发送请求")
            try:
                return self._call_once(cmd, args)
            except (OSError, ConnectionError, json.JSONDecodeError) as e:
                # 先关闭旧 socket；重连失败不得遮蔽原始“结果未知”语义。
                self.close()
                if cmd in _SAFE_RETRY:
                    # 只读幂等命令可重建连接重试一次。
                    self._open()
                    return self._call_once(cmd, args)
                # 写命令：结果未知，绝不自动重放；best-effort 重连供下次使用，
                # 并保持原始异常包装为明确的 UnknownResult。
                try:
                    self._open()
                except OSError:
                    pass
                raise RuntimeError(
                    f"命令 {cmd} 连接中断、结果未知，请勿重复操作: {e}")

    def _call_once(self, cmd, args):
        resp = self._exchange(cmd, args)
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or "命令失败")
        return resp.get("data") or {}

    def status(self):
        return self.call("status")

    def close(self):
        for attr in ("_reader", "_sock"):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
                setattr(self, attr, None)
