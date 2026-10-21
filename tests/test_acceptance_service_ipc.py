#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · ResidentService IPC（审查者独占 test_acceptance_service_*）。

覆盖：status 白名单脱敏、未知/畸形命令零副作用、set_config 路径与类型校验、
命令串行、多客户端、socket 0600/目录 0700、stop 命令。构造注入 fake，不触真实系统。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance_service_fakes as F  # noqa: E402
from acceptance_service_fakes import (  # noqa: E402
    ScriptedDiscovery, RecordingMount, RecordingProxy, view, IpcClient, wait_until)

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "acc_service", F._MOUNTAPP / "service.py")
service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service)

STATUS_KEYS = {
    "service_pid", "connected", "mode", "webdav_port", "mounted",
    "mount_path", "storage", "error", "generation",
}


def _make(tmp, views=None):
    return service.ResidentService(
        state_path=str(Path(tmp) / "state.json"),
        socket_dir=str(Path(tmp) / "run"),
        discovery=ScriptedDiscovery(views or [view(True, 9001)]),
        mount_registry=RecordingMount(),
        proxy_factory=lambda p: RecordingProxy(p),
        stop_timeout=2.0,
        interval=0.2)


class ServiceIpcTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="acc-ipc-")
        self.addCleanup(self.tmp.cleanup)
        self.svc = _make(self.tmp.name)
        self.svc.start()
        self.addCleanup(self.svc.stop)
        self.sock = Path(self.tmp.name) / "run" / "mwdav.sock"
        wait_until(lambda: self.sock.exists())

    # ---- SI-1：status 仅白名单键，响应不含凭证 ----
    def test_si1_status_whitelist_redacted(self):
        c = IpcClient(str(self.sock))
        self.addCleanup(c.close)
        r = c.call("status")
        self.assertTrue(r["ok"], r)
        self.assertEqual(set(r["data"].keys()), STATUS_KEYS)
        blob = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("password", blob.lower())
        self.assertNotIn("username", blob.lower())
        self.assertNotIn(F.FIXTURE_PASSWORD, blob)

    # ---- SI-2：未知命令 / 畸形 JSON / 非对象 / 缺 cmd → ok=false 零副作用 ----
    def test_si2_unknown_and_malformed_zero_side_effect(self):
        c = IpcClient(str(self.sock))
        self.addCleanup(c.close)
        self.assertFalse(c.call("nope")["ok"])
        self.assertFalse(json.loads(c.raw("{not json}"))["ok"])
        self.assertFalse(json.loads(c.raw("[1,2]"))["ok"])
        self.assertFalse(json.loads(c.raw('{"args":{}}'))["ok"])
        # 服务仍正常
        self.assertTrue(c.call("status")["ok"])

    # ---- SI-3：set_config 路径/类型校验：逃逸/非 /Volumes/非布尔拒绝 ----
    def test_si3_set_config_validation(self):
        c = IpcClient(str(self.sock))
        self.addCleanup(c.close)
        bad = [
            {"mount_dir": "/Volumes/../etc"},
            {"mount_dir": "/tmp/x"},
            {"mount_dir": "../../etc"},
            {"auto_mount": "yes"},
        ]
        for args in bad:
            self.assertFalse(c.call("set_config", args)["ok"], args)
        ok = c.call("set_config", {"auto_mount": False, "mount_dir": "/Volumes/x"})
        self.assertTrue(ok["ok"], ok)
        self.assertFalse(ok["data"]["auto_mount"])
        self.assertEqual(ok["data"]["mount_dir"], "/Volumes/x")

    # ---- SI-4：并发请求全部 ok、id 回显、无交错损坏（串行锁） ----
    def test_si4_concurrent_serialized(self):
        results = []
        lock = threading.Lock()

        def worker(i):
            c = IpcClient(str(self.sock))
            r = c.call("status", req_id=i)
            with lock:
                results.append(r)
            c.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 20)
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(sorted(r["id"] for r in results), list(range(20)))

    # ---- SI-5：多客户端，断开其一不影响另一与服务 ----
    def test_si5_multi_client_disconnect_isolated(self):
        c1 = IpcClient(str(self.sock))
        c2 = IpcClient(str(self.sock))
        self.assertTrue(c1.call("status")["ok"])
        self.assertTrue(c2.call("status")["ok"])
        c1.close()
        self.assertTrue(c2.call("status")["ok"])
        c2.close()
        c3 = IpcClient(str(self.sock))
        self.addCleanup(c3.close)
        self.assertTrue(c3.call("status")["ok"])

    # ---- SI-6：socket 0600、目录 0700 ----
    def test_si6_permissions(self):
        smode = stat.S_IMODE(os.stat(self.sock).st_mode)
        dmode = stat.S_IMODE(os.stat(self.sock.parent).st_mode)
        self.assertEqual(smode, 0o600)
        self.assertEqual(dmode, 0o700)

    # ---- SI-7：stop 命令 ok，服务停止、socket 清理 ----
    def test_si7_stop_command(self):
        c = IpcClient(str(self.sock))
        r = c.call("stop")
        self.assertTrue(r["ok"], r)
        c.close()
        wait_until(lambda: not self.sock.exists(), timeout=3)
        self.assertFalse(self.sock.exists())


if __name__ == "__main__":
    unittest.main()
