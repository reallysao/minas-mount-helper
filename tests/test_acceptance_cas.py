#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · CAS 强并发屏障 + 损坏文件/同值发布（审查者独占 test_acceptance_*）。

针对已确认真实 race：read/compare/replace 无跨进程锁时，多写者同读一代次可双双
通过 CAS 并覆盖。本用例用 barrier 让 N 个独立进程同时以 expected_generation=0 发布，
强断言：恰好 1 个成功、其余全部 GenerationConflictError、0 个意外错误；最终代次=1、
只落盘胜者目标。另验：损坏 JSON 发布报错且代次不归 0；同值发布不落盘不 bump。

不改生产代码，不触网，凭证仅为本地夹具常量。
"""
import json
import multiprocessing as mp
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import acceptance_helpers as H
from acceptance_helpers import load_state_store

SS = load_state_store()
N_WORKERS = 8


def _barrier_worker(barrier, queue, path, port, user, pw):
    """独立进程：barrier 同步放行后同代次 CAS 发布，回报结果。"""
    try:
        barrier.wait()
        snap = SS.publish_target(
            path, port=port, username=user, password=pw,
            base="/pool0/data", expected_generation=0)
        queue.put(("ok", snap.generation, snap.port))
    except SS.GenerationConflictError:
        queue.put(("conflict",))
    except Exception as e:  # 任何意外错误都要显式暴露，禁止吞掉
        queue.put(("error", f"{type(e).__name__}: {e}"))


class CasBarrierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="acc-cas-")
        self.path = os.path.join(self.tmp.name, "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_n_processes_exactly_one_wins_cas(self):
        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(N_WORKERS)
        queue = ctx.Queue()
        procs = []
        for i in range(N_WORKERS):
            p = ctx.Process(
                target=_barrier_worker,
                args=(barrier, queue, self.path, 20000 + i,
                      H.FIXTURE_USER, H.FIXTURE_PASSWORD))
            p.start()
            procs.append(p)
        for p in procs:
            p.join(30)
            self.assertFalse(p.is_alive(), "CAS 工作进程未在时限内结束")

        outcomes = [queue.get_nowait() for _ in range(N_WORKERS)]
        ok = [o for o in outcomes if o[0] == "ok"]
        conflict = [o for o in outcomes if o[0] == "conflict"]
        errors = [o for o in outcomes if o[0] == "error"]

        # 强断言：恰好一成功，其余全部冲突，零意外错误（不放宽计数）
        self.assertEqual(len(ok), 1,
                         f"CAS 胜者数量应为 1，实际 {len(ok)}: {outcomes}")
        self.assertEqual(len(conflict), N_WORKERS - 1,
                         f"冲突数量应为 {N_WORKERS - 1}，实际 {len(conflict)}: {outcomes}")
        self.assertEqual(errors, [], f"存在意外错误: {errors}")

        final = SS.load_snapshot(self.path)
        self.assertIsNotNone(final)
        self.assertEqual(final.generation, 1,
                         f"最终代次应为 1，实际 {final.generation}")
        # 只落盘胜者端口，没有其他写者覆盖
        self.assertEqual(final.port, ok[0][2])
        self.assertEqual(final.username, H.FIXTURE_USER)

    def test_in_process_threads_one_wins_one_conflict(self):
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(port):
            barrier.wait()
            try:
                snap = SS.publish_target(
                    self.path, port=port, username=H.FIXTURE_USER,
                    password=H.FIXTURE_PASSWORD, base="/pool0/data",
                    expected_generation=0)
                with lock:
                    results.append(("ok", snap.generation, snap.port))
            except SS.GenerationConflictError:
                with lock:
                    results.append(("conflict",))
            except Exception as e:
                with lock:
                    results.append(("error", f"{type(e).__name__}: {e}"))

        t1 = threading.Thread(target=worker, args=(31001,))
        t2 = threading.Thread(target=worker, args=(31002,))
        t1.start(); t2.start(); t1.join(15); t2.join(15)
        ok = [r for r in results if r[0] == "ok"]
        conflict = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(len(conflict), 1, results)
        self.assertEqual(SS.load_snapshot(self.path).generation, 1)

    def test_corrupt_json_publish_errors_generation_not_reset(self):
        # 先写一个代次 2 的合法快照
        SS.publish_target(self.path, port=21000, username=H.FIXTURE_USER,
                          password=H.FIXTURE_PASSWORD, base="/pool0/data")
        SS.publish_target(self.path, port=21001, username=H.FIXTURE_USER,
                          password=H.FIXTURE_PASSWORD, base="/pool0/data")
        before = SS.load_snapshot(self.path)
        self.assertEqual(before.generation, 2)

        # 破坏 JSON
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json !!!")

        # 发布必须报错，且不得把损坏文件当 current=None 重置代次到 0
        with self.assertRaises(SS.StateFormatError):
            SS.publish_target(
                self.path, port=21002, username=H.FIXTURE_USER,
                password=H.FIXTURE_PASSWORD, base="/pool0/data")
        # 文件仍为损坏内容，未被回写成 gen=0 的合法文件
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"{ this is not valid json !!!")
        # 纯读也必须报错，不静默返回 None
        with self.assertRaises(SS.StateFormatError):
            SS.load_snapshot(self.path)

    def test_same_value_publish_does_not_bump(self):
        snap = SS.publish_target(
            self.path, port=22000, username=H.FIXTURE_USER,
            password=H.FIXTURE_PASSWORD, base="/pool0/data")
        gen = snap.generation
        again = SS.publish_target(
            self.path, port=22000, username=H.FIXTURE_USER,
            password=H.FIXTURE_PASSWORD, base="/pool0/data")
        self.assertEqual(again.generation, gen, "同值发布不应递增代次")
        self.assertEqual(SS.load_snapshot(self.path).generation, gen)


if __name__ == "__main__":
    unittest.main()
