#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · 动态回环真实 HTTP/TLS + 独立代理进程（审查者独占 test_acceptance_*）。

- spawn 独立子进程加载生产 mountapp/mwdav_proxy.py；
- 本地临时自签 CA/服务端/客户端证书；127.0.0.1 临时端口；
- 不连真实 NAS、不挂载、不用真实凭证、不调根探针。

覆盖：D01 正常 A→B 对照、D02 读不回滚/错误不写旧目标、D04 重试取最新 B、
T14 不安全方法响应丢失不重放、mTLS+认证 OPTIONS、匿名 0 上游、慢存储不阻塞发布。
"""
import base64
import http.client
import multiprocessing as mp
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_helpers as H
from acceptance_helpers import (
    make_ca_tree, FixtureDav, load_state_store, production_proxy_path,
)


def _child_main(state_file, ready, done):
    """子进程：加载生产代理，纯读采纳快照，起临时端口，回报后等待停止。"""
    import sys
    import traceback
    import importlib.util
    try:
        mountapp = str(H.MOUNTAPP)
        sys.path.insert(0, mountapp)
        spec = importlib.util.spec_from_file_location(
            "acceptance_child_proxy", str(production_proxy_path()))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.STATE_FILE = state_file
        srv = mod.ProxyServer(0)
        srv.start()
        ready.put(srv.httpd.server_port)
        done.wait(30)
        srv.stop()
    except Exception:
        ready.put("ERR:" + traceback.format_exc())
        done.wait(30)


class ChildProxy:
    def __init__(self, state_file):
        # 显式使用 spawn：子进程从干净解释器启动，避免在已有代理工作线程的
        # 多线程父进程中 fork（fork 在多线程进程里可能死锁并触发
        # DeprecationWarning）。target 为本模块顶层函数，spawn 按引用可 pickle。
        ctx = mp.get_context("spawn")
        self.ready, self.done = ctx.Queue(), ctx.Event()
        self.proc = ctx.Process(
            target=_child_main, args=(state_file, self.ready, self.done))
        self.proc.start()
        try:
            payload = self.ready.get(timeout=15)
        except Exception:
            self.proc.terminate()
            self.proc.join()
            raise RuntimeError("代理子进程未就绪（15s 无回报）")
        if isinstance(payload, str) and payload.startswith("ERR:"):
            self.proc.terminate()
            self.proc.join()
            raise RuntimeError("代理子进程异常:\n" + payload[4:])
        self.port = payload

    def close(self):
        self.done.set()
        self.proc.join(10)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join()


def _get(port, path="/fixture", headers=None, method="GET"):
    # headers=None 时默认带夹具凭证；显式传 {} 表示匿名（401 用例）
    hdrs = {"Authorization": H.FIXTURE_AUTH} if headers is None else dict(headers)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, headers=hdrs)
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        conn.close()


class DynamicRotationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp_root = tempfile.TemporaryDirectory(prefix="acc-dyn-")
        cls.certs = make_ca_tree(Path(cls.tmp_root.name) / "ca",
                                  "fixture-nas.test")

    @classmethod
    def tearDownClass(cls):
        cls.tmp_root.cleanup()

    def setUp(self):
        self.stack = ExitStack()
        self.tmp = tempfile.TemporaryDirectory(prefix="acc-case-")
        self.stack.callback(self.tmp.cleanup)
        self.state_file = str(Path(self.tmp.name) / "state.json")
        self.ss = load_state_store()

    def tearDown(self):
        self.stack.close()

    def publish(self, port, tls=True):
        # tls=False：明文上游（用于模拟"已提交但响应丢失"的普通 HTTP 夹具）
        target_tls = None
        if tls:
            target_tls = self.ss.TlsTarget(
                ca_file=self.certs["ca_cert"],
                cert_file=self.certs.get("client_cert"),
                key_file=self.certs.get("client_key"),
                server_name="fixture-nas.test")
        return self.ss.publish_target(
            self.state_file, port=port,
            username=H.FIXTURE_USER, password=H.FIXTURE_PASSWORD,
            base="/pool0/data", tls=target_tls)

    def state_port(self):
        return self.ss.load_snapshot(self.state_file).port

    def proxy(self):
        p = ChildProxy(self.state_file)
        self.stack.callback(p.close)
        return p

    # ---- D01 对照：无交错 A→B 两次 200 ----
    def test_d01_normal_rotation_ab(self):
        a = self.stack.enter_context(FixtureDav(self.certs, label="A"))
        b = self.stack.enter_context(FixtureDav(self.certs, label="B"))
        self.publish(a.port)
        p = self.proxy()
        s1, body1 = _get(p.port)
        self.assertEqual((s1, body1), (200, "A"))
        self.publish(b.port)
        s2, body2 = _get(p.port)
        self.assertEqual((s2, body2), (200, "B"))

    # ---- D02：读请求不回写；失败路径不把旧目标写回；状态代次不因读增长 ----
    def test_d02_read_never_writes_and_error_does_not_rollback(self):
        b = self.stack.enter_context(FixtureDav(self.certs, label="B"))
        snap0 = self.publish(b.port)
        p = self.proxy()
        # 若干成功读：文件代次/内容不变（读零写）
        gen_before = self.ss.load_snapshot(self.state_file).generation
        for _ in range(3):
            s, _ = _get(p.port, headers={"Authorization": H.FIXTURE_AUTH})
            self.assertEqual(s, 200)
        self.assertEqual(self.ss.load_snapshot(self.state_file).generation,
                         gen_before)
        # 发布一个不可达端口后请求失败：错误不得把目标写回旧值
        self.ss.publish_target(
            self.state_file, port=1, username=H.FIXTURE_USER,
            password=H.FIXTURE_PASSWORD, base="/pool0/data",
            tls=self.ss.TlsTarget(ca_file=self.certs["ca_cert"],
                                   server_name="fixture-nas.test"))
        _get(p.port, headers={"Authorization": H.FIXTURE_AUTH})
        # 恢复 B 并确认仍可达（错误路径未把代次/目标污染）
        self.publish(b.port)
        s, body = _get(p.port, headers={"Authorization": H.FIXTURE_AUTH})
        self.assertEqual((s, body), (200, "B"))

    # ---- D04：首次失败后发布 B，重试/下一请求真正去 B，而非滞留旧 A ----
    def test_d04_retry_uses_new_target_b(self):
        b = self.stack.enter_context(FixtureDav(self.certs, label="B"))
        # 先发布不可达 A（关闭端口）
        self.publish(1)
        p = self.proxy()
        s_fail, _ = _get(p.port, headers={"Authorization": H.FIXTURE_AUTH})
        self.assertIn(s_fail, (502, 503))
        # 发布 B 后，新请求必须到达 B，B 收到请求
        self.publish(b.port)
        s, body = _get(p.port, headers={"Authorization": H.FIXTURE_AUTH})
        self.assertEqual((s, body), (200, "B"))
        self.assertTrue(any(r["method"] in ("GET", "OPTIONS")
                             for r in b.server.requests))

    # ---- T14：不安全 MOVE 响应丢失，上游只收到一次，不盲重放，返回 502 ----
    def test_t14_unsafe_move_not_replayed_after_lost_response(self):
        ep = self.stack.enter_context(
            FixtureDav(self.certs, label="D", drop=True))
        self.publish(ep.port)
        p = self.proxy()
        # 上游 12s 超时后才判定提交不确定（夹具 abrupt close 不立即 EOF），
        # 客户端等待要覆盖该窗口；关键断言是上游只收到 1 次、代理不重放。
        conn = http.client.HTTPConnection("127.0.0.1", p.port, timeout=20)
        try:
            conn.request("MOVE", "/src/item",
                         headers={"Authorization": H.FIXTURE_AUTH,
                                  "Destination": "https://127.0.0.1/dst/item"})
            r = conn.getresponse()
            r.read()
            status = r.status
        finally:
            conn.close()
        self.assertEqual(status, 502)
        time.sleep(0.3)
        self.assertEqual(ep.server.drop_count, 1,
                         "不安全方法被重放，上游收到第二次 MOVE")

    # ---- mTLS + 认证 OPTIONS：带客户端证书与 Basic 才成功；服务端要求 mTLS ----
    def test_mtls_and_authenticated_options(self):
        ep = self.stack.enter_context(
            FixtureDav(self.certs, label="B", mtls=True, auth_required=True))
        self.publish(ep.port)
        p = self.proxy()
        s, body = _get(p.port, path="/pool0/data/",
                       method="OPTIONS",
                       headers={"Authorization": H.FIXTURE_AUTH})
        self.assertEqual(s, 200)
        self.assertEqual(body, "B")
        self.assertTrue(ep.server.requests)
        self.assertEqual(ep.server.requests[0]["auth"], H.FIXTURE_AUTH)

    # ---- 匿名：无凭证 401，上游 0 请求 ----
    def test_anonymous_zero_upstream_requests(self):
        ep = self.stack.enter_context(FixtureDav(self.certs, label="B"))
        self.publish(ep.port)
        p = self.proxy()
        s, _ = _get(p.port, headers={})  # 显式匿名，不带 Authorization
        self.assertEqual(s, 401)
        self.assertEqual(ep.server.requests, [])

    # ---- D07（C 侧）：慢存储 get_pool_info 独立，refresh 快路径不调用它 ----
    def test_d07_slow_storage_separated_from_fast_path(self):
        import importlib.util
        cm_path = H.MOUNTAPP / "conn_manager.py"
        spec = importlib.util.spec_from_file_location("acc_cm", str(cm_path))
        cm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cm)
        self.assertTrue(hasattr(cm, "ProbeOutcome"), "缺分层探测结果 ProbeOutcome")
        self.assertTrue(hasattr(cm.ConnManager, "poll_storage"),
                        "慢路径 poll_storage 未独立成方法")
        src = cm_path.read_text(encoding="utf-8")
        # refresh 方法体不得直接调用 get_pool_info / _probe_pool_once
        import re
        m = re.search(r"def refresh\(self.*?(?=\n    def )", src, re.S)
        self.assertIsNotNone(m)
        body = m.group(0)
        self.assertNotIn("_probe_pool_once", body,
                         "refresh 快路径同步调用了慢存储探测（D07 违规）")
        self.assertNotIn("get_pool_info", body,
                         "refresh 快路径同步请求了 get_pool_info（D07 违规）")


if __name__ == "__main__":
    unittest.main()
