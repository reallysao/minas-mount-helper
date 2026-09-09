#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测本地 P2P 隧道各端口，寻找 NAS WebDAV(5000) 映射"""
import ssl, base64, sys, urllib.request

CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
CERT = f"{CERTDIR}/2855650711_1191211642_cert.pem"
KEY = f"{CERTDIR}/2855650711_1191211642_private_key.pem"
CA = f"{CERTDIR}/ca_chain.pem"
USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"

def make_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
    ctx.load_verify_locations(cafile=CA)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

def probe(port, path="/", method="OPTIONS"):
    ctx = make_ctx()
    url = f"https://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, method=method)
    auth = base64.b64encode(f"{USER}:{PWD}".encode()).decode()
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
        print(f"[{port}] {method} {path} -> HTTP {e.code} | Server={e.headers.get('Server')} | Allow={e.headers.get('Allow')} | DAV={e.headers.get('DAV')}")
        return e.code
    except Exception as e:
        print(f"[{port}] {method} {path} -> FAIL {type(e).__name__}: {str(e)[:120]}")
        return None

if __name__ == "__main__":
    ports = [int(x) for x in sys.argv[1:]] or [49558, 49559, 49560, 49561, 49562, 49563, 49564, 49565, 25445]
    for p in ports:
        probe(p)
