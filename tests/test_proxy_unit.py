#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mwdav_proxy 单元测试（内存流 + mock 上游，无网络/真实 NAS/挂载）。

覆盖：A01 入站认证、A02 超限/chunked/短读、A04 Depth 隔离、A05 命名空间缓存、
A06 MOVE 双端失效、A12 HEAD 长度、A13 硬上限、A14 不安全方法不重放、缓存体隔离。
"""
import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mountapp"))

import mwdav_proxy as proxy
from state_store import TlsTarget, load_snapshot, publish_target

AUTH = "Basic " + base64.b64encode(
    b"fixture-user:fixture-password-000000000").decode()


def auth_headers(**extra):
    h = {"Authorization": AUTH}
    h.update(extra)
    return h


def listing(prefix=""):
    nodes = "".join(
        f'<{prefix}response><{prefix}href>/pool0/data/dir/{n}</{prefix}href></{prefix}response>'
        for n in ("a", "b"))
    if prefix:
        return (f'<multistatus xmlns:D="DAV:">{nodes}</multistatus>').encode()
    return f'<multistatus xmlns="DAV:">{nodes}</multistatus>'.encode()


def make_handler(method, path, headers, body=b""):
    h = object.__new__(proxy.Handler)
    h.command, h.path = method, path
    h.headers = headers
    h.rfile, h.wfile = io.BytesIO(body), io.BytesIO()
    h.send_response, h.send_header, h.end_headers = Mock(), Mock(), Mock()
    return h


class ProxyUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "state.json")
        proxy.STATE_FILE = self.state
        publish_target(self.state, port=12345, username="fixture-user",
                       password="fixture-password-000000000",
                       tls=TlsTarget(server_name="fixture-nas.test",
                                     ca_file="/tmp/ca.pem"))
        proxy.UP = proxy.Upstream()
        proxy.UP.adopt(load_snapshot(self.state))
        proxy._propfind_cache.clear()

    def tearDown(self):
        proxy._propfind_cache.clear()
        self.tmp.cleanup()

    # ---- A01 ----
    def test_a01_no_authorization_rejected_401(self):
        h = make_handler("GET", "/dir", {})
        with patch.object(proxy, "_send_upstream") as up:
            h._handle()
        up.assert_not_called()
        h.send_response.assert_called_once_with(401)

    def test_a01_wrong_credentials_401(self):
        h = make_handler("GET", "/dir", {"Authorization": "Basic aW52YWxpZDp3cm9uZw=="})
        with patch.object(proxy, "_send_upstream") as up:
            h._handle()
        up.assert_not_called()
        h.send_response.assert_called_once_with(401)

    def test_a01_correct_credentials_forwarded(self):
        import base64
        auth = "Basic " + base64.b64encode(b"fixture-user:fixture-password-000000000").decode()
        h = make_handler("GET", "/dir", {"Authorization": auth})
        with patch.object(proxy, "_send_upstream", return_value=(200, {}, b"ok")) as up:
            h._handle()
        up.assert_called_once()

    # ---- A02 ----
    def test_a02_oversize_put_rejected_413_not_forwarded(self):
        h = make_handler("PUT", "/big.bin",
                        {"Authorization": AUTH,
                         "Content-Length": str(proxy.MAX_REQUEST_BODY + 1)})
        with patch.object(proxy, "_send_upstream") as up:
            h._handle()
        up.assert_not_called()
        h.send_response.assert_called_once_with(413)

    def test_a02_chunked_write_rejected_501(self):
        h = make_handler("PUT", "/big.bin",
                        {"Authorization": AUTH, "Transfer-Encoding": "chunked",
                         "Content-Length": "10"})
        with patch.object(proxy, "_send_upstream") as up:
            h._handle()
        up.assert_not_called()
        h.send_response.assert_called_once_with(501)

    def test_a02_short_body_rejected_no_empty_forward(self):
        h = make_handler("PUT", "/f.bin",
                        {"Authorization": AUTH, "Content-Length": "10"}, body=b"short")
        with patch.object(proxy, "_send_upstream") as up:
            h._handle()
        up.assert_not_called()
        h.send_response.assert_called_once_with(400)

    # ---- A04 Depth 隔离 ----
    def test_a04_depth0_does_not_hit_depth1_cache(self):
        # 先写 Depth:1 缓存
        h1 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "1"}, body=b"")
        with patch.object(proxy, "_send_upstream",
                          return_value=(207, {"Content-Type": "application/xml"},
                                        listing())):
            h1._handle()
        # Depth:0 必须重新上游取
        h2 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "0"}, body=b"")
        with patch.object(proxy, "_send_upstream") as up:
            h2._handle()
        up.assert_called_once()

    # ---- A05 命名空间缓存 ----
    def test_a05_namespaced_listing_cached(self):
        h1 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "1"}, body=b"")
        with patch.object(proxy, "_send_upstream",
                          return_value=(207, {"Content-Type": "application/xml"},
                                        listing("D:"))):
            h1._handle()
        h2 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "1"}, body=b"")
        with patch.object(proxy, "_send_upstream") as up:
            h2._handle()
        up.assert_not_called()

    # ---- A06 MOVE 目标失效 ----
    def test_a06_move_invalidates_destination_cache(self):
        key = proxy._propfind_cache_key("/dest", "1", proxy.UP.generation, None)
        proxy._cache_set(key, b"old-listing")
        self.assertIsNotNone(proxy._cache_get(key))
        h = make_handler("MOVE", "/src/item",
                        {"Authorization": AUTH,
                         "Destination": "http://127.0.0.1:18445/dest/item"})
        with patch.object(proxy, "_send_upstream", return_value=(201, {}, b"")):
            h._handle()
        self.assertIsNone(proxy._cache_get(key))

    # ---- A12 HEAD ----
    def test_a12_head_preserves_upstream_content_length(self):
        h = make_handler("HEAD", "/file", {"Authorization": AUTH})
        with patch.object(proxy, "_send_upstream",
                          return_value=(200, {"Content-Length": "4096",
                                              "Content-Type": "text/plain"}, b"")):
            h._handle()
        sent = {}
        for call in h.send_header.call_args_list:
            k, v = call.args
            sent[k] = v
        self.assertEqual(sent.get("Content-Length"), "4096")
        self.assertEqual(h.wfile.getvalue(), b"")

    # ---- A13 硬上限 ----
    def test_a13_cache_hard_cap(self):
        for i in range(proxy.PROPFIND_CACHE_MAX_ENTRIES + 5):
            proxy._cache_set(
                proxy._propfind_cache_key(f"/p{i}", "1", 1, None), b"x")
        self.assertLessEqual(len(proxy._propfind_cache),
                             proxy.PROPFIND_CACHE_MAX_ENTRIES)
        self.assertEqual(len(proxy._propfind_cache),
                         proxy.PROPFIND_CACHE_MAX_ENTRIES)

    # ---- 缓存体隔离（不同属性查询不串用） ----
    def test_propfind_different_body_not_shared(self):
        b1, b2 = b"<q1/>", b"<q2/>"
        h1 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "1",
                           "Content-Length": str(len(b1))}, body=b1)
        with patch.object(proxy, "_send_upstream",
                          return_value=(207, {"Content-Type": "application/xml"},
                                        listing())):
            h1._handle()
        h2 = make_handler("PROPFIND", "/dir",
                          {"Authorization": AUTH, "Depth": "1",
                           "Content-Length": str(len(b2))}, body=b2)
        with patch.object(proxy, "_send_upstream") as up:
            h2._handle()
        up.assert_called_once()

    # ---- A14 不安全方法不重放 ----
    def test_a14_unsafe_method_no_replay_on_reset(self):
        conn = Mock()
        conn.request.side_effect = ConnectionResetError("lost")
        with patch.object(proxy, "_obtain_conn", return_value=conn):
            with self.assertRaises(proxy.UnknownCommitError):
                proxy._send_upstream("DELETE", "/item", {}, None)
        self.assertEqual(conn.request.call_count, 1)

    def test_a14_safe_get_retries_once(self):
        conn = Mock()
        first = [True]
        def side_effect(*a, **k):
            if first[0]:
                first[0] = False
                raise ConnectionResetError("lost")
            body_chunks = [b"ok", b""]
            conn.getresponse.return_value = Mock(
                status=200, read=lambda n=-1: body_chunks.pop(0),
                getheaders=lambda: [])
        conn.request.side_effect = side_effect
        with patch.object(proxy, "_obtain_conn", return_value=conn):
            status, _, data = proxy._send_upstream("GET", "/item", {}, None)
        self.assertEqual(conn.request.call_count, 2)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
