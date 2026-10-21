# 运行时规格（Runtime Spec）v4 — 常驻单服务控制面 + GUI 瘦客户端

状态：v4（常驻架构冻结；先规格后实现）
基线：`d25c2516a17a28321df9cdaf4dd7c82662af3d3a`，分支 `fix/sdd-webdav-hardening`
runtime owner 独占：`mountapp/app_core.py`、`mountapp/conn_manager.py`、`mountapp/main.py`、
`mountapp/netfs_mount.m`、`mountapp/launcher.sh`，以及新增 `mountapp/service.py`、
`mountapp/ipc.py`、`mountapp/client.py`；测试 `tests/runtime/`、`tests/dynamic/`、`tests/test_service*.py`。

协作边界（不改他人文件，仅经明确接口配合）：
- proxy owner：`mwdav_proxy.py` / `state_store.py` / `tls_transport.py`。服务**进程内嵌** `ProxyServer`，
  不再 fork 独立代理子进程；proxy owner 仅按需提供嵌入生命周期钩子（start/stop/健康），不改数据面硬化。
- doc owner：总体 architecture、用户文档、README；本文件只定 runtime 模块边界与 IPC 契约。

> v3 中「GUI 退出后无常驻、不发现/不自愈」的声明**被本版取代**：现由常驻服务承担持续发现与自愈。

## 0. 已固化保留的修复（v1–v3，回归不得回退）

| 编号 | 落点 | 要点 |
|---|---|---|
| T07 | `app_core.parse_mount_table`/`_own_mount_entry` | 只认领源 `127.0.0.1:18445` 且挂载点匹配期望的卷；他卷不认领/不卸载/不自愈 |
| T08 | `app_core.is_mounted` | 自定义 `mount_dir` 必须在挂载表实测归属，裸字符串不算已挂 |
| T09 | `conn_manager.refresh`/`_reset_stale`/`_fail` | 进入即归位 connected/mode/端口；失败分支显式 `connected=False+error` |
| T10 | `main.human_size` | 模块级纯函数（1024 进制），刷新无 NameError |
| D05 | `conn_manager._probe_once(present_cert=)`/`probe_webdav` | 匿名剥离证书先探；明确 mtls_required 或模糊 EOF 且已配证书→自动带证书升级；`verify_failed` 不换证书掩盖；未配证书 fail-closed |
| D06 | `conn_manager.probe_webdav` | 401 Basic 单列 `auth_required` 候选，带凭证重试转 confirmed，不丢弃 |
| D07 | `conn_manager.poll_storage` + 服务快慢分离 | 快路径确认即发布；慢存储独立低优先级、失败非致命、不阻塞发布 |
| D08 | `conn_manager.find_tunnel_ports` | `lsof` 以 `-a` 将目标 PID、TCP 与 LISTEN 条件做 AND；候选仅来自 `sso_login`，不得混入系统其他 App 监听端口 |
| M01/M02 | 配置/launcher | 服务加载并原子持久化配置；`get_config` 独立只读；源码与 app bundle 启动路径均支持；launcher 0755、Python 3.10+ 能力门禁、无宽泛 pgrep |
| M03 | `ResidentService._mount_health` | 连续断线/死挂载清理并恢复，显式用户卸载不自动挂回 |
| M04/M06/M08 | discovery | 200/207 仍须 DAV 头；`pgrep -x sso_login`；first-confirmed 不等待慢输家 |
| M05/M09 | HTTP/IPC/lifecycle | 拒绝路径 close_connection；Expect 前置门禁；读体前并发门禁；IPC reader 关闭；启动失败释放 socket/flock |
| M07/M09/M10 | proxy/TLS/state | TLS lockout 快速失败；原生 SSL 分类后计入闭锁；解析后 server_name 用于 SNI；adopt 锁内原子化；query 与 path 分离；代理 CLI 无凭证参数 |
| M10 | mount/netfs | 手动挂载成功复位用户卸载意图；helper 超时 kill+reap；stdin 仅移除行分隔符并保留凭证空白 |
| SR-3 | `conn_manager._VerifiedConn`/`load_tls_config` | CERT_REQUIRED+check_hostname=True；server_name 仅显式配置；无 verify=false 降级 |
| SR-4 | `netfs_mount.m` + `app_core.mount` | 凭证走 stdin、不进 argv；`MWDAV_MOUNT_PROTOCOL=2` 能力握手，旧 helper 拒绝 |

## 1. 架构目标与非目标

目标：
- **一个常驻用户服务进程**持有 `AppCore`/`ConnManager`/挂载协调与**内嵌 ProxyServer**；
  持续做 A→B 动态发现、目标发布、健康恢复，与 GUI 是否打开无关。
- GUI 只做展示与经受保护本地 IPC 下发命令；**不拥有**发现/发布/自愈线程。
- 启动幂等：多 GUI 只对应一个服务、一个内嵌代理；单实例锁。
- IPC 优先 Unix socket（0700 目录、socket 0600）；状态响应**绝不返回 NAS 凭证**；命令完整校验；秘密不进 argv。

非目标：
- 不做网络远程控制、不做多用户/跨机；IPC 仅本机当前用户。
- 不在 Linux 冒充 macOS/Finder/NetFS 实机；不重编译既有 `netfs_mount` 二进制。
- 不改代理数据面硬化（入站恒认证、缓存、有界体、不安全方法不重放）。

## 2. 进程与模块边界

```
┌─────────────────────────── 常驻服务进程 (service.py) ───────────────────────────┐
│  SingleInstanceLock ── ResidentService                                           │
│    ├─ 内嵌 ProxyServer (mwdav_proxy.ProxyServer, 线程内 serve_forever)           │
│    ├─ AppCore + ConnManager：发现循环 / CAS 发布 / 挂载协调 / 健康自愈            │
│    └─ IpcServer (ipc.py)：Unix socket，命令序列化 + 校验 + 脱敏                  │
└───────────────▲─────────────────────────────────────────────────────────────────┘
                │ Unix socket（0700 目录 / 0600 socket，帧化 JSON）
┌───────────────┴───────────────┐
│ GUI 进程 (main.py → client.py) │ 仅轮询脱敏 status、下发命令；无发现/发布/自愈线程
└───────────────────────────────┘
```

### 2.1 `service.py`（常驻服务，runtime owner）
- `ResidentService`：组装内嵌代理、AppCore/ConnManager、IPC server；负责 start/stop 生命周期。
- 持有发现/发布/自愈循环（由现 `AppCore._loop`/`_storage_loop` 迁入，行为保持）。
- 内嵌代理在进程内 `ProxyServer.start()`；目标变更经内存 `UP.adopt(snap)` 采纳，不必重读文件。
- 单实例：`flock` 锁文件位于运行时目录；第二个启动者检测到锁即**附着**（连接现有服务），不重复绑定 18445。
- 安全关停：`stop()` 置停事件 → 加入发现/存储线程 → 关 IPC socket → 停内嵌代理 → 释放锁；不杀无关进程。

### 2.2 `ipc.py`（传输与协议，runtime owner）
- 路径：运行时目录（0700，当前用户独占）下 socket 文件（0600）；不监听 TCP。
- 帧化 JSON 请求/响应；单 worker 队列 + 锁保证**命令序列化**，并发调用按序处理，不交错写状态。
- 命令白名单与 schema 校验；非法字段/未知命令 → 结构化错误，不执行。
- 脱敏层：`status` 响应剥离 `username/password/tls.key_file/私钥材料`，只回非敏感视图。

### 2.3 `client.py`（GUI 瘦客户端，runtime owner）
- 连接 socket 发送命令、接收脱敏视图；GUI 关闭仅断开连接，**不通知服务停止**。
- 无后台发现线程；展示数据全部来自服务 `status`。

### 2.4 `main.py`（入口分流）
- `main.py --service`：以前台常驻方式启动 `ResidentService`（供 LaunchAgent 调用）。
- 默认（无参）：GUI 客户端；必要时幂等拉起服务再连接。

## 3. IPC 命令契约（帧化 JSON，请求→响应）

通用响应：`{"ok": bool, "error": str|null, "data": {...}}`。

| 命令 | 入参（校验后） | 行为 | 响应 data（脱敏） |
|---|---|---|---|
| `status` | 无 | 返回当前非敏感运行视图 | `connected, mode, webdav_port, mounted, mount_path, storage[{total,used}], error, service_pid`；**无 username/password** |
| `get_config` | 无 | 返回服务权威的非敏感用户配置 | `auto_mount, auto_start, mount_dir` |
| `mount` | 无 | 服务内执行挂载协调（凭证仍走 stdin，不进 argv） | `mounted, mount_path` |
| `unmount` | 无 | 服务内卸载，保留用户意图 | `mounted=false` |
| `open_finder` | 无 | 打开已挂载点（无挂载点→错误） | `opened: bool` |
| `refresh` | 无 | 触发一次强制发现刷新 | `connected, webdav_port` |
| `set_config` | `auto_mount:bool, auto_start:bool, mount_dir:str?` 类型校验 | 原子持久化；`auto_start` 安装/卸载 LaunchAgent；失败回滚 | 回显已接受配置 |
| `stop` | 无 | 显式安全关停服务（仅用户主动/退出 LaunchAgent） | `stopped: true` |

校验规则：布尔必须为 bool；`mount_dir` 为字符串且规范化后非空才接受；未知命令/缺字段/类型错 → `ok=false`，不执行副作用。

## 4. 生命周期与 LaunchAgent（合理 KeepAlive，避免无限重启误杀）

- GUI 关闭：仅断开 IPC；服务继续发现 A→B、健康恢复、发布目标。
- 显式停止：`stop` 命令或卸载 LaunchAgent；不由 GUI 窗口关闭触发。
- LaunchAgent：`RunAtLoad=true`；`KeepAlive` 采用**受限策略**（`Crashed=true` 崩溃重启、`SuccessfulExit=false`
  正常退出不强拉，`ThrottleInterval=10`），标签 `com.minas.mount` 作用域内，**不重启/不误杀其他进程**。
  plist 由 `plistlib` 生成合法 XML（路径特殊字符转义），文件 0600；`launchctl` 经 argv 列表
  （`bootstrap/bootout gui/{当前uid}`，uid 可注入）`check=True` 调用，无 shell 拼接、无注入；
  不硬编码 501；runner 可注入便于测试。
- 单实例锁 + 18445 绑定冲突时拒绝第二实例并报错，不抢占端口。

## 5. 安全约束（冻结）
- IPC 仅本机 Unix socket；目录 0700、socket 0600；无 TCP、无匿名连接。
- `status` 永不回 NAS 用户名/密码/私钥；凭证只存在于服务内存与受信状态文件，不进 argv、不进 IPC 明文回显。
- TLS 发现/数据面 fail-closed 不变：`CERT_REQUIRED + check_hostname=True`，server_name 仅显式配置，无 verify=false。
- NetFS 凭证仍经私有 stdin；旧 helper 无 `MWDAV_MOUNT_PROTOCOL=2` 标记一律拒绝。

## 6. 测试策略（仅本地 fixture，SR-6；计数以 CI 最新结果为准）
既有缺陷回归与新增服务用例全部必须通过（全部 mock/临时目录/回环，不触真实 NAS、不真实 mount）：
- `tests/test_service_lifecycle.py`：单服务启动、内嵌代理存活、`stop` 干净释放端口与锁。
- GUI 退出保持服务：客户端断开后服务循环继续，动态目标 A→B 仍被发布（注入轮换目标断言二次发布）。
- `tests/test_ipc_protocol.py`：status 脱敏（无 password）；命令序列化（并发调用有序）；非法 schema 被拒。
- 单实例：第二次启动附着/拒绝，仅一个内嵌代理绑定 18445。
- 安全关停：线程加入、socket/代理关闭、无孤儿；无真实 mount（断言不执行 `/sbin/mount`/netfs 二进制）。

## 8. Service API 冻结（供独立验收直接写用例；未列字段/命令一律不存在）

> 注入只走构造参数；**生产 IPC 不提供** test-mode 开关、不提供任意上游 host/port/凭证设置命令、
> 不向代理透传任意目标。发现候选只能来自认证后的 discovery backend，凭证不进 IPC。

### 8.1 启动入口与构造注入（`mountapp/service.py`）

```python
class DiscoveryBackend(Protocol):
    def refresh(self, force: bool) -> DiscoveryView: ...   # 见 8.5
    def poll_storage(self) -> list[dict]: ...

class MountBackend(Protocol):
    def ensure_capable(self) -> bool: ...
    # username/password 来自 ResidentService 当前已确认 DiscoveryView；生产适配器必须
    # 原样送入显式凭证挂载入口，不得回读另一个 ConnManager 的状态。
    def mount(self, url: str, mountpoint: str, username: str, password: str) -> tuple[bool, str]: ...
    def unmount(self, mountpoint: str) -> tuple[bool, str]: ...
    def own_mountpoint(self, expected: str) -> str | None: ...  # 归己才返回点，否则 None

class ResidentService:
    def __init__(self, config: dict | None = None, *,
                 config_path: str | None = None,         # 生产缺省 config.json；测试可传 tmpdir/纯内存
                 state_path: str | None = None,          # state_store 文件；测试传 tmpdir
                 socket_dir: str | None = None,          # 0700 运行时目录；测试传 tmpdir
                 discovery: DiscoveryBackend | None = None,  # 缺省=真实 ConnManager；测试注入脚本化 fake
                 mount_registry: MountBackend | None = None, # 缺省=真实 netfs/mount；测试注入内存 fake
                 proxy_factory: Callable[[int], ProxyLike] | None = None,  # 缺省=mwdav_proxy.ProxyServer
                 adopt_target: Callable[[TargetSnapshot], bool] | None = None, # 缺省=全局 UP.adopt
                 proxy_port: int = 18445,
                 stop_timeout: float = 5.0): ...
    def start(self) -> None: ...      # 进程内：起循环线程（测试/嵌入用）
    def serve(self) -> int: ...       # 前台阻塞：LaunchAgent/SIGTERM 用；返回退出码
    def stop(self) -> None: ...       # 幂等安全关停
    def last_generation(self) -> int | None: ...  # 已发布目标代次（非密，观测点）
    def status_view(self) -> dict: ...# 脱敏内存视图（同 IPC status data）
```

- CLI：`main.py --service` → 前台 `serve()`（LaunchAgent）；无参 = GUI 客户端。
- 进程内测试用 `start()`/`stop()` 跑线程；S1/S6 真实进程用 `serve()` fork 子进程。
- 缺省 `discovery` 适配现有 `ConnManager`；缺省 `mount_registry` 适配现有 netfs；行为与 v1–v3 一致。

### 8.2 IPC 协议（`mountapp/ipc.py`）
- Unix stream socket；目录 0700、socket 文件 0600；**newline-delimited 单行 JSON**（请求/响应各一行）。
- 请求：`{"cmd": str, "id": int|str, "args": {...}}`；响应：`{"ok": bool, "id": ..., "error": str|None, "data": {...}}`。
- 服务端单 worker 队列 + 锁，命令**按到达顺序串行**；`status` 只读内存视图，不触发 refresh/publish/写。
- 命令白名单（除此之外一律 `ok=false, error="unknown command"`，零副作用）：
  `status` / `get_config` / `mount` / `unmount` / `open_finder` / `refresh` / `set_config` / `stop`。
- **无** `publish`、无任意上游/凭证命令、无代理透传、无 RCE 面。

### 8.3 命令字段与校验
| cmd | args（类型严格，缺/错 → ok=false 不执行） |
|---|---|
| `status` | 无 args |
| `get_config` | 无 args；只返回 `auto_mount/auto_start/mount_dir` |
| `mount` | 无 args（URL/挂载点由服务内部决定，不经客户端传入） |
| `unmount` | 无 args |
| `open_finder` | 无 args |
| `refresh` | 无 args（强制刷新一次） |
| `set_config` | `auto_mount: bool`、`auto_start: bool`、`mount_dir?: str`；`mount_dir` 须为归一化绝对字符串、拒绝 `..`/空/非绝对；错则不写盘（测试仅验证拒绝与内存 fake，不碰真实路径） |
| `stop` | 无 args |

### 8.4 status 脱敏白名单（响应 data 只含以下键）
```json
{"service_pid": 0, "connected": false, "mode": "", "webdav_port": null,
 "mounted": false, "mount_path": null,
 "storage": [{"total": 0, "used": 0}], "error": null, "generation": 0}
```
**禁止出现在任何响应/日志/socket 路径**：`username`、`password`、token、tls 私钥/`key_file` 内容、CA 内容、原始凭证。`generation` 为非密发布代次。

### 8.5 DiscoveryView（注入返回值，对齐 ConnManager.snapshot）
```python
@dataclass(frozen=True)
class DiscoveryView:
    connected: bool
    mode: str            # "" | "p2p" | "lan"
    webdav_port: int | None
    username: str
    password: str
    storage: list[dict]  # [{total,used}]
    error: str | None
```
- 服务确认 `connected` 后经 `state_store.publish_target(expected_generation=...)` CAS 发布，`generation` 单调不回退；
  随后内存 `proxy.adopt(snap)`。fake discovery 脚本化 A→B 轮换与失败/恢复窗口即替换 `refresh`/`poll_storage`。

### 8.6 A→B 观测点（内嵌代理）
- `proxy_factory(port)` 产物**仅生命周期 API**：`.start()/.stop(timeout)/.is_running()/.listen_port`；
  不提供每实例 `adopt`（避免多实例全局混乱）。目标经注入的 `adopt_target(snap)` 采纳：
  缺省=进程内唯一全局 `mwdav_proxy.UP.adopt`（一个 service 仅一个 UP）；fake 记录
  `adopted: list[(port, generation)]`。验收据此断言 A→B 后打到 B、A 不再被采纳、代次单调一致。
- `state_path` 测试参数必须传给 `publish_target`，代理读取同一文件；服务只发布认证发现结果，
  生产 IPC 不能直接喂任意目标。

### 8.7 单实例锁
- 默认 `socket_dir/mwdav.sock`、`socket_dir/service.lock`（非阻塞 `flock LOCK_EX|NB`）。
- 第二实例抢锁失败 → 抛 `InstanceAlreadyRunning(pid)`，CLI 非零退出；不绑 socket、不起代理、不双写。
- 干净启动先 unlink 陈旧 socket 再 bind；存活持锁者的 lock 不被抢占；客户端重复连接不算第二实例。

### 8.8 stop 语义
- `stop` 命令或 SIGTERM：置停事件 → drain 在途请求（`stop_timeout`）→ join 发现/存储线程 → 停内嵌代理 →
  unlink socket、释放锁 → 退出码 0；重复 stop 幂等不报错；停止后不再发布/自愈。
- `start()` 在获取 flock 后任一步（IPC bind、线程启动）失败，必须停止已建 IPC、unlink socket 并释放 flock 后重抛，
  禁止留下“服务没起来但第二次启动永远报已运行”的半启动状态。
- 超时仅 best-effort join，正常路径不用 SIGKILL；SIGTERM 处理器只在 `serve()` 注册。

### 8.9 MountBackend fake（替代真实 NetFS/mount）
- 内存 registry 记录 `mount/unmount` 意图与幂等；`own_mountpoint` 仅对归己条目返回；
  默认实现才执行 `/sbin/mount` 表与 netfs helper，fake 绝不碰真实路径/二进制。

## 7. 已知限制（如实声明）
- Linux 无 macOS/NetFS/Finder：实机挂载卸载、真实 `/sbin/mount` 表、lsof/pgrep、LaunchAgent、tkinter 交互未执行。
- `netfs_mount` 已由 macOS CI 使用 Apple SDK 构建为 arm64+x86_64 Universal、0755，并验证 v2 stdin 协议；
  真实 Finder/NAS 挂载和错误码仍未实机验证。真实 NAS mTLS/设备 CN-SAN 未知。
