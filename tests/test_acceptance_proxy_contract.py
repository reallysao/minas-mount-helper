#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · 数据面契约（审查者独占 test_acceptance_*，进程内 Handler 级）。

不触网：上游发送函数被替换为记录夹具；所有目标写入临时 STATE_FILE。
覆盖 T01 入站恒认证/503/不匿名转发、T02 413/501/短读、T03 读零写、
T04 Depth 隔离、T05 命名空间缓存、T06 MOVE 双端失效、T12 HEAD 长度、T13 硬上限。
T14 不重放与 D01/D02/D04 真实回环进程测试见 test_acceptance_dynamic_rotation.py。
"""
import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_helpers as H
from acceptance_helpers import load_proxy_module, load_state_store

PROPFIND_NS = (
    b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">'
    b'<D:response><D:href>/pool0/data/a</D:href></D:response>'
    b'<D:response><D:href>/pool0/data/b</D:href></D:response>'
    b'</D:multistatus>')

PROPFIND_DEFAULT = (
    b'<multistatus xmlns="DAV:">'
    b'<response><href>/pool0/data/a</href></response>'
    b'<response><href>/pool0/data/b</href></response>'
    b'</multistatus>')


def make_request(mod, method="GET", path="/fixture", headers=None, body=b""):
    h = object.__new__(mod.Handler)
    h.command, h.path = method, path
    h.headers = headers or {}
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    h.send_response = Mock()
    h.send_header = Mock()
    h.end_headers = Mock()
    return h


class ProxyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod, cls.path, cls.hardened = load_proxy_module()

    def setUp(self):
        self.assertTrue(
            self.hardened,
            f"生产代理 {self.path.name} 尚未硬化（仍含 CERT_NONE/无入站认证），"
            "契约用例在就绪后复核；当前为未修复状态。")
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = str(Path(self.tmp.name) / "state.json")
        p = patch.object(self.mod, "STATE_FILE", self.state_file)
        p.start()
        self.addCleanup(p.stop)
        ss = load_state_store()
        tls = None
        if hasattr(ss, "TlsTarget"):
            tls = ss.TlsTarget(server_name="fixture-nas.test", ca_file="/tmp/ca.pem")
        ss.publish_target(
            self.state_file, port=49559,
            username=H.FIXTURE_USER, password=H.FIXTURE_PASSWORD,
            base="/pool0/data", tls=tls)
        self.mod.UP = self.mod.Upstream()
        self.mod.UP.adopt(ss.load_snapshot(self.state_file))
        self.mod._propfind_cache.clear()
        self.addCleanup(lambda: self.mod._propfind_cache.clear())
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return 200, {"Content-Type": "application/xml"}, b"ok"

    def _handle(self, method, path, headers=None, body=b""):
        h = make_request(self.mod, method, path, headers, body)
        with patch.object(self.mod, "_send_upstream", side_effect=self._fake_send):
            h._handle()
        return h

    # ---- T01：缺凭证 401 + WWW-Authenticate，不触达上游 ----
    def test_t01_missing_auth_401_and_upstream_untouched(self):
        h = self._handle("GET", "/fixture")
        h.send_response.assert_called_with(401)
        h.send_header.assert_any_call("WWW-Authenticate", 'Basic realm="MiNAS"')
        self.assertEqual(self.sent, [])

    def test_t01_wrong_password_401(self):
        bad = "Basic " + base64.b64encode(
            f"{H.FIXTURE_USER}:wrong".encode()).decode()
        h = self._handle("GET", "/fixture", {"Authorization": bad})
        h.send_response.assert_called_with(401)
        self.assertEqual(self.sent, [])

    def test_t01_correct_auth_reaches_upstream(self):
        h = self._handle("GET", "/fixture", {"Authorization": H.FIXTURE_AUTH})
        h.send_response.assert_called_with(200)
        self.assertEqual(len(self.sent), 1)

    def test_t01_incomplete_target_503_no_forward(self):
        # 目标配齐凭证但端口为 0（未配齐）→ 503，不转发；不落非法状态文件
        ss = load_state_store()
        snap = ss.TargetSnapshot(
            port=0, username=H.FIXTURE_USER,
            password=H.FIXTURE_PASSWORD, base="/pool0/data")
        self.mod.UP.adopt(snap)
        with patch.object(self.mod, "_reload_snapshot", return_value=snap):
            h = self._handle("GET", "/fixture",
                              {"Authorization": H.FIXTURE_AUTH})
        h.send_response.assert_called_with(503)
        self.assertEqual(self.sent, [])

    # ---- T02：超限 413 不空体转发；chunked 501；短读 400 ----
    def test_t02_oversize_put_413_not_empty_forward(self):
        limit = self.mod.MAX_REQUEST_BODY
        h = self._handle("PUT", "/big.bin",
                         {"Authorization": H.FIXTURE_AUTH,
                          "Content-Length": str(limit + 1)}, b"")
        h.send_response.assert_called_with(413)
        self.assertEqual(self.sent, [])

    def test_t02_chunked_write_501(self):
        h = self._handle("PUT", "/c.bin",
                         {"Authorization": H.FIXTURE_AUTH,
                          "Transfer-Encoding": "chunked"}, b"5\r\nhello\r\n0\r\n\r\n")
        h.send_response.assert_called_with(501)
        self.assertEqual(self.sent, [])

    def test_t02_short_body_400(self):
        h = self._handle("PUT", "/s.bin",
                         {"Authorization": H.FIXTURE_AUTH,
                          "Content-Length": "10"}, b"short")
        h.send_response.assert_called_with(400)
        self.assertEqual(self.sent, [])

    # ---- T03：纯读不回写、不 bump 代次；健康不落盘 ----
    def test_t03_read_has_no_persist_side_effect(self):
        before = Path(self.state_file).read_bytes()
        mtime = os.stat(self.state_file).st_mtime_ns
        for _ in range(3):
            self.mod._reload_snapshot()
        self.assertEqual(before, Path(self.state_file).read_bytes())
        self.assertEqual(mtime, os.stat(self.state_file).st_mtime_ns)
        self.mod.UP.set_health(connected=False, error="x")
        self.assertEqual(before, Path(self.state_file).read_bytes())

    # ---- T04：Depth0 不命中 Depth1 缓存（键含 Depth+代次+体摘要） ----
    def test_t04_depth_isolation(self):
        g = self.mod.UP.generation
        k1 = self.mod._propfind_cache_key("/dir", "1", g, None)
        self.mod._cache_set(k1, PROPFIND_DEFAULT)
        k0 = self.mod._propfind_cache_key("/dir", "0", g, None)
        self.assertIsNone(self.mod._cache_get(k0))
        self.assertEqual(self.mod._cache_get(k1), PROPFIND_DEFAULT)

    # ---- T05：命名空间 <D:response> 合法列表可缓存；坏 XML 计数 -1 ----
    def test_t05_namespaced_listing_accepted(self):
        self.assertGreaterEqual(self.mod._count_responses(PROPFIND_NS), 2)
        self.assertEqual(self.mod._count_responses(b"<not xml<<>"), -1)
        g = self.mod.UP.generation
        k = self.mod._propfind_cache_key("/ns", "1", g, PROPFIND_NS)
        self.mod._cache_set(k, PROPFIND_NS)
        self.assertIsNotNone(self.mod._cache_get(k))

    # ---- T06：MOVE 双端失效 ----
    def test_t06_move_invalidates_both_ends(self):
        g = self.mod.UP.generation
        ks = self.mod._propfind_cache_key("/src/dir", "1", g, None)
        kd = self.mod._propfind_cache_key("/dst/dir", "1", g, None)
        self.mod._cache_set(ks, b"x")
        self.mod._cache_set(kd, b"y")
        self.mod._invalidate_tree("/src/dir")
        self.mod._invalidate_tree("/dst/dir")
        self.assertIsNone(self.mod._cache_get(ks))
        self.assertIsNone(self.mod._cache_get(kd))

    # ---- T12：HEAD 保留上游 Content-Length，不改写 0 ----
    def test_t12_head_preserves_content_length(self):
        h = make_request(self.mod, "HEAD", "/f",
                          {"Authorization": H.FIXTURE_AUTH})
        with patch.object(self.mod, "_send_upstream",
                          return_value=(200, {"Content-Length": "4096"}, b"")):
            h._handle()
        lengths = [c.args[1] for c in h.send_header.call_args_list
                   if c.args[0].lower() == "content-length"]
        self.assertIn("4096", lengths)
        self.assertNotIn("0", lengths)

    # ---- T13：硬上限，插入超限强制淘汰 ----
    def test_t13_cache_hard_cap(self):
        cap = self.mod.PROPFIND_CACHE_MAX_ENTRIES
        for i in range(cap + 5):
            k = self.mod._propfind_cache_key(f"/p{i}", "1", 1, None)
            self.mod._cache_set(k, b"x")
        with self.mod._propfind_lock:
            size = len(self.mod._propfind_cache)
        self.assertLessEqual(size, cap)


if __name__ == "__main__":
    unittest.main()
