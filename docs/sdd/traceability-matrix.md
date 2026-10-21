# Traceability Matrix（需求 ↔ 缺陷 ↔ 设计 ↔ 测试 ↔ 验收）

- 基线：`d25c2516a17a28321df9cdaf4dd7c82662af3d3a`
- 缺陷编号固定为离线验证 14 项（T01–T14，源自 `架构评审/离线特征验证.py`）；
  动态端口原 7 场景编号 D01–D07（源自 `动态端口定位/动态端口复现测试.py`），
  PR #2 macOS 实测追加端口选择回归 D08；公开命令/协议文档契约审计 M01–M11。
- 状态取值：`基线复现`（已在基线复现、未修）/ `修复中` / `已关闭` / `已关闭`。
- 分组：**数据面** = 本地回环代理（mwdav_proxy.py / state_store.py / tls_transport.py）；
  **控制面** = 发现/发布/挂载/界面（main.py / app_core.py / conn_manager.py）；
  **文档测试** = 文档、测试入口、CI、安全隔离。
- 回归测试路径相对仓库根；A=本地自动化（CI），B=macOS 手工实机。

## 离线运行快照

- 规范入口：`python3 tests/run_tests.py`（每模块独立子进程；统计真实 Ran N / skipped，
  Ran=0 判失败；子进程带超时）；模块/用例数以实跑为准，不冻结旧计数。
- CAS 真实竞争已用 flock + RLock 在锁内 CAS、坏 JSON 拒绝，多轮稳定。
- 已关闭的 T/D 项全部通过其回归；D01/D03 为对照场景。B 层 macOS 实机项未验，不用 A 层脚本代替。
- S01–S07（常驻服务，见本文件 S 节）Linux/loopback 自动验收已关闭；macOS LaunchAgent/Finder/NetFS 为 B 层未执行。

## A. 离线 14 项（T01–T14）

| 编号 | 基线现象（复现脚本观测） | 根因定位（基线源码） | 需求 | 分组 | 回归测试（实际） | 验收层 | 状态 |
|---|---|---|---|---|---|---|---|
| T01 | 匿名请求直达 mock 上游，入站不携带 Authorization 也被转发（test_01） | `_check_incoming_auth()` 存在但 `Handler._handle()` 不调用；转发统一注入 NAS 凭证 | SR-1 | 数据面 | `tests/test_proxy_unit.py::test_a01_*`；`tests/test_acceptance_proxy_contract.py::test_t01_*`（缺/错凭证 401+WWW-Authenticate、未配齐 503、上游 0 请求；无 optional 匿名开关） | A（NetFS 兼容另见 B-7） | 已关闭 |
| T02 | 超 `MAX_BODY_SIZE` 的 PUT 被把长度置 0、以空体转发（test_02） | 超限分支 `length=0; body=None` 后继续转发，无 413 | FR-2.6 | 数据面 | `tests/test_proxy_unit.py::test_a02_*`；`test_acceptance_proxy_contract.py::test_t02_*`（超限 413、不空体转发、短体 400） | A | 已关闭 |
| T03 | 配置完全相同，连续两次 `_load_shared_state()` 仍各写盘一次、池版本各 +1（test_03） | 读取经 `UP.update()` 触发 `_persist_state()` 与 `_connection_pool_reset()`；读有副作用 | FR-1.3、FR-2.8 | 数据面 | `tests/test_state.py::test_read_is_pure_*`；`test_acceptance_proxy_contract.py::test_t03_*`（读不落盘、健康仅内存） | A | 已关闭 |
| T04 | Depth:0 PROPFIND 命中 Depth:1 列表缓存并原样返回（test_04） | 缓存键仅 path，不含 Depth | FR-2.3 | 数据面 | `tests/test_proxy_unit.py::test_a04_*`；`test_acceptance_proxy_contract.py::test_t04_*`（Depth0/1 不串缓存） | A | 已关闭 |
| T05 | 带命名空间 `<D:response>` 的列表响应不进入缓存（test_05） | 用 `data.count(b"<response")` 判多条目，命名空间前缀失配 | FR-2.3 | 数据面 | `tests/test_proxy_unit.py::test_a05_*`；`test_acceptance_proxy_contract.py::test_t05_*`（命名空间中立，合法列表可缓存） | A | 已关闭 |
| T06 | MOVE 后目标目录 `/destination/` 旧缓存未失效（test_06） | `_invalidate_propfind_cache(self.path)` 只清源路径，不清目标 | FR-2.3 | 数据面 | `tests/test_proxy_unit.py::test_a06_*`；`test_acceptance_proxy_contract.py::test_t06_*`（MOVE/COPY 双端失效） | A | 已关闭 |
| T07 | 任意 WebDAV 卷（/Volumes/UnrelatedDAV）被认作己方挂载（test_07） | `find_mount_path` 只 grep mount 输出含 "webdav"，不校验来源 URL/设备身份 | FR-3.1 | 控制面 | `tests/runtime/test_mount_identity.py`（非本应用卷不被认领/卸载；仅认 127.0.0.1:18445 己方条目） | A（实机 `/sbin/mount` 表格式见 B 层） | 已关闭 |
| T08 | 配置 `mount_dir` 后 `is_mounted()` 仅凭字符串返回真，不实测（test_08） | 自定义目录分支直接 return | FR-3.2 | 控制面 | `tests/runtime/test_mount_identity.py`（自定义目录须在 mount 表实测归属，裸字符串不算） | A（实机表格式见 B 层） | 已关闭 |
| T09 | 无端口分支只设 error，不清空旧 `connected=True`（test_09） | `refresh()` 在 `not ports` 分支 return 前未重置 connected | FR-3.3 | 控制面 | `tests/runtime/test_state_reset.py`（失败路径清空 connected/mode/luci/webdav 陈旧在线态） | A | 已关闭 |
| T10 | 界面 `refresh()` 调未导入的 `human_size`，存储非空即 NameError（test_10） | main.py 未导入/未定义 human_size（定义在未接入的 sync_engine.py） | FR-4.1 | 控制面 | `tests/runtime/test_gui_refresh.py`（直接取 main.py 模块级 human_size，非本地副本；非空刷新无异常） | A（GUI 渲染见 B 层） | 已关闭 |
| T11 | 上游 TLS 上下文 `CERT_NONE`、`check_hostname=False`（test_11） | 服务器证书校验关闭；加载 CA 不等于校验；server_name 未固定 | SR-3 | 数据面/控制面 | `tests/test_tls.py`；`tests/test_acceptance_tls_policy.py`（正向链+身份握手；缺 server_name/CA/证书不成对/错身份/错链/缺客户端证书闭锁）；`tests/security/test_tls_policy_docs.py`（无 verify=false 开关、env 入口、文档无"确保真NAS"宣称） | A（真实设备握手见 B 层） | 已关闭 |
| T12 | HEAD 响应丢掉上游 Content-Length，按空体改成 0（test_12） | 统一 `Content-Length: len(data)`，HEAD 体为空 | FR-2.5 | 数据面 | `tests/test_proxy_unit.py::test_a12_*`；`test_acceptance_proxy_contract.py::test_t12_*`（HEAD 保留上游长度） | A | 已关闭 |
| T13 | 缓存超过 200 项只删过期项，非硬上限（test_13） | `_set_propfind_cache` 仅清过期，无容量淘汰 | FR-2.4 | 数据面 | `tests/test_proxy_unit.py::test_a13_*`；`test_acceptance_proxy_contract.py::test_t13_*`（硬上限淘汰） | A | 已关闭 |
| T14 | 响应丢失的写操作（MOVE）在连接重置后盲重试一次（test_14） | 异常分支用同一旧快照重发，不区分是否已提交 | FR-2.7 | 数据面 | `tests/test_proxy_unit.py::test_a14_*`；`test_acceptance_dynamic_rotation.py::test_t14_*`（真实进程上游计数=1，不重放） | A | 已关闭 |

## B. 动态端口 7 场景（D01–D07）

| 编号 | 基线现象（复现观测） | 根因定位 | 需求 | 分组 | 回归测试（实际） | 验收层 | 状态 |
|---|---|---|---|---|---|---|---|
| D01 | 无竞态 A→B 两次均 200（正常对照，证明端口变化本身可成功） | 非缺陷；作为回归反证 | FR-1.2（行为保持） | 数据面/控制面 | 对照，不单独标缺陷修复；`test_acceptance_dynamic_rotation.py::test_d01_*` | A | 基线复现（对照） |
| D02 | 代理读旧快照后回写，覆盖刚发布的新端口 B → 502 `WRONG_VERSION_NUMBER`；重发 B 后 200 | 读状态有副作用 + GUI/代理多写者，线程锁无法跨进程串行 | FR-1.3、FR-1.4 | 数据面 | 进程内 `test_acceptance_proxy_contract.py::test_t03_*`（读不落盘）；`test_acceptance_dynamic_rotation.py::test_d02_*`（代次不变、错误不回滚） | A | 已关闭 |
| D03 | 内存中仅禁用代理 `_persist_state`（仓库不改）→ 200/200，B 保留（因果对照，非修复） | 非缺陷；证明单写者即消除回滚 | FR-1.3（因果证据） | 文档测试 | 不单独建测试；在 D02 回归中以注释/对照说明 | A | 基线复现（对照） |
| D04 | 首次握手失败后发布 B，重试两次仍打旧 A，B 无请求；错误处理又把状态滚回 A | 重试取一次旧快照；`UP.update(error=...)` 持久化旧目标 | FR-2.7、FR-1.4 | 数据面 | `test_acceptance_dynamic_rotation.py::test_d04_*`（不可达 A→发布 B→B 收到请求）；安全方法重读快照重试 | A | 已关闭 |
| D05 | 要求 mTLS 的端点：原探测 False；带夹具客户端证书 OPTIONS 200；服务端 `PEER_DID_NOT_RETURN_A_CERTIFICATE`（条件性，NAS 真实策略未知） | `probe_webdav` 不带客户端证书，401/TLS/非 DAV 统一折叠 False | SR-3、FR-1（分层结果） | 控制面 | `tests/dynamic/test_discovery_auth_layers.py`（匿名剥离证书先探；mtls_required/模糊 EOF 且已配证书→自动带证书升级一次；verify_failed 不换证书掩盖；未配证书 fail-closed） | A（真实设备 mTLS 策略见 B 层） | 已关闭 |
| D06 | 匿名 OPTIONS 401 被识别为失败；携带认证后 200（HTTP 认证失败 ≠ TLS 失败） | HTTPError 全折叠为 False | SR-3、FR-1 | 控制面 | `tests/dynamic/test_discovery_auth_layers.py`（401 Basic 挑战单列 auth_required 候选，带凭证重试转 confirmed，不丢弃） | A | 已关闭 |
| D07 | 阻塞的可选 pool 查询期间已识别新端口但不发布，释放后才更新 | 慢路径存储统计在快路径发布之前同步执行；AppCore 等 refresh 返回才发布 | FR-1.2 | 控制面 | `tests/dynamic/test_fast_path_publish.py`（快路径成功即发布，不被慢查询阻塞）；`tests/runtime/test_pool_sni.py`（慢路径 SNI/主机名=server_name，不用 127.0.0.1 冒充） | A | 已关闭 |
| D08 | macOS 实测 NAS 显示离线；端口发现把系统其他 App 的监听端口一并纳入候选，真实隧道端口被大量无关探测淹没 | `lsof -p PID -iTCP -sTCP:LISTEN` 缺少 `-a`；lsof 默认对选择条件做 OR 而非 AND | FR-1.1 | 控制面 | `tests/runtime/test_tunnel_port_selection.py`（命令必须含 `-a`，且位于 PID 与网络/状态选择之前；fixture 输出只解析目标 PID 列） | A（macOS 实测复核仍见 B-2） | 已关闭 |

> 冻结事实：入站恒为 require/fail-closed（缺/错凭证 401、未配齐 503），**无 optional 匿名开关**；
> NetFS 不送凭证属兼容性失败，登记 B-7，不用匿名绕过。

## S. 常驻服务架构（S01–S07，FR-5；Linux/loopback 自动验收已关闭）

> Service API 冻结于 `specs/runtime.md` §8；S01/02/03/05/06/07 由 tests/ 内独立服务生命周期/IPC/runtime
> 用例在 Linux/loopback 自动通过关闭。S04 拆为自动部分（GUI 断开后服务继续、A→B 二次发布）已关闭，
> macOS LaunchAgent 保活/Finder/NetFS 实机为 B 层未执行。

| 编号 | 要求（runtime §8） | 需求 | 分组 | 实际验收脚本 | 验收层 | 状态 |
|---|---|---|---|---|---|---|
| S01 | ResidentService 单进程内嵌单 ProxyServer，无独立子进程；唯一发布者；单实例 flock，第二实例非零退出；start/serve/stop 干净释放 | FR-5.1 | 服务 | test_acceptance_service_lifecycle.py | A（Linux/loopback） | 已关闭 |
| S02 | discovery/proxy/mount/state/ipc 五模块经构造注入 Protocol 交互，不跨边界直写；adopt(snap)+CAS；SystemMount 使用服务显式传入的目标/凭证，不回读独立空 ConnManager | FR-5.2 | 服务 | test_service_runtime.py；test_production_mount_adapter.py | A（Linux/loopback） | 已关闭 |
| S03 | GUI（无参 main.py）仅 IPC 客户端，status 只读/白名单命令，不持有服务状态 | FR-5.3 | 服务 | test_acceptance_service_ipc.py | A（Linux/loopback） | 已关闭 |
| S04a | GUI 断开 IPC 后服务循环继续，A→B 二次发布被采纳 | FR-5.4 | 服务 | test_acceptance_service_lifecycle.py（注入轮换） | A（Linux/loopback） | 已关闭 |
| S04b | macOS LaunchAgent 登录拉起/KeepAlive、Finder/NetFS 实机挂载 | FR-5.4 | 服务 | B 层实机 | B | 未执行（B 层） |
| S05 | stop/SIGTERM 幂等：drain→join→停代理→unlink→释放锁→退出码 0；与关窗分离 | FR-5.5 | 服务 | test_acceptance_service_lifecycle.py | A（Linux/loopback） | 已关闭 |
| S06 | 目录 0700/socket 0600/属主 uid；白名单外命令零副作用；无 publish/任意凭证/透传；status 脱敏 | FR-5.6 | 服务 | test_acceptance_service_ipc.py | A（Linux/loopback） | 已关闭 |
| S07 | status_view()/status 仅白名单键、只读内存视图，不触发 refresh/publish/写；写操作走显式命令 | FR-5.7 | 服务 | test_acceptance_service_ipc.py | A（Linux/loopback） | 已关闭 |

## C. 文档/平台契约审计（M01–M11）

| 编号 | 文档反证与故障模式 | 需求 | 实现落点 | 回归测试 | 验收层 | 状态 |
|---|---|---|---|---|---|---|
| M01 | 默认配置覆盖已保存值；服务未加载/持久化；GUI 假显示 auto_start | FR-6.1 | app_core/service/main | test_macos_contract_audit.py::test_m01_* | A | 已关闭 |
| M02 | launcher 仅支持 bundle 路径、0644、宽泛 pgrep 误判 | FR-6.2 | launcher.sh | test_macos_contract_audit.py::test_m02_* | A（实机 LaunchAgent 见 B） | 已关闭 |
| M03 | 文档宣称自愈，服务却不清死卷/不检查活性 | FR-6.3 | service/SystemMount | test_macos_contract_audit.py::test_m03_* | A（真实 mount 表见 B） | 已关闭 |
| M04 | OPTIONS 200/207 无 DAV 头也 confirmed，可能选错本地 TLS 服务 | FR-6.4 | conn_manager._probe_once | test_macos_contract_audit.py::test_m04_* | A | 已关闭 |
| M05 | 未配置目标误报 401；Connection: close 未设置 handler flag | FR-6.5 | Handler._handle/_send_simple | test_macos_contract_audit.py::test_m05_* | A | 已关闭 |
| M06 | `pgrep -f` 子串误认无关进程 | FR-6.4 | find_sso_login_pid | test_macos_contract_audit.py::test_m06_* | A（多同名真实进程见 B） | 已关闭 |
| M07 | TLS 闭锁只记录不执行；代理 CLI 可把凭证放 argv | FR-6.6、SR-4 | _send_upstream/CLI | test_macos_contract_audit.py::test_m07_* | A | 已关闭 |
| M08 | executor context 在首个 confirmed 后仍等待慢任务 | FR-6.4 | _parallel_first_confirmed | test_macos_contract_audit.py::test_m08_* | A | 已关闭 |
| M09 | env SNI 值未用于连接；adopt 缺锁；Expect 提前放体；读体前无门禁；IPC reader 泄漏；半启动遗锁 | FR-6.5/FR-6.6 | proxy/client/service | test_macos_contract_audit.py::test_m09_* | A | 已关闭 |
| M10 | 服务挂载不复位卸载意图；query 被编码进 path；原生 SSL 绕过分类闭锁；helper 超时未回收；凭证合法空白被裁剪 | FR-6.6/FR-6.7 | service/proxy/tls/app_core/netfs | test_macos_contract_audit.py::test_m10_* | A（Objective-C 编译/真实 NetFS 见 B） | 已关闭 |
| M11 | 仓库 helper 为旧 x86_64/0644，无法代表 v2 stdin 协议或 Apple Silicon 交付 | FR-6.8 | macOS CI + netfs_mount binary | `netfs-universal` job：双架构编译、lipo、协议、0755、artifact；仓库二进制同契约复核 | macOS CI（真实挂载见 B） | 已关闭 |

## D. 矩阵维护规则

1. 修复一项即把状态改为 `已关闭`，回归脚本在 A 层通过后改 `已关闭`，并在备注补实际测试文件名。
2. 新增缺陷必须先在本矩阵登记编号、需求、分组、验收层，再写代码（SDD 顺序）。
3. D01/D03 为对照场景，永远不标"缺陷修复"；它们用于防止回归时把正常路径改坏。
4. 任何 B 层手工验收项不得用 A 层脚本通过代替；B 层结果记录于 `docs/sdd/acceptance.md`。
5. 跨分组接口边界在两份设计文档（proxy.md / runtime.md）划清，文档测试只核对一致性，不改核心代码。
