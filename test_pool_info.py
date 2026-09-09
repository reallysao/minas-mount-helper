#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试通过 P2P 隧道端口调用 NAS get_pool_info 接口"""
import ssl, json, sys, urllib.request

CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
CERT = f"{CERTDIR}/2855650711_1191211642_cert.pem"
KEY = f"{CERTDIR}/2855650711_1191211642_private_key.pem"
CA = f"{CERTDIR}/ca_chain.pem"

def test(port, host="127.0.0.1", path="/cgi-bin/luci/filemgr/get_pool_info", method="POST", body=b"{}"):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
    ctx.load_verify_locations(cafile=CA)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # 本地隧道，跳过主机名校验
    url = f"https://{host}:{port}{path}"
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{port}] HTTP {resp.status} len={len(data)}")
            print(data.decode("utf-8", "replace")[:800])
            return True
    except Exception as e:
        print(f"[{port}] FAIL: {e}")
        return False

if __name__ == "__main__":
    ports = [int(x) for x in sys.argv[1:]] or [49558, 49559, 49560, 49562]
    for p in ports:
        test(p)
