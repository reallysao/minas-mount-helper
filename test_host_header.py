#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""调试 49559 的 451 响应：测试 Host 头/SNI 是否影响路由"""
import ssl, base64, socket, sys, urllib.request, urllib.error

USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"
CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
CN = "nas.2855650711.6c9e038a39ce0552950c0bc665f5428689c61f6a.2"
PORT = 49559

def test(name, host_header, sni, path="/", method="OPTIONS", body=None, cert=True):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cert:
        ctx.load_cert_chain(certfile=f"{CERTDIR}/2855650711_1191211642_cert.pem",
                            keyfile=f"{CERTDIR}/2855650711_1191211642_private_key.pem")
    ctx.load_verify_locations(cafile=f"{CERTDIR}/ca_chain.pem")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["http/1.1"])
    url = f"https://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url, method=method, data=body)
    auth = base64.b64encode(f"{USER}:{PWD}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/xml")
    if host_header:
        req.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{name}] HTTP {resp.status} len={len(data)}")
            print(f"    Server={resp.headers.get('Server')} DAV={resp.headers.get('DAV')} Allow={resp.headers.get('Allow')}")
            print(f"    Body: {data.decode('utf-8','replace')[:300]}")
            return data
    except urllib.error.HTTPError as e:
        data = e.read() if hasattr(e, 'read') else b""
        print(f"[{name}] HTTP {e.code} | Server={e.headers.get('Server')} | Body: {data.decode('utf-8','replace')[:300]}")
        return None
    except Exception as e:
        print(f"[{name}] FAIL {type(e).__name__}: {str(e)[:150]}")
        return None

if __name__ == "__main__":
    propfind = b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>'
    test("无Host无SNI(默认)", None, None, method="PROPFIND", body=propfind)
    test("Host=CN", CN, None, method="PROPFIND", body=propfind)
    test("Host=127.0.0.1", "127.0.0.1", None, method="PROPFIND", body=propfind)
    test("Host=minas", "minas", None, method="PROPFIND", body=propfind)
