#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] Finder 挂载场景（无客户端证书/无 SNI/Basic）WebDAV 全操作验证。

警告：本探针会真实 PUT/DELETE 远端测试文件，必须同时提供：
  --allow-live            连接真实隧道；
  --allow-destructive     允许写/删操作（仅 --allow-live 时只读 OPTIONS/PROPFIND/GET）。
凭证来自环境变量，不内置；默认 pytest 不收集本文件。
环境：MINAS_TEST_USER / MINAS_TEST_PASSWORD / MINAS_TEST_CERT_DIR(可选) / MINAS_TEST_PORT(可选)
"""
import argparse
import base64
import http.client
import os
import socket
import ssl
import sys

BASE = "/pool0/data"
PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:">'
            b'<D:prop><D:displayname/><D:getcontentlength/>'
            b'<D:getlastmodified/><D:resourcetype/><D:getcontenttype/></D:prop></D:propfind>')


class RawConn(http.client.HTTPSConnection):
    def __init__(self, host, port, cert=True, **kw):
        super().__init__(host, port, **kw)
        self._cert = cert

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cert_dir = os.environ.get("MINAS_TEST_CERT_DIR", "")
        if self._cert and cert_dir and os.path.isdir(cert_dir):
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
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(10)


def req(name, user, pwd, port, method, path, body=None, cert=True, auth=True, depth="1"):
    conn = RawConn("127.0.0.1", port, cert=cert)
    try:
        h = {}
        if auth:
            h["Authorization"] = f"Basic {base64.b64encode(f'{user}:{pwd}'.encode()).decode()}"
        if body is not None:
            h["Content-Type"] = "application/xml"
            h["Depth"] = depth
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        print(f"[{name}] {method} {path} -> HTTP {resp.status} len={len(data)}")
        return resp, data
    except Exception as e:
        print(f"[{name}] {method} {path} -> FAIL {type(e).__name__}: {str(e)[:120]}")
        return None, b""
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-live", action="store_true")
    ap.add_argument("--allow-destructive", action="store_true")
    args = ap.parse_args()
    user = os.environ.get("MINAS_TEST_USER", "")
    pwd = os.environ.get("MINAS_TEST_PASSWORD", "")
    port = int(os.environ.get("MINAS_TEST_PORT", "49559"))
    if not args.allow_live or not (user and pwd):
        print("拒绝执行：加 --allow-live 并设置 MINAS_TEST_USER/MINAS_TEST_PASSWORD。",
              file=sys.stderr)
        sys.exit(2)

    r, d = req("无证书PROPFIND", user, pwd, port, "PROPFIND", BASE, PROPFIND, cert=False)
    if d:
        print(d[:600].decode("utf-8", "replace"))

    if not args.allow_destructive:
        print("\n未加 --allow-destructive：跳过 PUT/MKCOL/DELETE 等写操作。")
        sys.exit(0)

    r, d = req("无证书PUT", user, pwd, port, "PUT", f"{BASE}/.finder_upload_test.txt",
               "Finder mount test - 中文内容测试".encode("utf-8"), cert=False)
    r, d = req("无证书GET", user, pwd, port, "GET", f"{BASE}/.finder_upload_test.txt", cert=False)
    if r:
        print("   内容:", d.decode("utf-8", "replace"))
    req("无证书MKCOL", user, pwd, port, "MKCOL", f"{BASE}/.finder_test_dir", cert=False)
    r, d = req("无证书PROPFIND新目录", user, pwd, port, "PROPFIND",
               f"{BASE}/.finder_test_dir", PROPFIND, cert=False)
    if d:
        print(d[:300].decode("utf-8", "replace"))
    req("无证书DELETE目录", user, pwd, port, "DELETE", f"{BASE}/.finder_test_dir", cert=False)
    req("无证书DELETE文件", user, pwd, port, "DELETE", f"{BASE}/.finder_upload_test.txt", cert=False)
    print("\n全部测试完成")
