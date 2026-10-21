#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档守卫：用户面向文档不得保留已被否定的安全/功能宣称。

核对：
- 不得出现旧的 mTLS 绝对化承诺句；
- 必须如实声明 sync_engine 未接入、GUI 退出后常驻服务继续、历史 ZIP 漂移；
- 不得把客户端证书说成"确保连接真正 NAS"。
"""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USER_DOCS = [
    os.path.join(REPO_ROOT, "README.md"),
    os.path.join(REPO_ROOT, "使用说明.md"),
]

# 仅禁止肯定语气的原始宣传整句；否定/澄清语境下复述该说法不算违规。
FORBIDDEN_PHRASES = [
    "客户端证书（Ed25519）双向认证，确保连接的是真正的 NAS",
]


def _read(p):
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


class UserDocsHonestyTest(unittest.TestCase):
    def test_no_forbidden_mtls_promise_in_user_docs(self):
        for p in USER_DOCS:
            text = _read(p)
            for phrase in FORBIDDEN_PHRASES:
                self.assertNotIn(phrase, text, f"{os.path.basename(p)} 含已否定宣称: {phrase}")

    def test_user_docs_declare_sync_not_wired(self):
        for p in USER_DOCS:
            text = _read(p)
            self.assertTrue(
                ("未接入" in text and "sync_engine" in text) or "同步面板" in text and "不可用" in text,
                f"{os.path.basename(p)} 未声明 sync_engine 未接入",
            )

    def test_user_docs_declare_gui_exit_keeps_resident_service(self):
        blob = "\n".join(_read(p) for p in USER_DOCS)
        self.assertIn("GUI 窗口关闭只断开 IPC", blob)
        self.assertIn("常驻服务继续", blob)

    def test_user_docs_declare_zip_drift(self):
        blob = "\n".join(_read(p) for p in USER_DOCS)
        self.assertIn("漂移", blob)


if __name__ == "__main__":
    unittest.main()
