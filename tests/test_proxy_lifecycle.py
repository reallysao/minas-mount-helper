#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ProxyServer 嵌入生命周期：start/stop/join/端口占用/可重复停止。

全部进程内线程 + 127.0.0.1 临时端口；不起子进程、不连真实 NAS/凭证/挂载。
"""
import http.client
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mountapp"))

import mwdav_proxy as mod


def _get(port: int, path: str = "/", method: str = "GET") -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, headers={})
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


class ProxyServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 隔离状态文件；未发布时入站认证仍先返回 401。
        os.environ["MWDAV_STATE"] = os.path.join(self.tmp.name, "state.json")
        mod.STATE_FILE = os.environ["MWDAV_STATE"]

    def tearDown(self):
        os.environ.pop("MWDAV_STATE", None)
        mod.STATE_FILE = mod.state_store.default_state_path()
        self.tmp.cleanup()

    def _http_thread_names(self) -> list[str]:
        return [t.name for t in threading.enumerate()
                if t.name.startswith("mwdav-http")]

    def test_start_stop_join_and_release(self):
        srv = mod.ProxyServer(0)
        port = srv.start()
        self.assertGreater(port, 0)
        self.assertTrue(srv.is_running())
        self.assertEqual(srv.listen_port, port)
        # 未发布目标时返回 503，证明服务线程在响应且配置故障不误报成认证失败。
        self.assertEqual(_get(port), 503)

        srv.stop()
        self.assertFalse(srv.is_running())
        self.assertIsNone(srv.listen_port)
        self.assertEqual(self._http_thread_names(), [])
        # 可重复停止，无异常。
        srv.stop()
        # 端口已释放：可在同端口再次启动。
        again = mod.ProxyServer(port)
        try:
            again.start()
            self.assertTrue(again.is_running())
        finally:
            again.stop()

    def test_bind_conflict_raises_keeps_original(self):
        first = mod.ProxyServer(0)
        port = first.start()
        self.addCleanup(first.stop)
        dup = mod.ProxyServer(port)
        with self.assertRaises(OSError):
            dup.start()
        self.assertIsNone(dup.listen_port)
        # 原服务未被波及。
        self.assertTrue(first.is_running())
        self.assertEqual(_get(port), 503)

    def test_repeated_start_stop_cycle_no_leak(self):
        srv = mod.ProxyServer(0)
        for _ in range(3):
            port = srv.start()
            self.assertTrue(srv.is_running())
            self.assertEqual(_get(port), 503)
            srv.stop()
            self.assertFalse(srv.is_running())
            self.assertEqual(self._http_thread_names(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
