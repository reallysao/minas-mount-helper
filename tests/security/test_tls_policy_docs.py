#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全回归：文档必须冻结 TLS 身份策略，且不允许任何跳过校验的降级开关。

核对：
- tls_server_name 为必填受信来源（SNI + hostname）；
- MWDAV_TLS_* 环境变量优先；
- 明确不存在 tls_verify=false / MWDAV_TLS_VERIFY 等跳过校验方案；
- 不宣称"客户端证书确保连接真正 NAS"。
"""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS = {
    "requirements": os.path.join(REPO_ROOT, "docs", "sdd", "requirements.md"),
    "acceptance": os.path.join(REPO_ROOT, "docs", "sdd", "acceptance.md"),
    "matrix": os.path.join(REPO_ROOT, "docs", "sdd", "traceability-matrix.md"),
    "threat": os.path.join(REPO_ROOT, "docs", "security", "threat-model.md"),
}


def _read(p):
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


class TlsPolicyDocsTest(unittest.TestCase):
    def test_requirements_declare_server_name_required_and_env_override(self):
        req = _read(DOCS["requirements"])
        self.assertIn("tls_server_name", req)
        self.assertIn("必填", req)
        self.assertIn("MWDAV_TLS_SERVER_NAME", req)
        self.assertIn("MWDAV_TLS_CA_FILE", req)

    def test_requirements_explicitly_forbid_verify_off(self):
        req = _read(DOCS["requirements"])
        # 必须显式声明"不存在/禁止"跳过校验开关，而非提供开关。
        self.assertIn("不存在", req)
        self.assertIn("tls_verify=false", req)
        self.assertIn("MWDAV_TLS_VERIFY", req)

    def test_no_enabling_verify_off_anywhere_in_owned_docs(self):
        blob = "\n".join(_read(p) for p in DOCS.values())
        for bad in ("MWDAV_TLS_VERIFY=0", "MWDAV_TLS_VERIFY=false",
                    "verify=False 即可", "跳过服务器校验即可"):
            self.assertNotIn(bad, blob)

    def test_internal_docs_negate_false_mtls_promise(self):
        # 内部规格应以"不得宣称"否定该说法，而不是把它当作成立的安全保证。
        req = _read(DOCS["requirements"])
        self.assertIn("不得宣称", req)
        self.assertIn("客户端认证与服务端验证是两件事", req)


if __name__ == "__main__":
    unittest.main()
