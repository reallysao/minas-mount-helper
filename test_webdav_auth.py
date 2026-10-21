#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] 验证 WebDAV 是否需要客户端证书 / Basic 认证。

安全：不内置账号密码；凭证来自环境变量，必须显式 --allow-live；默认 pytest 不收集本文件。
环境：MINAS_TEST_USER / MINAS_TEST_PASSWORD / MINAS_TEST_CERT_DIR(可选) / MINAS_TEST_PORT(可选)
"""
import argparse
import base64
import os
import ssl
import sys
import urllib.error
import urllib.request


def _live_guard():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-live", action="store_true")
    a, _ = ap.parse_known_args()
    user = os.environ.get("MINAS_TEST_USER", "")
    pwd = os.environ.get("MINAS_TEST_PASSWORD", "")
    if not a.allow_live or not (user and pwd):
        print("拒绝执行：本探针连接真实隧道。加 --allow-live 并设置 "
              "MINAS_TEST_USER/MINAS_TEST_PASSWORD。", file=sys.stderr)
        sys.exit(2)
    port = int(os.environ.get("MINAS_TEST_PORT", "49559"))
    return user, pwd, os.environ.get("MINAS_TEST_CERT_DIR", ""), port


def _ctx(use_client_cert, cert_dir):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if use_client_cert and cert_dir and os.path.isdir(cert_dir):
        for name in os.listdir(cert_dir):
            if name.endswith("_cert.pem"):
                key = os.path.join(cert_dir, name.replace("_cert.pem", "_private_key.pem"))
                if os.path.exists(key):
                    ctx.load_cert_chain(certfile=os.path.join(cert_dir, name), keyfile=key)
                break
    ca = os.path.join(cert_dir, "ca_chain.pem") if cert_dir else ""
    if ca and os.path.exists(ca):
        ctx.load_verify_locations(cafile=ca)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def test(name, user, pwd, cert_dir, port, use_client_cert, use_auth,
         path="/", method="PROPFIND", body=None, headers_extra=None):
    ctx = _ctx(use_client_cert, cert_dir)
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method, data=body)
    if use_auth:
        auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/xml")
    for k, v in (headers_extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{name}] HTTP {resp.status} len={len(data)}")
            return data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"[{name}] HTTP {e.code} | WWW-Authenticate={e.headers.get('WWW-Authenticate')}")
        return None
    except Exception as e:
        print(f"[{name}] FAIL {type(e).__name__}: {str(e)[:150]}")
        return None


if __name__ == "__main__":
    user, pwd, cert_dir, port = _live_guard()
    print(test("有证书+Basic", user, pwd, cert_dir, port, True, True))
    print(test("无证书+Basic", user, pwd, cert_dir, port, False, True))
    print(test("无认证", user, pwd, cert_dir, port, False, False))
    propfind = (b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
                b'<D:prop><D:displayname/><D:getcontentlength/>'
                b'<D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>')
    r = test("PROPFIND /", user, pwd, cert_dir, port, True, True, body=propfind)
    print((r or "")[:1200])
