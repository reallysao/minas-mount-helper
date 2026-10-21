#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] WebDAV PROPFIND 目录列表验证。
安全：不内置账号密码；环境变量 + --allow-live；默认 pytest 不收集。
环境：MINAS_TEST_USER / MINAS_TEST_PASSWORD / MINAS_TEST_CERT_DIR(可选) / MINAS_TEST_PORT(可选)
"""
import argparse
import base64
import os
import ssl
import sys
import urllib.error
import urllib.request

PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:">'
            b'<D:prop><D:displayname/><D:getcontentlength/>'
            b'<D:getlastmodified/><D:resourcetype/><D:getcontenttype/></D:prop></D:propfind>')


def _live_guard():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-live", action="store_true")
    a, _ = ap.parse_known_args()
    user = os.environ.get("MINAS_TEST_USER", "")
    pwd = os.environ.get("MINAS_TEST_PASSWORD", "")
    if not a.allow_live or not (user and pwd):
        print("拒绝执行：加 --allow-live 并设置 MINAS_TEST_USER/MINAS_TEST_PASSWORD。",
              file=sys.stderr)
        sys.exit(2)
    return user, pwd, os.environ.get("MINAS_TEST_CERT_DIR", ""), int(os.environ.get("MINAS_TEST_PORT", "49559"))


def req(user, pwd, cert_dir, port, method, path="/", body=None, cert=True, auth=True):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cert and cert_dir and os.path.isdir(cert_dir):
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
    r = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method, data=body)
    if auth:
        r.add_header("Authorization",
                     f"Basic {base64.b64encode(f'{user}:{pwd}'.encode()).decode()}")
    if body:
        r.add_header("Content-Type", "application/xml")
        r.add_header("Depth", "1")
    try:
        with urllib.request.urlopen(r, context=ctx, timeout=10) as resp:
            data = resp.read()
            print(f"[{method} {path}] HTTP {resp.status} len={len(data)}")
            return data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"[{method} {path}] HTTP {e.code} body={e.read()[:200]}")
        return None
    except Exception as e:
        print(f"[{method} {path}] FAIL {type(e).__name__}: {str(e)[:200]}")
        return None


if __name__ == "__main__":
    user, pwd, cert_dir, port = _live_guard()
    print("== OPTIONS / ==")
    print((req(user, pwd, cert_dir, port, "OPTIONS", "/") or "")[:200])
    print("== PROPFIND / (Depth 1) ==")
    print((req(user, pwd, cert_dir, port, "PROPFIND", "/", PROPFIND) or "")[:2500])
