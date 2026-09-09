#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WebDAV PROPFIND 目录列表验证"""
import ssl, base64, urllib.request, urllib.error

USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"
CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
PORT = 49559
PROPFIND = b'<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:getlastmodified/><D:resourcetype/><D:getcontenttype/></D:prop></D:propfind>'

def req(method, path="/", body=None, cert=True, auth=True):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cert:
        ctx.load_cert_chain(certfile=f"{CERTDIR}/2855650711_1191211642_cert.pem",
                            keyfile=f"{CERTDIR}/2855650711_1191211642_private_key.pem")
    ctx.load_verify_locations(cafile=f"{CERTDIR}/ca_chain.pem")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    r = urllib.request.Request(f"https://127.0.0.1:{PORT}{path}", method=method, data=body)
    if auth:
        r.add_header("Authorization", f"Basic {base64.b64encode(f'{USER}:{PWD}'.encode()).decode()}")
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
    print("== OPTIONS / ==")
    print(req("OPTIONS", "/", None)[:200])
    print("== PROPFIND / (Depth 1) ==")
    print(req("PROPFIND", "/", PROPFIND)[:2500])
