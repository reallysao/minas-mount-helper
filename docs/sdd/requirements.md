# 主规格说明书（Master Specification）

- 项目：MiNAS Mount Helper（小米智能存储 Finder 挂载助手）
- 分支：`fix/sdd-webdav-hardening`
- 评审基线提交：`d25c2516a17a28321df9cdaf4dd7c82662af3d3a`（2026-10-10）
- 文档状态：本分支定稿。本文件只定义"系统应当是什么"，不记录实现细节；
  模块内部设计见 `docs/specs/proxy.md`（数据面）与 `docs/specs/runtime.md`（控制面）。
- 证据来源：`架构评审/离线特征验证.py`（14 项离线特征复现，编号 T01–T14）、
  `动态端口定位/动态端口复现测试.py`（原 7 个回环场景，编号 D01–D07）、
  PR #2 macOS 实测发现的端口选择回归（D08），以及基于公开命令/协议文档的
  macOS 契约审计（M01–M11，`tests/test_macos_contract_audit.py` 与 macOS CI）。

## 0. 术语与边界

| 术语 | 定义 |
|---|---|
| 控制面（现状基线） | GUI 会话（main.py / app_core.py / conn_manager.py）：发现隧道、取凭证、发布目标、挂载生命周期、健康检查。 |
| 数据面（现状基线） | 独立的 `mwdav_proxy.py` 进程：把回环 `http://127.0.0.1:18445` 的 WebDAV 请求转发到官方 App P2P 隧道暴露的动态 HTTPS 端口 `/pool0/data`。 |
| 常驻服务（目标，FR-5） | 单一 PID 的后台服务，内嵌 ProxyServer，唯一配置发布者，内含发现/代理/挂载自愈/状态发布/IPC 服务端；GUI 仅为其 Unix socket IPC 客户端。 |
| 目标快照（target snapshot） | 上游 `host/port/username/password/generation` 的不可变视图；由控制面唯一写入，数据面只读。 |
| 特征复现（characterization） | 离线/mock 下确认"基线确实存在某行为"。复现通过 **不等于** 应用正确，也不等于缺陷已修。 |
| 本地自动化验收 | Linux/macOS 均可跑、不连真实 NAS、不挂载、不使用真实凭证的脚本验收。 |
| 手工实机验收 | 必须在真实 macOS + 官方 App + NAS 环境由人工执行的验收项，本地无法替代。 |

**明确不做（本分支范围外）**：不实现微服务；不逆向官方 P2P 隧道；不修改 NAS/路由器；
不重写历史 v1.4 ZIP；不轮换/使用仓库中暴露的真实凭证（仅在安全说明中提示所有者轮换）。

## 1. 产品目标（Goal）

G1. 在用户已登录并运行官方「小米智能存储」App 的 macOS 上，提供一个本机回环 WebDAV
端点，使 Finder 通过 NetFS 原生挂载浏览/上传/下载 NAS 文件，NAS 在异地（非局域网）也可用。
G2. 隧道端口动态变化时，控制面能重新发现并把新目标安全地发布给数据面，且旧目标不得回滚覆盖新目标。
G3. 数据面只把 NAS 凭证用于已授权的本地访问；匿名/未授权的本地访问不得借代理读写 NAS。
G4. 所有安全声明与实际代码一致：不宣称未实现的服务器证书验证、未接入的功能或旧发布包已有的能力。

## 2. 功能需求（Functional Requirements）

### FR-1 隧道发现与目标发布（控制面：conn_manager/app_core）
- FR-1.1 控制面通过 `sso_login` 进程及其监听端口集合发现官方隧道；凭证优先从官方 App
  本地缓存（LevelDB）读取，失败时从运行中代理的状态文件兜底（只读）。端口枚举调用 `lsof`
  时必须把目标 PID、TCP socket、LISTEN 状态作为 **AND** 条件组合（`-a`），禁止把系统其他
  App 的监听端口并入候选集；该命令契约须由离线测试冻结（对应 D08）。
- FR-1.2 快路径（PID → 端口 → 凭证 → WebDAV 端口探测）成功后，**立即**把目标快照发布给数据面；
  可选的存储统计（get_pool_info）必须独立异步、限频、退避，**不得阻塞**目标发布（对应 D07）。
- FR-1.3 目标快照含 `schema_version`、`generation`（单调递增）、`published_at`、设备/身份字段；
  **唯一写者 = ResidentService（常驻控制面）**，数据面 `load_snapshot()` 纯读不可变快照，
  **读取零写副作用**：不落盘、不递增代次、不改内存目标，也永不调用发布接口回写（对应 T03 / D02）。
  运行健康（`connected`/`last_error`/TLS 闭锁）只在各进程内存，永不写入配置文件，读路径不得回写健康。
- FR-1.4 发布由控制面经 `publish_target(..., expected_generation=...)` 完成，CAS 拒绝旧代次覆盖
  （多开/旧窗口持旧代次发布被拒并返回最新快照）；原子替换（同目录临时文件 → fsync → `os.replace`）、
  schema/version 校验；文件 0600、目录 0700；消费者读到半写/旧代次数据时丢弃并等待下一快照，
  不得把旧值整体覆盖新值（对应 D02）。落盘格式与 API 以 `docs/specs/proxy.md` §2 `state_store` 冻结面为准。
- FR-1.5 GUI 会话生命周期（现状基线，已随 T/D 项关闭；**将被 FR-5 常驻服务目标取代**）：
  该临时形态下 GUI 退出只保留退出瞬间代理已服务的**静态目标**，停止动态发现、端口轮换跟随与自愈。
  目标架构（FR-5）要求服务常驻，本项不得再当作最终形态对外宣传；现状与目标差异见 FR-5 与 S 系列验收。

### FR-2 数据面转发（数据面：mwdav_proxy）
- FR-2.1 全方法透传 DAV 1,2；路径由代理根改写为上游 `/pool0/data`；MOVE/COPY 的 `Destination`
  头改写为上游绝对 URL，保留 query/fragment。
- FR-2.2 Finder 元数据请求（`._*`、`.DS_Store`、`.Spotlight-V100`、`.fseventsd`、`.Trashes`、
  `.VolumeIcon.icns`、`.hidden`）本地直接 404，不转发上游。
- FR-2.3 PROPFIND 缓存：缓存键必须包含 path、Depth、请求属性集与上游 generation；
  只接纳明确成功（207）的响应；使用安全 XML 解析（命名空间中立）识别 `<response>` 条目；
  MOVE/COPY/PUT/DELETE/MKCOL/PROPPATCH/LOCK/UNLOCK 后同时失效**源与目标**目录缓存（对应 T04/T05/T06）。
- FR-2.4 缓存容量为硬上限：超过 `PROPFIND_CACHE_MAX_ENTRIES` 时按策略（LRU 或最旧）强制淘汰，
  不得只删过期项而无限增长（对应 T13）。
- FR-2.5 HEAD 响应保留上游 `Content-Length`，不得按空响应体改写为 0（对应 T12）。
- FR-2.6 请求体与响应体采用分块流式 + 背压；无法流式时，超过 `MAX_BODY_SIZE` 的请求必须
  返回 413 并正确排空/关闭连接，**禁止以空请求体转发**（对应 T02）。
- FR-2.7 写操作（PUT/MOVE/COPY/DELETE/MKCOL/PROPPATCH/LOCK/UNLOCK）的重试必须区分
  "握手前失败（可安全重试）"与"服务端可能已提交（响应丢失）"；后者不得盲目重放，
  应重新加载目标代次并在必要时先核对远端状态（对应 T14 / D04）。
- FR-2.8 连接池按上游身份（host/port/凭证代次/TLS 配置）分桶，仅身份实际变化时换 generation
  并显式关闭过期连接；配置不变时复用连接，请求路径只读快照、不落盘、不全局重置（对应 T03）。

### FR-3 挂载身份与健康（控制面：app_core）
- FR-3.1 引入 MountIdentity（规范化源 URL/端口、设备标识、挂载点）；`find_mount_path` 只认领
  属于本应用的卷，不得把任意 WebDAV 卷当作己方卷（对应 T07）。
- FR-3.2 配置了自定义 `mount_dir` 时，也必须实测该路径确为当前应用的挂载点才算已挂载，
  不得仅凭字符串返回真（对应 T08）。
- FR-3.3 所有失败/无端口分支必须清空旧的 `connected`/在线态，不得保留过期在线状态（对应 T09）。
- FR-3.4 挂载/卸载/自愈走串行执行器、幂等；保留"用户主动卸载"意图，下一轮不得自动挂回；
  卸载前先校验归属，避免误操作其他 WebDAV 卷。
- FR-3.5 界面把"隧道存在 / 认证通过 / 代理就绪 / 卷已挂 / I/O 正常"分开表达，
  不得把"有挂载表条目"直接等同"NAS 可达"。

### FR-4 界面与发布完整性（控制面：main.py）
- FR-4.1 通用工具函数（如 `human_size`）归属单一可导入模块，界面调用前必须导入；
  存储数据非空时界面刷新不得抛 NameError（对应 T10）。
- FR-4.2 主程序只加载实际接入的功能；未接入主链路的模块（如当前的 `sync_engine.py`）
  不得在用户文档中宣称已提供。若保留为未来模块，需明确标注"未接入"。
- FR-4.3 发布包与单一源码版本对应，携带 commit 标识与校验和；不含 pycache、共享私钥或真实凭证。

### FR-5 常驻服务架构（S01–S07；Service API 见 `specs/runtime.md` §8）

> S01/S02/S03/S05/S06/S07 与 S04 自动部分由 tests/ 独立服务生命周期/IPC/runtime 用例在
> Linux/loopback 自动通过关闭；macOS LaunchAgent 保活、Finder/NetFS 实机属 B 层，未执行。

- FR-5.1（S01）**单一常驻服务进程（一个服务 PID）**：`ResidentService` 内嵌**单个** ProxyServer
  实例（`proxy_factory` 注入，缺省 = mwdav_proxy.ProxyServer），不再 fork/拉起独立代理子进程；
  服务是配置的**唯一发布者**，承担发现、代理转发、挂载自愈，生命周期自包含。进程内 `start()` 起循环线程，
  前台 `serve()` 阻塞供 LaunchAgent/SIGTERM；`stop()` 幂等安全关停（`stop_timeout=5.0`，drain 在途请求后
  join 线程、停内嵌代理、释放锁/socket）。单实例锁：第二实例抢 `flock NB` 失败即非零退出，不起代理、不双写。
- FR-5.2（S02）**明确模块边界**：服务内部划分 discovery（`DiscoveryBackend.refresh/poll_storage`）、
  proxy（嵌入 ProxyServer，`adopt(snap)` 采纳发布快照）、mount（`MountBackend.ensure_capable/mount/unmount/
  own_mountpoint`）、state publish（`publish_target(expected_generation)` CAS、`last_generation()`）、
  ipc server 五个模块；模块只经构造注入接口交互，禁止跨边界直写内部状态。
  `MountBackend.mount(url, mountpoint, username, password)` 必须使用 ResidentService 当前 DiscoveryView
  显式传入的目标与凭证，不得重新实例化发现状态或回读独立空 ConnManager（对应生产挂载链路回归）。
- FR-5.3（S03）**GUI 为受保护 Unix socket IPC 客户端**：`main.py` 无参 = GUI 客户端，`--service` = 前台
  `serve()`；GUI 只展示 `status_view()` 脱敏视图、下发白名单命令，不持有/发布服务状态。
- FR-5.4（S04）**GUI 退出服务继续**：关窗只断开 IPC 连接，服务循环、发现跟随、A→B 二次发布与自愈持续
  （自动部分，Linux/loopback 已关闭）。macOS per-user LaunchAgent 登录拉起/KeepAlive 保活与 Finder/NetFS
  实机挂载属 B 层，未执行。
- FR-5.5（S05）**显式 stop**：IPC `stop` 命令或 SIGTERM 触发同一幂等关停，独立于"关闭窗口"；
  停止后不再发布/自愈，退出码 0，重复 stop 不报错。
- FR-5.6（S06）**IPC 安全**：运行时目录 0700、`mwdav.sock` 0600，属主当前用户；命令白名单
  `status/get_config/mount/unmount/open_finder/refresh/set_config/stop`，其余命令 `ok=false` 零副作用；
  无 `publish`、无任意上游/凭证命令、无代理透传、无 RCE 面；`get_config` 只回显非敏感
  `auto_mount/auto_start/mount_dir`；`set_config` 仅允许这三项并持久化，`auto_start` 实际安装/卸载 LaunchAgent，
  `mount_dir` 必须为归一化 `/Volumes/...` 路径（拒 `..`/逃逸）。
- FR-5.7（S07）**状态只读暴露**：`status_view()`/IPC `status` 仅含白名单键（service_pid/connected/mode/
  webdav_port/mounted/mount_path/storage/error/generation），禁止 username/password/token/私钥/CA 内容；
  `status` 只读内存视图，不触发 refresh/publish/写。

### FR-6 平台/协议契约审计（M01–M11）
- FR-6.1 配置以常驻服务为权威：已保存值覆盖默认值；生产无参服务加载 `config.json`；`set_config`
  原子持久化且 `auto_start` 真正联动 LaunchAgent；GUI 经 `get_config` 初始化，失败回滚而不假装生效（M01）。
- FR-6.2 启动器同时支持源码目录与 `.app/Contents/MacOS → ../Resources` 布局，文件模式 0755，
  校验 Python 3.10+、ssl、tkinter；单实例只由服务 flock 决定，禁止宽泛 `pgrep -f` 跳过启动（M02）。
- FR-6.3 常驻自愈连续两轮断线时只卸载己方卷，恢复后按 `auto_mount` 重挂；用户显式卸载意图优先；
  已挂载但 `mount_point_alive` 连续失活时执行同样恢复链（M03）。
- FR-6.4 DAV 候选必须同时满足成功状态和明确 `DAV` 能力头；`sso_login` 用 `pgrep -x` 精确识别；
  并发探测发布首个 confirmed 后不得等待无关慢探针（M04/M06/M08）。
- FR-6.5 HTTP/1.1 拒绝路径必须设置 `close_connection`；`Expect: 100-continue` 必须在发送 100 前完成
  目标、认证、chunked、Content-Length 与体积门禁；进入读取请求体前获取有界并发槽位（M05/M09）。
- FR-6.6 TLS 闭锁冷却期快速失败；原生 `ssl.SSLError` 必须先分类再计入闭锁；环境变量解析后的
  `server_name` 必须用于实际 SNI/hostname；`Upstream.adopt` 在锁内原子采纳；IPC reader 复用并关闭；
  服务启动失败释放 socket 与 flock（M07/M09/M10）。
- FR-6.7 手动挂载成功清除服务层“用户已卸载”意图，失败不清除；上游 URL 只编码 path、原样保留 query；
  NetFS helper 超时必须 kill 后再次 communicate 回收；stdin 凭证只移除 LF/CRLF 分隔符，不裁剪合法空白（M10）。
- FR-6.8 `netfs_mount.m` 必须在 GitHub Actions macOS runner 使用 Apple SDK 分别编译 arm64/x86_64，
  以 `lipo -create` 合并 Universal Mach-O；交付二进制模式 0755，`lipo -verify_arch arm64 x86_64` 通过，
  `--protocol` 精确输出 `MWDAV_MOUNT_PROTOCOL=2`。CI 上传已验证产物；Finder/真实 NAS 挂载仍属 B 层（M11）。

## 3. 安全需求（Security Requirements）

- SR-1 数据面访问控制（对应 T01 / P0）：入站**始终 require、fail-closed**，本分支不提供
  `optional`/匿名降级开关。目标已配齐凭证时，入站 Basic 必须与目标一致，否则返回
  `401 + WWW-Authenticate: Basic realm="MiNAS"`；目标未配齐时返回 `503`（未配置），
  **绝不匿名转发**。若实测 macOS NetFS 不携带凭证导致无法挂载，这是**兼容性失败**，
  必须在 B 层手工验收中显式登记"未通过/未完成"，不得以匿名或弱认证绕过。
  只绑定回环地址不能隔离同机其他用户/进程，600 状态文件权限也保护不了"无需读密码即可用代理"。
- SR-2 凭证清理与轮换提示：仓库源码与历史脚本中不得出现真实账号/密码/私钥文件名；
  所有探测脚本改读环境变量/显式开关，默认拒绝执行。已暴露的历史凭证视为已泄露，
  文档必须提示所有者轮换/吊销，本分支不代为轮换、不复制到新文件。
- SR-3 传输身份（对应 T11 / P1、D05/D06）：**发现侧探测与数据面转发都**必须完整校验信任链与身份，
  `CERT_REQUIRED` + `check_hostname=True`，禁止 `CERT_NONE`/`check_hostname=False` 静默放行。
  配置入口（冻结）：
  - `config.json`：`tls_server_name` **必填**（同时用于 SNI 与主机名校验，受信来源）；
    `tls_ca_file` / `tls_cert_file` / `tls_key_file` 为可选绝对路径，缺省回退本地官方证书目录自动发现。
  - 环境变量优先：`MWDAV_TLS_SERVER_NAME` / `MWDAV_TLS_CA_FILE` / `MWDAV_TLS_CERT_FILE` /
    `MWDAV_TLS_KEY_FILE`，覆盖 config.json 同名项。
  - 期望身份只能来自上述受信配置，禁止从未验证的对端证书 CN/SAN 或 pool 信息自动学习，回环不豁免；
    未配置 `tls_server_name` 时 fail-closed（`TlsConfigError`），控制面不得伪造主机名校验。
  - **不存在 `tls_verify=false` / `MWDAV_TLS_VERIFY` 等任何跳过校验开关**（该降级提案已被拒绝），
    文档与验收一律禁止 insecure 降级。CA 缺失、客户端证书/私钥加载失败一律 fail-closed 并给出可诊断原因。
  文档不得宣称"客户端证书双向认证确保连接的是真正 NAS"——客户端认证与服务端验证是两件事；
  官方 P2P 隧道真实证书链在仓库外，本规格只约束本仓库代码与措辞。
- SR-4 凭证不落 argv：NetFS 挂载、代理 CLI、curl 探活等不得把密码放在命令行参数；改用受控管道、
  0600 状态文件、环境或文件描述符；代理 CLI 不提供 `--username/--password`。
- SR-5 状态文件安全：确需落盘的凭证以 600 权限、目录 700、原子方式写入；日志不得记录
  密码、Authorization、私钥或完整用户文件路径。
- SR-6 默认安全：默认 `pytest`/测试入口只运行离线安全脚本，不得触网、不得挂载、
  不得调用根目录旧探针；真实 NAS 操作必须显式 `--allow-live` 并打印警告。

## 4. 非功能需求（NFR）

- NFR-1 可移植：本地自动化测试在 Linux（CI）与 macOS 均可运行；不依赖 macOS 专有命令作为测试前提。
- NFR-2 可观测：诊断字段包含发现代次、候选/选中端口、目标更新时间、失败阶段
  （connect/tls/http/dav）、TLS 错误类型、HTTP 状态、DAV 能力、证书是否加载成功；不含敏感值。
- NFR-3 性能目标保持现状并可量化：连接复用只在身份变化时失效；大目录 PROPFIND 缓存 TTL 可配。
- NFR-4 可维护：每条缺陷（T01–T14、D02/D04/D05/D06/D07/D08、M01–M11）在 traceability 矩阵中有唯一编号、
  需求、设计归属、回归测试与验收层；无悬空占位。

## 5. 验收分层（摘要，详见 acceptance.md）

- A 层 · 本地自动化：离线单元/契约/回环场景脚本，CI 必跑（见 tests/）。
- B 层 · macOS 手工实机：Finder 挂载、端口轮换、多用户隔离、大文件传输等，清单见 acceptance.md。
- S 层 · 常驻服务（S01–S07）：Linux/loopback 自动验收已关闭；macOS LaunchAgent/Finder/NetFS 为 B 层未执行。
- 任一 A 层失败不得合并；B 层未完成项必须在发布说明中显式标注"未实机验证"。

## 6. 需求编号索引（与 traceability 矩阵对应）

| 需求 | 覆盖缺陷 |
|---|---|
| FR-1.1 | D08 |
| FR-1.2 / FR-1.3 / FR-1.4 / FR-1.5 | D07、D02、T03、GUI 退出限制(B-9) |
| FR-2.3 | T04、T05、T06 |
| FR-2.4 | T13 |
| FR-2.5 | T12 |
| FR-2.6 | T02 |
| FR-2.7 | T14、D04 |
| FR-2.8 | T03 |
| FR-3.1 / FR-3.2 / FR-3.3 | T07、T08、T09 |
| FR-4.1 | T10 |
| SR-1 | T01 |
| SR-3 | T11、D05、D06 |
| FR-5.1 ~ FR-5.7 | S01 ~ S07（Linux/loopback 自动验收已关闭；LaunchAgent/Finder 实机 B 层未执行） |
| FR-6.1 ~ FR-6.8 | M01 ~ M11（M11 为 macOS CI 构建验收；真实 Finder/NAS 行为见 B 层） |

D01（无竞态正常切换 200）与 D03（单写者内存对照）是**控制/对照场景**，不是待修缺陷，
用于回归时证明"端口变化本身可成功、单写者因果成立"。

> S01–S07 常驻服务架构在 Linux/loopback 自动验收关闭；macOS LaunchAgent 保活与 Finder/NetFS 实机为 B 层未执行。
