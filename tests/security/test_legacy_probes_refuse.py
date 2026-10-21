#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全回归：仓库根目录历史人工探针在无 --allow-live、无环境凭证时必须拒绝执行。

这些脚本会连接真实隧道/使用真实凭证；默认（含 pytest、run_tests.py）必须退出码 2，
不得触网、不得挂载。属 A 层离线用例：仅以子进程验证拒绝行为，不发起任何网络。
"""
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_PROBES = [
    "probe_ports.py",
    "test_finder_full.py",
    "test_host_header.py",
    "test_pool_info.py",
    "test_propfind.py",
    "test_sni.py",
    "test_webdav_auth.py",
]

# 干净环境：不传任何 MINAS_TEST_* 凭证。
CLEAN_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "PYTHONIOENCODING": "utf-8",
}


class LegacyProbesRefuseTest(unittest.TestCase):
    def test_each_probe_refuses_without_opt_in(self):
        failures = []
        for name in LEGACY_PROBES:
            path = os.path.join(REPO_ROOT, name)
            if not os.path.exists(path):
                failures.append(f"{name}: 文件不存在")
                continue
            proc = subprocess.run(
                [sys.executable, path],
                cwd=REPO_ROOT, env=CLEAN_ENV,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
            )
            if proc.returncode != 2:
                stderr = proc.stderr.decode("utf-8", "replace")[:200]
                failures.append(f"{name}: 期望退出码 2，实际 {proc.returncode} | {stderr}")
        self.assertFalse(failures, "以下历史探针未默认拒绝执行:\n" + "\n".join(failures))

    def test_probe_does_not_run_when_credentials_absent_but_flag_passed(self):
        # 即使带了 --allow-live，缺环境凭证仍必须拒绝（exit 2），防止空凭证误触网。
        path = os.path.join(REPO_ROOT, "test_webdav_auth.py")
        proc = subprocess.run(
            [sys.executable, path, "--allow-live"],
            cwd=REPO_ROOT, env=CLEAN_ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )
        self.assertEqual(proc.returncode, 2,
                         "带 --allow-live 但无凭证时也必须拒绝执行")


if __name__ == "__main__":
    unittest.main()
