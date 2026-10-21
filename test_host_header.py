#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] 调试某隧道端口的 451：测试 Host 头/SNI 是否影响路由。
安全：不内置账号密码/设备 CN；环境变量 + --allow-live；默认 pytest 不收集。
环境：MINAS_TEST_USER / MINAS_TEST_PASSWORD / MINAS_TEST_CERT_DIR /
      MINAS_TEST_DEVICE_CN(可选) / MINAS_TEST_PORT(可选)
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
        print("拒绝执行：加 --allow-live 并设置 MINAS_TEST_USER/MINAS_TEST_PASSWORD。",
              file=sys.stderr)
        sys.exit(2)
    return user, pwd, os.environ.get("MINAS_TEST_CERT_DIR", ""), int(os.environ.get("MINAS_TEST_PORT", "49559"))


def test(name, user, pwd, cert_dir, port, host_header, path="/", method="OPTIONS", body=None, cert=True):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cert and cert_dir and os.path.isdir(cert_dir):
        for cname in os.listdir(cert_dir):
            if cname.endswith("_cert.pem"):
                key = os.path.join(cert_dir, cname.replace("_cert.pem", "_private_key.pem"))
                if os.path.exists(key):
                    ctx.load_cert_chain(certfile=os.path.join(cert_dir, cname), keyfile=key)
                break
    ca = os.path.join(cert_dir, "ca_chain.pem") if cert_dir else ""
    if ca and os.path.exists(ca):
        ctx.load_verify_locations(cafile=ca)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method, data=body)
    req.add_header("Authorization", f"Basic {base64.b64encode(f'{user}:{pwd}'.encode()).decode()}")
    req.add_header("Content-Type", "application/xml")
    if host_header:
        req.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{name}] HTTP {resp.status} len={len(data)}")
            print(f"    Server={resp.headers.get('Server')} DAV={resp.headers.get('DAV')} "
                  f"Allow={resp.headers.get('Allow')}")
            print(f"    Body: {data.decode('utf-8','replace')[:300]}")
            return data
    except urllib.error.HTTPError as e:
        data = e.read() if hasattr(e, "read") else b""
        print(f"[{name}] HTTP {e.code} | Server={e.headers.get('Server')} | "
              f"Body: {data.decode('utf-8','replace')[:300]}")
        return None
    except Exception as e:
        print(f"[{name}] FAIL {type(e).__name__}: {str(e)[:150]}")
        return None


if __name__ == "__main__":
    user, pwd, cert_dir, port = _live_guard()
    cn = os.environ.get("MINAS_TEST_DEVICE_CN", "")
    propfind = (b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
                b'<D:prop><D:displayname/><D:getcontentlength/>'
                b'<D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>')
    test("无Host无SNI(默认)", user, pwd, cert_dir, port, None, method="PROPFIND", body=propfind)
    if cn:
        test("Host=CN", user, pwd, cert_dir, port, cn, method="PROPFIND", body=propfind)
    test("Host=127.0.0.1", user, pwd, cert_dir, port, "127.0.0.1", method="PROPFIND", body=propfind)
    test("Host=minas", user, pwd, cert_dir, port, "minas", method="PROPFIND", body=propfind)
