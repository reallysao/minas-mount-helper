#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] 通过 P2P 隧道端口调 NAS get_pool_info 接口。
安全：不内置证书文件名；证书目录来自环境变量；必须 --allow-live；默认 pytest 不收集。
环境：MINAS_TEST_CERT_DIR（官方 App 客户端证书目录）/ MINAS_TEST_PORT(可选)
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request
import urllib.error


def _live_guard():
    ap = argparse.ArgumentParser()
    ap.add_argument("ports", nargs="*", type=int)
    ap.add_argument("--allow-live", action="store_true")
    args = ap.parse_args()
    cert_dir = os.environ.get("MINAS_TEST_CERT_DIR", "")
    if not args.allow_live or not cert_dir or not os.path.isdir(cert_dir):
        print("拒绝执行：加 --allow-live 并设置 MINAS_TEST_CERT_DIR 指向官方证书目录。",
              file=sys.stderr)
        sys.exit(2)
    return args.ports or [int(x) for x in os.environ.get("MINAS_TEST_PORT", "").split() if x.isdigit()], cert_dir


def test(port, cert_dir, host="127.0.0.1", path="/cgi-bin/luci/filemgr/get_pool_info",
         method="POST", body=b"{}"):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cert = key = None
    for name in os.listdir(cert_dir):
        if name.endswith("_cert.pem"):
            cert = os.path.join(cert_dir, name)
            key = os.path.join(cert_dir, name.replace("_cert.pem", "_private_key.pem"))
    ca = os.path.join(cert_dir, "ca_chain.pem")
    if cert and key and os.path.exists(key):
        ctx.load_cert_chain(certfile=cert, keyfile=key)
    if os.path.exists(ca):
        ctx.load_verify_locations(cafile=ca)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 历史只读探针，非应用数据面策略
    req = urllib.request.Request(f"https://{host}:{port}{path}", data=body, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{port}] HTTP {resp.status} len={len(data)}")
            print(data.decode("utf-8", "replace")[:800])
            return True
    except urllib.error.HTTPError as e:
        print(f"[{port}] HTTP {e.code}")
        return False
    except Exception as e:
        print(f"[{port}] FAIL: {e}")
        return False


if __name__ == "__main__":
    ports, cert_dir = _live_guard()
    if not ports:
        print("用法：test_pool_info.py --allow-live <端口> [更多端口]", file=sys.stderr)
        sys.exit(2)
    for p in ports:
        test(p, cert_dir)
