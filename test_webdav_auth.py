#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 WebDAV 是否需要客户端证书，以及 Basic 认证 + PROPFIND 文件列表"""
import ssl, base64, sys, urllib.request, urllib.error

USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"
PORT = 49559

def test(name, use_client_cert, use_auth, path="/", method="PROPFIND", body=None, headers_extra=None):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if use_client_cert:
        CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
        ctx.load_cert_chain(certfile=f"{CERTDIR}/2855650711_1191211642_cert.pem",
                            keyfile=f"{CERTDIR}/2855650711_1191211642_private_key.pem")
    ctx.load_verify_locations(cafile="/Applications/小米智能存储.app/Contents/Resources/extraResources/cert/ca_chain.pem")
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    url = f"https://127.0.0.1:{PORT}{path}"
    req = urllib.request.Request(url, method=method, data=body)
    if use_auth:
        auth = base64.b64encode(f"{USER}:{PWD}".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/xml")
    for k, v in (headers_extra or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
            data = resp.read()
            print(f"[{name}] HTTP {resp.status} len={len(data)}")
            return data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"[{name}] HTTP {e.code} | WWW-Authenticate={e.headers.get('WWW-Authenticate')}")
        return None
    except Exception as e:
        print(f"[{name}] FAIL {type(e).__name__}: {str(e)[:150]}")
        return None

if __name__ == "__main__":
    # 1. 有客户端证书 + Basic 认证
    print(test("有证书+Basic", True, True))
    # 2. 无客户端证书 + Basic 认证
    print(test("无证书+Basic", False, True))
    # 3. 无认证
    print(test("无认证", False, False))
    # 4. PROPFIND 根目录（WebDAV 列表）
    propfind = b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:getlastmodified/><D:resourcetype/></D:prop></D:propfind>'
    print(test("PROPFIND /", True, True, path="/", body=propfind)[:1200])
    # 5. PROPFIND /home/u2855650711 (alias_root)
    print(test("PROPFIND alias", True, True, path="/home/u2855650711", body=propfind)[:1200])
