#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""终极验证：Finder 挂载场景（无客户端证书、无SNI、Basic认证）下 WebDAV 各操作"""
import ssl, base64, socket, http.client, urllib.parse

USER = "u2855650711"
PWD = "=q=SUa#Wbt+y(@TKfE+Cwe%74Wbgx)-xm+b481ZitXQoLw(~_^pt8gza-LrwHp##"
PORT = 49559
BASE = "/pool0/data"
PROPFIND = b'<?xml version="1.0" encoding="utf-8"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:getlastmodified/><D:resourcetype/><D:getcontenttype/></D:prop></D:propfind>'

class RawConn(http.client.HTTPSConnection):
    def __init__(self, host, port, cert=True, **kw):
        super().__init__(host, port, **kw)
        self._cert = cert
    def connect(self):
        sock = socket.create_connection((self.host, self.port), timeout=10)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._cert:
            CERTDIR = "/Applications/小米智能存储.app/Contents/Resources/extraResources/cert"
            ctx.load_cert_chain(certfile=f"{CERTDIR}/2855650711_1191211642_cert.pem",
                                keyfile=f"{CERTDIR}/2855650711_1191211642_private_key.pem")
        ctx.load_verify_locations(cafile="/Applications/小米智能存储.app/Contents/Resources/extraResources/cert/ca_chain.pem")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self.sock.settimeout(10)

def req(name, method, path, body=None, cert=True, auth=True, depth="1"):
    conn = RawConn("127.0.0.1", PORT, cert=cert)
    try:
        h = {}
        if auth:
            h["Authorization"] = f"Basic {base64.b64encode(f'{USER}:{PWD}'.encode()).decode()}"
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
    # 1. 无证书+Basic: PROPFIND 列表
    r, d = req("无证书PROPFIND", "PROPFIND", BASE, PROPFIND, cert=False)
    if d: print(d[:600].decode("utf-8","replace"))
    print()
    # 2. 无证书: PUT 上传测试文件
    r, d = req("无证书PUT", "PUT", f"{BASE}/.finder_upload_test.txt", "Finder mount test - 中文内容测试".encode("utf-8"), cert=False)
    print()
    # 3. 无证书: GET 下载刚才的文件
    r, d = req("无证书GET", "GET", f"{BASE}/.finder_upload_test.txt", cert=False)
    if r: print("   内容:", d.decode("utf-8","replace"))
    print()
    # 4. 无证书: MKCOL 建目录
    r, d = req("无证书MKCOL", "MKCOL", f"{BASE}/.finder_test_dir", cert=False)
    print()
    # 5. 无证书: PROPFIND 新目录 (确认可写)
    r, d = req("无证书PROPFIND新目录", "PROPFIND", f"{BASE}/.finder_test_dir", PROPFIND, cert=False)
    if d: print(d[:300].decode("utf-8","replace"))
    print()
    # 6. 无证书: DELETE 测试目录
    r, d = req("无证书DELETE目录", "DELETE", f"{BASE}/.finder_test_dir", cert=False)
    # 7. 无证书: DELETE 测试文件
    r, d = req("无证书DELETE文件", "DELETE", f"{BASE}/.finder_upload_test.txt", cert=False)
    print()
    print("全部测试完成")
