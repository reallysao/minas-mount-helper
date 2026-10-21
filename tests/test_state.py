#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state_store 契约测试：原子发布、纯读零写、v1 兼容、CAS、健康不落盘。

全部本地临时目录，无网络、无真实凭证、无挂载。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mountapp"))

import state_store
from state_store import (
    TargetSnapshot, TlsTarget, StateFormatError, GenerationConflictError,
    load_snapshot, publish_target, read_legacy,
)


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "proxy_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_none_without_writing(self):
        self.assertIsNone(load_snapshot(self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_publish_is_atomic_and_restricted_permissions(self):
        snap = publish_target(self.path, port=49559, username="u-fixture",
                              password="fixture-password-000000000",
                              tls=TlsTarget(ca_file="/x/ca.pem", server_name="nas.test"))
        self.assertEqual(snap.generation, 1)
        mode = os.stat(self.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        data = json.loads(Path(self.path).read_text())
        self.assertEqual(data["port"], 49559)
        self.assertEqual(data["tls"]["server_name"], "nas.test")
        # 健康字段不落盘
        self.assertNotIn("connected", data)
        self.assertNotIn("last_error", data)

    def test_read_is_pure_and_does_not_rewrite_or_bump(self):
        publish_target(self.path, port=1, username="u", password="p")
        mtime1 = os.stat(self.path).st_mtime_ns
        g1 = load_snapshot(self.path).generation
        load_snapshot(self.path)
        self.assertEqual(load_snapshot(self.path).generation, g1)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime1)

    def test_generation_monotonic_and_cas_rejects_stale(self):
        first = publish_target(self.path, port=1, username="u", password="p")
        second = publish_target(self.path, port=2, username="u", password="p",
                                expected_generation=first.generation)
        self.assertEqual(second.generation, 2)
        with self.assertRaises(GenerationConflictError):
            publish_target(self.path, port=3, username="u", password="p",
                           expected_generation=first.generation)
        self.assertEqual(load_snapshot(self.path).port, 2)

    def test_corrupt_file_raises_without_self_healing(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        with self.assertRaises(StateFormatError):
            load_snapshot(self.path)
        # 不回写
        self.assertEqual(Path(self.path).read_text(), "{not json")

    def test_legacy_v1_fields_readable_but_health_ignored(self):
        Path(self.path).write_text(json.dumps({
            "port": 4000, "username": "u", "password": "p", "connected": True,
        }), encoding="utf-8")
        snap = load_snapshot(self.path)
        self.assertEqual(snap.port, 4000)
        self.assertFalse(hasattr(snap, "connected"))
        legacy = read_legacy(self.path)
        self.assertEqual(legacy, {"username": "u", "password": "p", "port": 4000})

    def test_concurrent_publishers_cas_exactly_one_winner_multi_round(self):
        """Barrier 同时发起 N 个带同一旧窗口的发布：恰一成功、其余 CAS 拒绝，
        胜方快照原样保留，重复多轮证明锁内 read/compare/write 原子性。"""
        publish_target(self.path, port=1, username="u", password="p")  # generation=1
        for round_no in range(5):
            baseline = load_snapshot(self.path).generation
            barrier = threading.Barrier(8)
            winners, errors = [], []

            def writer(port):
                barrier.wait()
                try:
                    snap = publish_target(
                        self.path, port=port, username="u", password="p",
                        expected_generation=baseline)
                    winners.append(snap.port)
                except GenerationConflictError:
                    errors.append(port)

            threads = [threading.Thread(target=writer, args=(200 + round_no * 10 + i,))
                       for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(winners), 1, f"round {round_no}: 应恰一成功")
            self.assertEqual(len(errors), 7, f"round {round_no}: 其余应被 CAS 拒绝")
            final = load_snapshot(self.path)
            self.assertEqual(final.port, winners[0])  # 胜方快照保留，无旧值回滚
            self.assertEqual(final.generation, baseline + 1)

    def test_corrupt_file_publish_refused_no_generation_rollback(self):
        publish_target(self.path, port=1, username="u", password="p")
        Path(self.path).write_text("{not json", encoding="utf-8")
        # 损坏文件不得被当成 current=None 把代次回滚为 0 后重写，直接拒绝。
        with self.assertRaises(StateFormatError):
            publish_target(self.path, port=2, username="u", password="p",
                           expected_generation=1)
        self.assertEqual(Path(self.path).read_text(), "{not json")

    def test_serial_cas_monotonic_and_stale_rejected(self):
        s = publish_target(self.path, port=1, username="u", password="p")
        s2 = publish_target(self.path, port=2, username="u", password="p",
                            expected_generation=s.generation)
        with self.assertRaises(GenerationConflictError):
            publish_target(self.path, port=3, username="u", password="p",
                           expected_generation=s.generation)
        self.assertEqual(load_snapshot(self.path).generation, s2.generation)

    def test_same_value_publish_does_not_rewrite_or_bump(self):
        first = publish_target(self.path, port=1, username="u", password="p")
        mtime1 = os.stat(self.path).st_mtime_ns
        again = publish_target(self.path, port=1, username="u", password="p")
        self.assertEqual(again.generation, first.generation)
        self.assertEqual(os.stat(self.path).st_mtime_ns, mtime1)

    def test_invalid_port_rejected(self):
        with self.assertRaises(StateFormatError):
            publish_target(self.path, port=0, username="u", password="p")
        with self.assertRaises(StateFormatError):
            publish_target(self.path, port=70000, username="u", password="p")


if __name__ == "__main__":
    unittest.main(verbosity=2)
