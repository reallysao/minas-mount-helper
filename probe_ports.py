#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] 探测本机 P2P 隧道候选端口上的 NAS WebDAV。

安全说明：
- 本脚本会连接真实隧道并使用真实凭证，不属于自动化测试，默认不运行。
- 不内置任何账号/密码/私钥；凭证只从环境变量读取，且必须显式加 --allow-live。
- 默认 pytest / tests/run_tests.py 不会收集本文件。

环境变量：
  MINAS_TEST_USER      WebDAV 用户名
  MINAS_TEST_PASSWORD WebDAV 密码
  MINAS_TEST_CERT_DIR 官方 App 客户端证书目录（可选；需要 mTLS 探测时提供）

用法：
  MINAS_TEST_USER=.. MINAS_TEST_PASSWORD=.. python3 probe_ports.py --allow-live 49559 49560
"""
import argparse
import base64
import os
import ssl
import sys
import urllib.error
import urllib.request


def _load_config():
    user = os.environ.get("MINAS_TEST_USER", "")
    pwd = os.environ.get("MINAS_TEST_PASSWORD", "")
    cert_dir = os.environ.get("MINAS_TEST_CERT_DIR", "")
    return user, pwd, cert_dir


def make_ctx(cert_dir):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 历史探针：保持只读探测语义；不做服务器证书校验（与应用数据面的 fail-closed 要求无关）。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if cert_dir and os.path.isdir(cert_dir):
        for name in os.listdir(cert_dir):
            if name.endswith("_cert.pem"):
                key = os.path.join(cert_dir, name.replace("_cert.pem", "_private_key.pem"))
                ca = os.path.join(cert_dir, "ca_chain.pem")
                if os.path.exists(key):
                    ctx.load_cert_chain(certfile=os.path.join(cert_dir, name), keyfile=key)
                if os.path.exists(ca):
                    ctx.load_verify_locations(cafile=ca)
                break
    return ctx


def probe(port, user, pwd, cert_dir, path="/", method="OPTIONS"):
    ctx = make_ctx(cert_dir)
    url = f"https://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    if user and pwd:
        auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=6) as resp:
            headers = dict(resp.headers)
            print(f"[{port}] {method} {path} -> HTTP {resp.status}")
            for k in ("Server", "Allow", "DAV", "Content-Type", "WWW-Authenticate"):
                if k in headers:
                    print(f"    {k}: {headers[k]}")
            return resp.status
    except urllib.error.HTTPError as e:
        print(f"[{port}] {method} {path} -> HTTP {e.code} | "
              f"Server={e.headers.get('Server')} | Allow={e.headers.get('Allow')} | DAV={e.headers.get('DAV')}")
        return e.code
    except Exception as e:
        print(f"[{port}] {method} {path} -> FAIL {type(e).__name__}: {str(e)[:120]}")
        return None


def main():
    ap = argparse.ArgumentParser(description="历史人工端口探针（默认拒绝，需 --allow-live 与环境凭证）")
    ap.add_argument("ports", nargs="*", type=int, help="要探测的本地端口")
    ap.add_argument("--allow-live", action="store_true",
                    help="显式确认连接真实隧道并使用环境凭证；缺省拒绝执行")
    args = ap.parse_args()

    user, pwd, cert_dir = _load_config()
    if not args.allow_live or not (user and pwd):
        print("拒绝执行：本探针会连接真实隧道。请加 --allow-live，并通过 "
              "MINAS_TEST_USER / MINAS_TEST_PASSWORD 提供凭证。", file=sys.stderr)
        sys.exit(2)
    if not args.ports:
        print("用法：probe_ports.py --allow-live <端口> [更多端口]", file=sys.stderr)
        sys.exit(2)
    for p in args.ports:
        probe(p, user, pwd, cert_dir)


if __name__ == "__main__":
    main()
