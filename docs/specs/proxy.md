# WebDAV 代理修复规格（mwdav_proxy / state_store / tls_transport）

状态：草案 v1（规格先行，实现紧随其后；API 冻结后无通知不得破坏）
负责：proxy owner（本文件与 `mountapp/mwdav_proxy.py`、`mountapp/state_store.py`、`mountapp/tls_transport.py`、`tests/test_proxy*.py`、`tests/test_state*.py`、`tests/test_tls*.py` 独占）
不在本范围：`app_core.py` / `conn_manager.py` / `main.py` / `netfs_mount*`（client owner）、其余文档与 CI（docs owner）。

## 0. 设计原则

1. **配置（target）与运行健康（health）分离**：落盘的只有"上游目标配置"；`connected` / `last_error` / TLS 闭锁状态是运行期健康，只在内存，永不由读路径回写。
2. **读零磁盘写副作用**：`load_snapshot` 纯读，不落盘、不递增文件 generation、不调用发布；读到新目标可在内存采纳并 bump 本地连接代次（内存更新不算写副作用）。常驻 **service 进程是唯一写者**，经 `publish_target(expected_generation=...)` CAS 发布并持久化；GUI 只经 owner-only Unix IPC 发命令，不直接写状态文件。
3. **TLS fail-closed，绝无降级**：始终 `CERT_REQUIRED` + `check_hostname=True`；禁止 `CERT_NONE`、禁止 `check_hostname=False`、禁止"回环即信任"、无 `verify=false`、无 insecure 豁免开关。
4. **不以安全名义无提示破坏 Finder**：对齐 README 已承诺的"入站 Basic 认证、无凭证 401"；NetFS 不携带凭证则明确失败并列为手工验收项，不以匿名/弱认证替代兼容；返回标准挑战（`WWW-Authenticate`）与明确错误码，不静默吞掉请求。
5. **资源有界**：请求体、响应体、缓存条目、并发、排队均有独立硬上限；超限拒绝并正确关闭/排空连接，禁止把超限请求以空体转发上游。
6. **安全测试**：全部使用本地 loopback + 临时目录 + 进程内生成的自签 CA/证书；不访问真实 NAS、不使用真实凭证、不执行挂载、不调用仓库内既有联网探针。

## 1. 缺陷条目（A01–A06、A11–A14）与修复要求

| 编号 | 缺陷（基线复现） | 修复要求 |
|---|---|---|
| A01 | 入站认证被移除，匿名本机请求被转发并由代理注入上游凭证（T01） | 入站**始终 require**（无匿名开关、无降级模式）：目标已配凭证时，入站 Basic 必须与目标一致，否则 `401 + WWW-Authenticate: Basic`；目标未配齐时返回 `503`（未配置），**绝不匿名转发**。NetFS 不携带凭证则明确失败，列为手工验收未完成，不用匿名/弱认证替代兼容 |
| A02 | 超 `MAX_BODY_SIZE` 的 PUT 被改成 `body=None` 空转发，且未消费体破坏 keep-alive 边界（T02） | 超限 → `413`，不读满内存、不转发、连接 `close` 并受限排空；`Transfer-Encoding: chunked` 在写方法上显式 `501` 拒绝并关闭；声明长度与实际读取不一致时中止，禁止空体转发 |
| A03 | 配置完全相同的读取也触发 `update()` 回写 + 连接池版本递增（T03） | 读路径纯函数；`set_target()` 仅在 host/port/凭证/TLS 目标实质变化时持久化并 bump generation；健康更新（connected/error）不持久化、不重置池 |
| A04 | PROPFIND 缓存仅按 path 建键，`Depth:0` 命中 `Depth:1` 列表（T04） | 缓存键 = (归一化路径, 归一化 Depth, 目标 generation/身份, PROPFIND 请求体摘要)；Depth、不同属性查询、目标轮换互不串用；只缓存同键响应 |
| A05 | 用 `b"<response"` 计数，命名空间前缀 `<D:response>` 不进缓存（T05） | 命名空间无关解析（local-name = `response`）；`207` 且 ≥2 条 response 才缓存；解析失败不缓存、不破坏响应 |
| A06 | MOVE/COPY 只清源路径，目标目录缓存保留（T06） | 解析 `Destination`，失效源路径自身/父目录与目标路径自身/父目录的整棵相关缓存 |
| A11 | 上游 TLS `CERT_NONE` + `check_hostname=False`，加载客户端证书异常被吞（T11） | `tls_transport`：`CERT_REQUIRED` + 主机名校验；CA 缺失/客户端证书加载失败 fail-closed 抛 `TlsConfigError`；按 CA/主机名/握手分类错误；失败闭锁（cooldown）并给出可诊断原因；`127.0.0.1` 不豁免，期望身份必须显式配置 |
| A12 | HEAD 丢掉上游 `Content-Length`，按空体改写成 0（T12） | HEAD 原样转发上游响应头（含 Content-Length），只不写响应体；不伪造长度 |
| A13 | 缓存超 200 项只删过期项，非硬上限（T13） | 硬上限：插入超限即按最旧条目淘汰，保证 `len ≤ MAX` |
| A14 | PUT/MOVE/DELETE 在"已提交但响应丢失"后盲重放（T14） | 按发送阶段分类：发送前失败可重试；发送后不确定提交结果时，不安全方法**不重放**，返回带诊断的 `502/503`；安全方法（GET/HEAD/OPTIONS/PROPFIND）允许重试一次；重试前重新读取最新快照并重建 URL/auth/TLS/连接 |

动态端口两项（评审 P1 多写者/旧目标回滚、重试仍 `[A,A]`）：
- 读端纯读取，不再回写，新发布的 B 不会被旧 A 覆盖；
- 重试时重新 `load_snapshot()`，从最新快照重建 host/port/auth/TLS，不再持有旧连接与旧身份。

## 2. `mountapp/state_store.py`（共享模块，仅 proxy owner 写；client owner 只消费）

### 2.1 落盘格式（schema_version=2，向后兼容读取 v1 字段）

```json
{
  "schema_version": 2,
  "generation": 7,
  "published_at": 1760000000.12,
  "host": "127.0.0.1",
  "port": 49559,
  "base": "/pool0/data",
  "username": "<u...>",
  "password": "<...>",
  "tls": {
    "ca_file": "/abs/ca_chain.pem",
    "cert_file": "/abs/client_cert.pem",
    "key_file": "/abs/client_key.pem",
    "server_name": "nas.example-device"
  }
}
```

- v1 遗留字段 `connected` 仅作历史提示，**不是健康权威**，读接口不作为 health 输出。
- 原子发布：同目录临时文件 → `fsync` → `os.replace`；文件 `0600`、目录 `0700`；`generation` 单调递增。

### 2.2 API（冻结面）

```python
SCHEMA_VERSION = 2
DEFAULT_STATE_PATH = ...  # 兼容现有 ~/Library/Application Support/小米智能存储挂载助手/proxy_state.json

class StateFormatError(Exception): ...          # 文件损坏/类型错误；读接口抛出，绝不自愈回写

@dataclass(frozen=True)
class TlsTarget:
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    server_name: str | None = None   # 必须来自用户/受信配置；禁止从未验证的对端证书自动学习

@dataclass(frozen=True)
class TargetSnapshot:
    schema_version: int
    generation: int
    published_at: float
    host: str
    port: int
    base: str
    username: str
    password: str
    tls: TlsTarget
    def is_complete(self) -> bool: ...   # port/username/password 齐备
    def to_dict(self) -> dict: ...      # 可序列化副本（深拷贝）

def default_state_path() -> str
def load_snapshot(path: str = default_state_path()) -> TargetSnapshot | None:
    """纯读：不存在返回 None；损坏抛 StateFormatError；零写副作用。"""
def publish_target(
    path: str = default_state_path(),
    *, host: str = "127.0.0.1", port: int,
    username: str = "", password: str = "",
    base: str = "/pool0/data",
    tls: TlsTarget | None = None,
    expected_generation: int | None = None,
) -> TargetSnapshot:
    """唯一写者（常驻 service）原子发布；expected_generation CAS 拒绝旧窗口并发发布；返回新快照。
    proxy 只调用 load_snapshot 纯读，绝不调用本函数。"""
def read_legacy(path: str = default_state_path()) -> dict:
    """兼容 conn_manager 旧读取：只取 {username,password,port}，纯读。"""
```

**写者冻结**：常驻 **service 进程是 `publish_target` 唯一写者**，以 `expected_generation` CAS 拒旧窗口；proxy 仅 `load_snapshot`，health 纯内存。GUI 只经 owner-only Unix IPC 向 service 发命令，不直接写状态文件。service 持续发现/自愈并内嵌 `ProxyServer`，不另起 proxy 子进程；GUI 退出不停止 service，发现/自愈由 service 持续承担。

**CAS 原子性**：`publish_target` 在 `path + ".lock"`（0600）上 `flock(LOCK_EX)` 贯穿「重读 → 比对代次 → 同值去重 → 原子写 → 释放」，锁内重新从磁盘取代次，避免两写者同读一代次双双通过；损坏文件直接抛 `StateFormatError`，不静默回滚代次为 0。同值发布（身份完全一致）不落盘、不 bump。

健康键（`connected` / `last_error` / `tls_lockout`）**不进本文件**，由各进程内存持有。

## 3. `mountapp/tls_transport.py`（共享模块，仅 proxy owner 写）

```python
class TlsError(Exception):
    code: str            # machine-readable: config/verify/handshake
    diagnostic: str      # 面向日志/UI 的人类可读信息（绝不含密码/私钥内容）

class TlsConfigError(TlsError): ...    # CA/客户端证书/私钥缺失或不可用
class TlsVerifyError(TlsError): ...   # 信任链失败 / 主机名不匹配
class TlsHandshakeError(TlsError): ...# 协议/对端非 TLS/网络层握手失败

告警归类：`UNKNOWN_CA`/证书过期/吊销/自签/本地无签发者/主机名不匹配 → `verify`；
`BAD_CERTIFICATE`、`PEER_DID_NOT_RETURN_A_CERTIFICATE`（mTLS 缺客户端证书）、
非 TLS/EOF/断连 → `handshake`，不把 mTLS 缺证弱化为 verify。

@dataclass(frozen=True)
class TlsConfig:
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    server_name: str | None = None   # 必须来自用户/受信配置；禁止从未验证证书自动学习

def build_client_context(cfg: TlsConfig) -> ssl.SSLContext:
    """始终 CERT_REQUIRED + check_hostname=True（无降级开关）；
    server_name 必须显式给出（禁止用 127.0.0.1 冒充身份）；
    ca_file 缺失 → TlsConfigError；客户端证书/私钥加载失败 → TlsConfigError（fail closed）。"""

class TlsLockout:
    """失败分类 + 冷却闭锁 + 成功复位；状态可诊断，不落盘。"""
    def __init__(self, cooldown: float = 30.0, max_failures: int = 3)
    def record_failure(self, exc: BaseException) -> TlsError
    def record_success(self) -> None
    def locked(self) -> bool
    def status(self) -> dict   # {locked, reason_class, attempts, retry_after, last_diagnostic}
```

**TLS 配置来源（client 冻结键，无 `tls_verify=false`、无 insecure 豁免）**：
- `config.json`：`tls_server_name`（必填受信期望身份）、`tls_ca_file` / `tls_cert_file` / `tls_key_file`（路径可由官方本地证书发现兜底）；
- 环境变量优先：`MWDAV_TLS_SERVER_NAME` / `MWDAV_TLS_CA_FILE` / `MWDAV_TLS_CERT_FILE` / `MWDAV_TLS_KEY_FILE`；
- `server_name` 只来自上述显式受信配置，禁止从未验证的对端证书学习；未配 `server_name` 即 fail closed。
- CA 允许官方本地证书目录兜底（`ca_chain.pem`）；兜底也缺失时 fail-closed，不回退系统信任库。
- 发现层同样必须完成完整 hostname + 链验证，不允许跳过主机名。

## 4. `mwdav_proxy.py` 对内/对外接口（兼容旧调用）

### 4.1 进程内单例（client owner 现有调用保持可用）

```python
UP = Upstream()
# 旧签名保留：update(port=, username=, password=, connected=, error=)
# 内部拆分：port/username/password/host/base/tls → set_target()（实质变化才持久化+bump）
#           connected/error → set_health()（纯内存，不写盘，不重置池）
UP.snapshot() -> dict  # 含 generation/host/port/username/password/base/connected/last_error/tls
```

### 4.2 入站认证（A01，始终 require，无开关）

- 目标 `is_complete()` 但入站 Basic 不匹配 → `401 + WWW-Authenticate: Basic realm="MiNAS"`。
- 目标未配齐 → `503` 文案“代理目标尚未配置”，不转发；该判定先于认证，避免在根本没有可比较凭证时把配置故障误报为 401。
- 不存在匿名/optional/降级模式；NetFS 不携带凭证即明确失败，属待手工验收项，不以弱认证替代兼容。
- 对 `Expect: 100-continue`，在返回 100 前执行目标完整性、入站认证、chunked、Content-Length 和体积门禁；拒绝时设置 `close_connection=True`。
- 代理的读路径与 error 路径**绝不调用发布（publish）**；`UP.update` 仅用于 client 配置发布。

### 4.3 请求体/响应有界（A02 + 评审 P1 全量缓冲）

- `MAX_REQUEST_BODY`（默认 200MiB）：超限 → `413`，`Connection: close`，受限排空（≤256KiB）后关闭，不转发。
- `Transfer-Encoding: chunked` 写方法 → `501` + `Connection: close`（v1 不做流式解码；阶段二流式项）。
- 声明长度与实际不符/读取中断 → `400/502`，禁止 `body=None` 转发 PUT。
- `MAX_RESPONSE_BODY`（默认 64MiB 硬限，非并发总量）：上游响应超限 → 读完上限即中止，`507`，关闭上游连接；v1 不做 spool/streaming，超过该尺寸的大文件下载明确受限（Finder 元数据/小文件不受影响），真正流式背压列阶段二。
- 每请求独立读写超时；信号量在**读取之前**获取，排队时长有界。

### 4.4 缓存（A04/A05/A06/A13）

- 键 `(norm_path, norm_depth, target_generation/身份, PROPFIND 请求体摘要 sha256)`：Depth、不同属性查询、目标轮换互不串用；空请求体用固定标记。
- `xml.etree.ElementTree` 命名空间无关取 local-name `response`；仅 `207` 且 ≥2 条且解析成功才缓存。
- 硬上限 `PROPFIND_CACHE_MAX_ENTRIES`：超限淘汰最旧插入项，`len ≤ MAX` 恒成立。
- MOVE/COPY：失效源与 Destination 两端的自身及父目录前缀树。
- self-entry 剥离保留现有行为，但仅作用于成功响应；解析失败原样透传。

### 4.5 HEAD（A12）

原样透传上游响应头（含 `Content-Length`、`Content-Type` 等），不写响应体，不把长度改写成 0。

### 4.6 重试与提交确定性（A14 + 动态端口）

- 发送前（建连/发送阶段）失败：可重试一次，重试前 `load_snapshot()` 取最新目标，重建 URL/Basic/TLS/连接。
- 发送后不确定：安全方法（GET/HEAD/OPTIONS/PROPFIND）可重试；不安全方法（PUT/DELETE/MOVE/COPY/MKCOL/PROPPATCH/LOCK/UNLOCK）不重放，抛 `UnknownCommitError` → `502` 诊断"上游可能已处理，请勿自动重发"。
- 保守说明：不安全方法不区分 connect/send 阶段，任意传输失败都按"提交不确定"处理（宁可客户端显式复查，不冒双写风险）。
- TLS 失败（包括底层原生 `ssl.SSLError` / `SSLCertVerificationError`）先经 `classify_error` 转成
  `TlsVerifyError`/`TlsHandshakeError` 并计入 `TlsLockout`；冷却期内直接快速失败，不反复握手。
- 上游 request-target 必须拆分 path/query：只 percent-encode path，并保留原始 query 分隔符与已编码序列；
  首次请求与安全方法重试使用同一构造函数。

### 4.7 CLI（凭证不进 argv）

```
--port 18445 --upstream 49559 --host 127.0.0.1 --base /pool0/data
--ca --cert --key --server-name
--max-request-bytes --max-response-bytes
```

独立代理从 0600 状态快照读取 username/password；不提供 `--username/--password`，无
`--inbound-auth`、无 `--no-tls-verify` 降级开关。环境变量覆盖解析后的 `server_name`
同时用于 SSLContext 缓存键和实际 SNI/hostname，不得只构建 context 却把旧值/None 传给连接。

### 4.8 嵌入常驻 service 的 ProxyServer 生命周期

service 进程内嵌单一 `ProxyServer`，不另起 proxy 子进程；自身不发现、不发布，持续发现/自愈由 service 循环 `load_snapshot()` + `UP.adopt()` 承担。

```python
class ProxyServer:
    def __init__(self, port: int = 0, enable_https: bool = False)
    def start(self) -> int                  # 绑定+起 serve_forever 线程，返回实际监听端口；
                                            # 端口占用抛 OSError(EADDRINUSE)，不留半成品
    def stop(self, timeout: float = 5.0) -> None  # shutdown→server_close→join；可重复调用，清理套接字
    def is_running(self) -> bool
    @property
    def listen_port(self) -> int | None
```

- `stop()` 必须 join 服务线程并关闭套接字；重复 start/stop 不泄漏线程/端口；绑定冲突时原实例不受影响。
- GUI 只经 owner-only Unix IPC 向 service 发命令，不直接持有/启停本服务。

## 5. 测试契约（本地 fixture，client owner 可复用下列夹具约定）

- `tests/` 下三族：`test_state*.py`（原子发布/纯读/兼容 v1/并发不回滚）、`test_tls*.py`（信任链/主机名/客户端证书 fail-closed/闭锁分类）、`test_proxy*.py`（单测内存流、`ProxyServer` 嵌入生命周期 start/stop/join/占用、独立子进程真实 loopback HTTP/TLS 回归）。
- 证书：测试内用 `openssl` 临时生成 CA + 服务端证书（SAN DNS/IP）+ 客户端证书，用完即删；不读 `/Applications/...` 真实证书。
- 独立进程：`spawn` 起真实代理子进程，本地 TLS fixture 端点，覆盖：正常轮换 A→B、读竞态不回滚、重试取最新端口（不再 `[A,A]`）、错误 CA/错主机名/缺客户端证书闭锁、超限 413、chunked 501、HEAD 长度保留、Depth 隔离、命名空间缓存、MOVE 双端失效、硬上限、入站 401/503、不安全方法不重放。
- 不调用仓库根目录既有探针（`test_*.py` 联网/真实凭证脚本保持原样，不改不跑）。

## 6. 未验证范围（如实声明）

- 不在 Linux 冒充 macOS/Finder/NetFS 实机兼容；入站 `require` 在真实 NetFS 的兼容性需 client owner 在 macOS 验收。
- 大文件真正流式背压（spool/streaming）列为阶段二；v1 为 64MiB 硬限有界拒绝，超限下载明确失败而非静默截断。
- 官方 P2P 隧道真实证书链与设备 CN 机制在仓库外，本规格只定义校验接口与 fail-closed 行为。
- 入站 require 在真实 NetFS 的兼容性需 macOS 手工验收；未验收前不宣称兼容。
