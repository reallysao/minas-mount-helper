#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上游 TLS 传输配置与失败分类（fail-closed）。

硬约束：
- 始终 CERT_REQUIRED + check_hostname=True，无 CERT_NONE、无 check_hostname=False、
  无「127.0.0.1 即信任」豁免，也无显式降级开关。
- 期望身份（server_name）必须来自用户/受信配置；禁止从未验证的对端证书自动学习。
- CA / 客户端证书加载失败一律 fail closed（TlsConfigError），不吞异常。
- 错误按 config / verify / handshake 分类，供闭锁与诊断；diagnostic 不含密码/私钥内容。

本模块由 proxy owner 独占维护。
"""
import dataclasses
import os
import ssl
import time


class TlsError(Exception):
    """TLS 基础错误。code: config/verify/handshake；diagnostic 面向日志与 UI。"""

    code = "tls"

    def __init__(self, diagnostic: str):
        super().__init__(diagnostic)
        self.diagnostic = diagnostic

    def __str__(self):
        return f"[{self.code}] {self.diagnostic}"


class TlsConfigError(TlsError):
    """CA / 客户端证书 / 私钥缺失、不可读或格式错误（配置期 fail closed）。"""
    code = "config"


class TlsVerifyError(TlsError):
    """信任链校验失败或对端主机名与期望身份不匹配。"""
    code = "verify"


class TlsHandshakeError(TlsError):
    """协议层/网络层握手失败（对端非 TLS、断连、超时、拒绝连接等）。"""
    code = "handshake"


@dataclasses.dataclass(frozen=True)
class TlsConfig:
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    server_name: str | None = None  # 受信配置的期望身份（CN/SAN），禁止自动学习


def _require_file(path: str | None, label: str) -> str:
    if not path:
        raise TlsConfigError(f"缺少{label}配置（fail-closed）：未提供 {label}")
    if not isinstance(path, str) or not (os.path.isfile(path) and os.access(path, os.R_OK)):
        raise TlsConfigError(f"{label}不存在或不可读：{path}")
    return path


def build_client_context(cfg: TlsConfig) -> ssl.SSLContext:
    """构建强制校验的上游 TLS 上下文。

    - server_name 必须显式给出（SNI 与主机名校验都用它）；
    - ca_file 必须显式给出且可读（不回退系统信任库，避免私链被静默放行）；
    - 提供 cert_file 时必须同时提供 key_file，加载失败抛 TlsConfigError。
    """
    if not cfg.server_name:
        raise TlsConfigError("缺少 server_name（期望对端身份）；禁止用 127.0.0.1 冒充身份")
    ca = _require_file(cfg.ca_file, "CA 证书包")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    try:
        ctx.load_verify_locations(cafile=ca)
    except ssl.SSLError as e:
        raise TlsConfigError(f"CA 证书包加载失败：{e}") from e

    if cfg.cert_file or cfg.key_file:
        if not (cfg.cert_file and cfg.key_file):
            raise TlsConfigError("客户端证书与私钥必须同时提供")
        cert = _require_file(cfg.cert_file, "客户端证书")
        key = _require_file(cfg.key_file, "客户端私钥")
        try:
            ctx.load_cert_chain(certfile=cert, keyfile=key)
        except ssl.SSLError as e:
            raise TlsConfigError(f"客户端证书/私钥加载失败：{e}") from e
    return ctx


def classify_error(exc: BaseException) -> TlsError:
    """把底层异常归类为 TlsError（用于闭锁与诊断）。"""
    if isinstance(exc, TlsError):
        return exc
    if isinstance(exc, ssl.SSLCertVerificationError):
        detail = getattr(exc, "verify_message", None) or str(exc)
        return TlsVerifyError(f"服务端证书校验失败：{detail}")
    if isinstance(exc, ssl.SSLError):
        msg = str(exc)
        low = msg.lower()
        # 证书链/身份校验类告警 → verify。BAD_CERTIFICATE / 缺客户端证书等
        # 不并入 verify（保持 mTLS 缺证为 handshake，避免弱化类别）。
        verify_markers = (
            "unknown_ca", "certificate_unknown", "certificate_expired",
            "certificate_revoked", "certificate_verify_failed",
            "unable to get local issuer", "self-signed",
            "hostname mismatch", "certificate verify failed",
        )
        handshake_markers = (
            "bad_certificate", "peer_did_not_return_a_certificate",
            "certificate_required", "handshake_failure",
            "wrong_version_number", "version_too_new", "eof",
        )
        if any(m in low for m in verify_markers):
            return TlsVerifyError(f"服务端证书/主机名校验失败：{msg}")
        if any(m in low for m in handshake_markers):
            return TlsHandshakeError(f"对端非 TLS 或握手中断：{msg}")
        return TlsHandshakeError(f"TLS 握手失败：{msg}")
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return TlsConfigError(f"TLS 配置文件不可读：{exc}")
    if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, BrokenPipeError,
                        TimeoutError, OSError)):
        return TlsHandshakeError(f"网络层连接失败：{exc}")
    return TlsHandshakeError(f"未分类 TLS 错误：{type(exc).__name__}: {exc}")


class TlsLockout:
    """失败分类 + 冷却闭锁 + 成功复位；状态只在内存，不落盘。"""

    def __init__(self, cooldown: float = 30.0, max_failures: int = 3):
        self.cooldown = float(cooldown)
        self.max_failures = int(max_failures)
        self._failures = 0
        self._locked_until = 0.0
        self._last: TlsError | None = None

    def record_failure(self, exc: BaseException) -> TlsError:
        err = classify_error(exc)
        self._failures += 1
        self._last = err
        if self._failures >= self.max_failures:
            self._locked_until = time.time() + self.cooldown
        return err

    def record_success(self) -> None:
        self._failures = 0
        self._locked_until = 0.0
        self._last = None

    def locked(self) -> bool:
        return time.time() < self._locked_until

    def status(self) -> dict:
        return {
            "locked": self.locked(),
            "reason_class": self._last.code if self._last else None,
            "attempts": self._failures,
            "retry_after": max(0.0, self._locked_until - time.time()),
            "last_diagnostic": self._last.diagnostic if self._last else None,
        }
