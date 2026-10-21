#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[历史人工探针 · 默认拒绝执行] 验证 SNI=设备 CN 时 PROPFIND 是否通过（模拟 Finder TLS 行为）。
安全：不内置账号密码/设备 CN；环境变量 + --allow-live；默认 pytest 不收集。
环境：MINAS_TEST_USER / MINAS_TEST_PASSWORD / MINAS_TEST_CERT_DIR /
      MINAS_TEST_DEVICE_CN(可选 SNI) / MINAS_TEST_PORT(可选)
"""
import argparse
import base64
import http.client
import os
import socket
import ssl
import sys

PROPFIND = (b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:">'
            b'<D:prop><D:displayname/><D:getcontentlength/>'
            b'<D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>')


def _live_guard():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-live", action="store_true")
    a, _ = ap.parse_known_args()
    user = os.environ.get("MINAS_TEST_USER", "")
    pwd = os.environ.get("MINAS_TEST_PASSWORD", "")
    cn = os.environ.get("MINAS_TEST_DEVICE_CN", "")
    if not a.allow_live or not (user and pwd and cn):
        print("拒绝执行：加 --allow-live，并设置 MINAS_TEST_USER/PASSWORD/DEVICE_CN。",
              file=sys.stderr)
        sys.exit(2)
    return user, pwd, os.environ.get("MINAS_TEST_CERT_DIR", ""), cn, int(os.environ.get("MINAS_TEST_PORT", "49559"))


class SNIConnection(http.client.HTTPSConnection):
    """连接 127.0.0.1 但 TLS SNI 用设备 CN"""
    def __init__(self, host, port, sni, **kw):
        super().__init__(host, port, **kw)
        self._sni = sni

    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cert_dir = os.environ.get("MINAS_TEST_CERT_DIR", "")
        if cert_dir and os.path.isdir(cert_dir):
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
        self.sock = ctx.wrap_socket(sock, server_hostname=self._sni)
        self.sock.settimeout(10)


def run(user, pwd, sni, host_header, method, path, body=None, extra_headers=None):
    conn = SNIConnection("127.0.0.1", int(os.environ.get("MINAS_TEST_PORT", "49559")), sni)
    try:
        headers = {"Authorization": f"Basic {base64.b64encode(f'{user}:{pwd}'.encode()).decode()}"}
        if body:
            headers["Content-Type"] = "application/xml"
            headers["Depth"] = "1"
        if host_header:
            headers["Host"] = host_header
        if extra_headers:
            headers.update(extra_headers)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        print(f"[SNI={sni[:40]}... Host={host_header or '-'}] {method} {path} "
              f"-> HTTP {resp.status} len={len(data)}")
        return data.decode("utf-8", "replace")
    except Exception as e:
        print(f"[SNI={sni[:40]}...] {method} {path} -> FAIL {type(e).__name__}: {str(e)[:150]}")
        return None
    finally:
        conn.close()


if __name__ == "__main__":
    user, pwd, _cert_dir, cn, _port = _live_guard()
    print("== 1. SNI=CN, Host=CN, PROPFIND / ==")
    print((run(user, pwd, cn, cn, "PROPFIND", "/", PROPFIND) or "")[:2000])
    print()
    print("== 2. SNI=CN, Host=CN, PROPFIND /home/<user> ==")
    print((run(user, pwd, cn, cn, "PROPFIND", f"/home/{user}", PROPFIND) or "")[:2000])
