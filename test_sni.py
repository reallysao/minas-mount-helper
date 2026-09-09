#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 SNI=设备CN 时 PROPFIND 是否通过（模拟 Finder 的 TLS 行为）"""
import ssl, base64, socket, http.client

USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"
CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
CN = "nas.2855650711.6c9e038a39ce0552950c0bc665f5428689c61f6a.2"
PORT = 49559
PROPFIND = b'<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:getlastmodified/><D:resourcetype/><D:getcontenttype/></D:prop></D:propfind>'

class SNIConnection(http.client.HTTPSConnection):
    """连接 127.0.0.1 但 TLS SNI 用设备 CN"""
    def __init__(self, host, port, sni, **kw):
        super().__init__(host, port, **kw)
        self._sni = sni
    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(certfile=f"{CERTDIR}/2855650711_1191211642_cert.pem",
                            keyfile=f"{CERTDIR}/2855650711_1191211642_private_key.pem")
        ctx.load_verify_locations(cafile=f"{CERTDIR}/ca_chain.pem")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(sock, server_hostname=self._sni)
        self.sock.settimeout(10)

def run(sni, host_header, method, path, body=None, extra_headers=None):
    conn = SNIConnection("127.0.0.1", PORT, sni)
    try:
        headers = {"Authorization": f"Basic {base64.b64encode(f'{USER}:{PWD}'.encode()).decode()}"}
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
        print(f"[SNI={sni[:40]}... Host={host_header or '-'}] {method} {path} -> HTTP {resp.status} len={len(data)}")
        return data.decode("utf-8", "replace")
    except Exception as e:
        print(f"[SNI={sni[:40]}...] {method} {path} -> FAIL {type(e).__name__}: {str(e)[:150]}")
        return None
    finally:
        conn.close()

if __name__ == "__main__":
    print("== 1. SNI=CN, Host=CN, PROPFIND / ==")
    print(run(CN, CN, "PROPFIND", "/", PROPFIND)[:2000])
    print()
    print("== 2. SNI=CN, Host=CN, PROPFIND /home/u2855650711 ==")
    print(run(CN, CN, "PROPFIND", "/home/u2855650711", PROPFIND)[:2000])
