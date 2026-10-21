#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享上游目标状态存储（schema v2）。

职责边界（与运行健康严格分离）：
- 落盘内容只有「上游目标配置」：host/port/base/username/password/tls/generation/published_at。
- ``connected`` / ``last_error`` / TLS 闭锁等运行健康状态**永不落盘**，由各进程内存持有。
- ``load_snapshot`` 是纯读：不存在返回 None，损坏抛 StateFormatError，绝不自愈、绝不回写。
- ``publish_target`` 是唯一写入口：同目录临时文件 + fsync + os.replace 原子发布，
  文件 0600、目录 0700，generation 单调递增。

本模块由 proxy owner 独占维护；client owner 只消费读接口与 publish_target。
"""
import dataclasses
import fcntl
import json
import os
import tempfile
import threading
import time

SCHEMA_VERSION = 2

# 进程内协调锁；跨进程/跨线程的 CAS 原子性由 lockfile 的 flock 保证。
_write_lock = threading.RLock()

_FALLBACK_STATE_FILE = os.path.expanduser(
    "~/Library/Application Support/小米智能存储挂载助手/proxy_state.json")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_BASE = "/pool0/data"


class StateFormatError(Exception):
    """状态文件存在但结构非法/类型错误。读接口抛出，绝不自动修复或回写。"""


class GenerationConflictError(Exception):
    """CAS 发布时 expected_generation 与文件内 generation 不一致。"""


def default_state_path() -> str:
    # 惰性读取 MWDAV_STATE：允许在模块导入后由调用方/测试指定状态文件，
    # 避免导入期固化路径导致发布写到别处。
    return os.environ.get("MWDAV_STATE", _FALLBACK_STATE_FILE)


def _as_str(value, default=""):
    return value if isinstance(value, str) else default


@dataclasses.dataclass(frozen=True)
class TlsTarget:
    """上游 TLS 目标配置。server_name 必须来自用户/受信配置，
    禁止从未经验证的对端证书中自动学习身份。"""
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    server_name: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TlsTarget":
        if not isinstance(data, dict):
            return cls()
        return cls(
            ca_file=_as_str(data.get("ca_file")) or None,
            cert_file=_as_str(data.get("cert_file")) or None,
            key_file=_as_str(data.get("key_file")) or None,
            server_name=_as_str(data.get("server_name")) or None,
        )


@dataclasses.dataclass(frozen=True)
class TargetSnapshot:
    """不可变上游目标快照。消费方持有副本，读路径不得据此回写。"""
    schema_version: int = SCHEMA_VERSION
    generation: int = 0
    published_at: float = 0.0
    host: str = DEFAULT_HOST
    port: int = 0
    base: str = DEFAULT_BASE
    username: str = ""
    password: str = ""
    tls: TlsTarget = dataclasses.field(default_factory=TlsTarget)

    def is_complete(self) -> bool:
        """目标是否配齐（端口 + 凭证齐备）。"""
        return (isinstance(self.port, int) and 1 <= self.port <= 65535
                and bool(self.username) and bool(self.password))

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["tls"] = self.tls.to_dict()
        return d


def _validate(data: dict) -> TargetSnapshot:
    if not isinstance(data, dict):
        raise StateFormatError("state root must be a JSON object")
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise StateFormatError("port must be an integer in 1..65535")
    host = _as_str(data.get("host"), DEFAULT_HOST) or DEFAULT_HOST
    base = _as_str(data.get("base"), DEFAULT_BASE) or DEFAULT_BASE
    username = _as_str(data.get("username"))
    password = _as_str(data.get("password"))
    generation = data.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        generation = 0
    published_at = data.get("published_at")
    if not isinstance(published_at, (int, float)) or isinstance(published_at, bool):
        published_at = 0.0
    return TargetSnapshot(
        schema_version=SCHEMA_VERSION,
        generation=generation,
        published_at=float(published_at),
        host=host,
        port=port,
        base=base,
        username=username,
        password=password,
        tls=TlsTarget.from_dict(data.get("tls")),
    )


def load_snapshot(path: str | None = None) -> TargetSnapshot | None:
    """纯读共享状态文件。

    - 文件不存在 → None（未配置，不是错误）。
    - 文件损坏/类型错误 → StateFormatError（调用方按未配置处理或报错，不得回写）。
    - 本函数不执行任何写操作，不修改文件，不递增版本。
    """
    path = path or default_state_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        raise
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StateFormatError(f"invalid JSON: {e}") from e
    return _validate(data)


def read_legacy(path: str | None = None) -> dict:
    """兼容 conn_manager 旧读取：只取 {username,password,port}，纯读。

    返回 {"username": str|None, "password": str|None, "port": int|None}。
    """
    snap = load_snapshot(path)
    if snap is None:
        return {"username": None, "password": None, "port": None}
    return {
        "username": snap.username or None,
        "password": snap.password or None,
        "port": snap.port or None,
    }


def _atomic_write(path: str, payload: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".proxy_state.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def publish_target(
    path: str | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int,
    username: str = "",
    password: str = "",
    base: str = DEFAULT_BASE,
    tls: TlsTarget | None = None,
    expected_generation: int | None = None,
) -> TargetSnapshot:
    """单写者原子发布上游目标。

    - generation 在现有文件基础上单调 +1；``expected_generation`` 提供可选 CAS 校验。
    - 只写目标配置；不写 connected/error 等健康字段。
    """
    path = path or default_state_path()
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        raise StateFormatError("port must be an integer in 1..65535")

    tls = tls or TlsTarget()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)

    # CAS 必须在同一把锁内完成「重读 → 比对 → 写」，否则两写者可同读同一代次、
    # 双双通过比对并覆盖写（os.replace 只保证单文件替换原子，不保证 CAS 原子）。
    # flock 跨线程/跨进程互斥；进程内 RLock 减少重复 flock 争用。
    lock_path = path + ".lock"
    with _write_lock:
        with open(lock_path, "a+") as lf:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                # 损坏文件直接抛出，绝不静默当成 current=None 把代次回滚到 0。
                current = load_snapshot(path)
                current_gen = current.generation if current is not None else 0
                if expected_generation is not None and current_gen != expected_generation:
                    raise GenerationConflictError(
                        f"expected generation {expected_generation}, found {current_gen}")

                # 同值发布去重：身份（host/port/base/凭证/TLS）完全一致则不落盘、不 bump。
                if current is not None and (
                    current.host == host
                    and current.port == port
                    and current.base == base
                    and current.username == username
                    and current.password == password
                    and current.tls == tls
                ):
                    return current

                snap = TargetSnapshot(
                    schema_version=SCHEMA_VERSION,
                    generation=current_gen + 1,
                    published_at=time.time(),
                    host=host,
                    port=port,
                    base=base,
                    username=username,
                    password=password,
                    tls=tls,
                )
                _atomic_write(path, snap.to_dict())
                return snap
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
