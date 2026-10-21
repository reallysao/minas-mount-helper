#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立验收 · 静态策略清点（审查者独占 test_acceptance_*）。

只读扫描，不触网、不挂载、不执行被审代码；用于就绪后的门禁复核。
覆盖：TLS 无降级字面量、env/config 入口、仓库零硬编码泄露、
旧 netfs 二进制不得冒充新 stdin 协议、文档安全宣称一致性。
"""
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acceptance_helpers import REPO_ROOT, MOUNTAPP

APP_SOURCE = [
    MOUNTAPP / "mwdav_proxy.py",
    MOUNTAPP / "conn_manager.py",
    MOUNTAPP / "app_core.py",
    MOUNTAPP / "main.py",
    MOUNTAPP / "state_store.py",
    MOUNTAPP / "tls_transport.py",
]

# 泄露检测不内嵌任何历史凭证明文：基线候选 token 的 sha256 前 16 位 hash 固化于此
# （由基线 d25c251 的 app_core/mwdav_proxy/conn_manager 离线提取），扫描当前源码
# 候选 hash 是否命中；不依赖运行时 git show（浅克隆/源码包无 .git 时也不误绿）。
import hashlib

BASELINE_LEAK_HASHES = {
    "1c88bfeffcd4ae11",
    "454081cd1cd3eb7b",
    "6a916b02e58916bc",
}

# 基线 d25c251 内 netfs_mount 原二进制 git blob 哈希（固定值，不比 HEAD，
# 避免 HEAD 漂移或浅克隆缺历史导致误判）。
NETFS_BINARY_ORIGINAL_SHA1 = "b8479f0fc74835c23d6da680049d66b0d060951d"


def _secret_candidates(text: str) -> set:
    """通用模式抽取候选：URL 密码、u+数字用户名、uid_数字证书名、IPv4、显式密码赋值。"""
    out = set()
    for m in re.finditer(r"://[^:/@\s\"']+:(?P<t>[^@/\s\"']+)@", text):
        out.add(m.group("t"))
    for m in re.finditer(r"\bu\d{6,}\b", text):
        out.add(m.group(0))
    for m in re.finditer(r"\b\d{6,}_\d{6,}\b", text):
        out.add(m.group(0))
    for m in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        ip = m.group(0)
        # 回环/未指定地址是本机正常配置，不是泄露公网主机，剔除避免误报
        if ip.split(".")[0] in ("127", "0"):
            continue
        out.add(ip)
    for m in re.finditer(
            r'(?:password|passwd|pwd)\s*[=:]\s*["\']([^"\']{6,})["\']',
            text, re.I):
        out.add(m.group(1))
    return out


def _h16(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

# SR-3 冻结的环境变量入口（env 优先于 config.json）。
REQUIRED_ENV = [
    "MWDAV_TLS_SERVER_NAME",
    "MWDAV_TLS_CA_FILE",
    "MWDAV_TLS_CERT_FILE",
    "MWDAV_TLS_KEY_FILE",
]

# 仅匹配"启用降级"的代码模式；拒绝清单（_DISABLE_KEYS）与注释/否定说明不算降级。
DOWNGRADE_PATTERNS = [
    (r"verify_mode\s*=\s*ssl\.CERT_NONE", "verify_mode=CERT_NONE"),
    (r"check_hostname\s*=\s*False", "check_hostname=False"),
    (r"os\.environ\.get\(\s*['\"]MWDAV_TLS_VERIFY['\"]\s*\)",
     "读取 MWDAV_TLS_VERIFY 作为开关"),
    (r"cfg\.get\(\s*['\"]tls_verify['\"]\s*\)\s*and", "tls_verify 真值分支"),
    (r"tls_verify\s*=\s*False", "tls_verify=False"),
]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


class InventoryPolicyTest(unittest.TestCase):
    def setUp(self):
        self.sources = {p.name: _read(p) for p in APP_SOURCE if p.exists()}

    # ---- T11 / SR-3：应用源码零 TLS 降级启用模式（忽略注释/文档串/否定句）----
    def test_app_source_has_no_tls_downgrade_literal(self):
        negation = ("无", "不提供", "不存在", "禁止", "不得", "never", "without", "no-")
        hits = []
        for name, text in self.sources.items():
            for line in text.splitlines():
                code = line.split("#", 1)[0]
                if any(n in line for n in negation):
                    continue  # 否定/禁止语境（含文档串）不算启用
                for pattern, label in DOWNGRADE_PATTERNS:
                    if re.search(pattern, code):
                        hits.append(f"{name}: {label}")
        self.assertFalse(hits, "应用源码发现 TLS 降级启用模式:\n" + "\n".join(hits))

    # ---- T11：env MWDAV_TLS_* 入口必须在应用源码中被读取 ----
    def test_must_read_mwdav_tls_env(self):
        blob = "\n".join(self.sources.values())
        missing = [e for e in REQUIRED_ENV if e not in blob]
        self.assertFalse(
            missing,
            "应用源码未读取 SR-3 冻结环境变量: " + ", ".join(missing)
            + "（env 须优先于 config.json 同名项）")

    # ---- SR-2：mountapp 内不得残留基线泄露 token（固化 hash 比对，不显示明文）----
    def test_mountapp_has_no_leaked_fragments(self):
        # 基线 hash 表为空属于测试配置错误，必须报错，绝不能空集合假通过
        self.assertTrue(
            BASELINE_LEAK_HASHES,
            "基线泄露 hash 表为空（BASELINE_LEAK_HASHES 未固化），禁止空集合误绿")
        hits = []
        for name, text in self.sources.items():
            for tok in _secret_candidates(text):
                h = _h16(tok)
                if h in BASELINE_LEAK_HASHES:
                    hits.append(f"{name}: 命中基线泄露 token hash={h}")
        self.assertFalse(
            hits,
            "mountapp 源码残留基线泄露凭证（仅显示 hash 前缀，不明文）:\n"
            + "\n".join(hits))

    # ---- M11：旧 x86_64/0644 blob 必须由 macOS CI 验证的 Universal v2 产物替换 ----
    def test_netfs_binary_replaced_by_verified_universal_build(self):
        binary = MOUNTAPP / "netfs_mount"
        if not binary.exists():
            self.skipTest("源码包未包含 netfs_mount 二进制，本项 N/A")
        try:
            work = subprocess.run(
                ["git", "hash-object", str(binary)], cwd=REPO_ROOT,
                capture_output=True, text=True, check=True).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as e:
            self.skipTest(f"无 git 可用，二进制哈希比对 N/A: {e}")
        self.assertNotEqual(
            work, NETFS_BINARY_ORIGINAL_SHA1,
            "仍是旧 x86_64/0644 netfs_mount；必须用 macOS CI 验证的 Universal v2 产物替换")
        self.assertTrue(os.access(binary, os.X_OK), "交付 helper 必须具备可执行权限")

    def test_netfs_source_uses_stdin_not_argv_for_credentials(self):
        src = _read(MOUNTAPP / "netfs_mount.m")
        self.assertTrue(src, "netfs_mount.m 缺失")
        # 新协议：user/pass 经 stdin 私有管道，argv 只含 url/mountpoint。
        # usage 字符串若在 url 后列出 <user> <pass>，即仍走 argv。
        self.assertNotRegex(
            src, r"mount <url> <user> <pass>|usage:.*<pass>",
            "netfs_mount.m 仍把 user/pass 放在 argv（SR-4 要求 stdin 管道）")
        self.assertIn("fileHandleWithStandardInput", src,
                      "netfs_mount.m 未从 stdin 读取凭证")
        # 新能力协议标记存在，供 caller 拒绝旧二进制
        self.assertIn("MWDAV_MOUNT_PROTOCOL=2", src)

    # ---- SR-4：app_core 不得把密码拼进挂载 argv / curl -u argv ----
    def test_no_credentials_on_argv(self):
        core = self.sources.get("app_core.py", "")
        suspicious = []
        for m in re.finditer(r"subprocess\.(run|Popen)\(\s*\[([^\]]*)\]", core, re.S):
            argv = m.group(2)
            # 只抓真正的凭证传递形态：curl -u、password= 字面量、user:pass 拼接元素
            if re.search(r'["\']-u["\']', argv) or \
                    re.search(r'password\s*=\s*["\'][^"\']+["\']', argv) or \
                    re.search(r'["\'][^"\']*:[^"\']*\{', argv):
                suspicious.append(argv[:160])
        self.assertFalse(suspicious,
                         "app_core 在 argv 中传递凭证:\n" + "\n".join(suspicious))

    # ---- SR-3 文档措辞：不得宣称 mTLS/客户端证书"确保真正 NAS" ----
    def test_docs_do_not_claim_client_cert_ensures_nas(self):
        bad = []
        for rel in ("README.md", "使用说明.md"):
            text = _read(REPO_ROOT / rel)
            if re.search(r"双向认证[，,].{0,12}确保连接的是真正", text):
                bad.append(rel)
        self.assertFalse(bad,
                         "文档把客户端认证等同于服务端身份验证（SR-3 禁止措辞）: "
                         + ", ".join(bad))

    # ---- 冻结约束：文档不得再宣称"入站 Basic 认证已移除" ----
    def test_manual_does_not_claim_inbound_auth_removed(self):
        text = _read(REPO_ROOT / "使用说明.md")
        self.assertNotRegex(
            text, r"移除本地代理入站 Basic 认证|入站 Basic 认证已于 v1\.2 移除",
            "使用说明.md 仍宣称入站认证已移除，与「入站永远 require 认证」冻结约束矛盾")

    # ---- FR-4.2：未接入的 sync_engine 不得在用户文档宣称可用 ----
    def test_docs_do_not_advertise_orphan_sync(self):
        main_py = self.sources.get("main.py", "")
        advertised = ("文件夹同步" in _read(REPO_ROOT / "README.md")
                       or "文件夹同步" in _read(REPO_ROOT / "使用说明.md"))
        imported = "sync_engine" in main_py
        if advertised and not imported:
            self.fail("文档宣称文件夹同步，但 main.py 未接入 sync_engine（FR-4.2）")

    # ---- SR-6：CI 离线门禁存在且只跑 tests/ ----
    def test_ci_offline_gate_present(self):
        wf = REPO_ROOT / ".github" / "workflows"
        files = list(wf.glob("*.yml")) + list(wf.glob("*.yaml")) if wf.exists() else []
        self.assertTrue(files, "缺少 CI 离线门禁工作流（SR-6）")
        blob = "\n".join(_read(f) for f in files)
        self.assertIn("tests/run_tests.py", blob)
        self.assertNotIn("allow-live", blob)


if __name__ == "__main__":
    unittest.main()
