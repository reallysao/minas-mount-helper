#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D07：快路径确认即发布目标，阻塞的可选存储查询不得延迟发布。

本地临时目录 + 真实 state_store 原子发布；无真实 NAS/挂载/网络。
"""
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "..", "mountapp"))
sys.path.insert(0, MOUNTAPP)
sys.path.insert(0, HERE)

import app_core as ac
from _https_fixture import make_certificates


class FastPathPublish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="minas-fast-")
        self.certs = make_certificates(self.tmp.name)
        self.state_file = os.path.join(self.tmp.name, "state.json")
        os.environ["MWDAV_STATE"] = self.state_file
        # 隔离真实用户配置目录
        self.cfg_patch = patch.object(ac, "CONFIG_PATH", os.path.join(self.tmp.name, "config.json"))
        self.sup_patch = patch.object(ac, "APP_SUPPORT", self.tmp.name)
        self.cfg_patch.start()
        self.sup_patch.start()
        self.core = ac.AppCore(config={
            "tls_server_name": "localhost",
            "tls_ca_file": self.certs["ca"],
            "tls_cert_file": self.certs["cert"],
            "tls_key_file": self.certs["key"],
        })

    def tearDown(self):
        self.cfg_patch.stop()
        self.sup_patch.stop()
        self.tmp.cleanup()
        os.environ.pop("MWDAV_STATE", None)

    def test_publish_succeeds_while_slow_pool_blocked(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_pool(_urlreq):
            entered.set()
            release.wait(5)
            return None

        self.core.conn._probe_pool_best = blocked_pool
        worker = threading.Thread(target=self.core.conn.poll_storage)
        worker.start()
        entered.wait(5)
        # 慢查询已进入并阻塞；快路径发布不得等待它
        ok = self.core._publish_target(49559, "fixture-user", "fixture-password-000000000")
        release.set()
        worker.join(7)
        self.assertTrue(ok)

        import state_store
        snap = state_store.load_snapshot(self.state_file)
        self.assertIsNotNone(snap)
        self.assertEqual(snap.port, 49559)
        self.assertEqual(snap.username, "fixture-user")
        self.assertEqual(snap.tls.server_name, "localhost")
        # 健康字段不落盘
        import json
        with open(self.state_file, encoding="utf-8") as f:
            raw = json.loads(f.read())
        self.assertNotIn("connected", raw)
        self.assertNotIn("last_error", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
