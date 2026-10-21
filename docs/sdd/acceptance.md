# 验收标准（Acceptance Criteria）

- A 层：本地自动化，CI 必跑，脚本在 `tests/`，默认离线。
- B 层：macOS 手工实机，本地无 NAS/macOS 无法替代；结果由执行人在此登记。
- 下表逐项给可判定标准；A 层为本地自动化，B 层为 macOS 手工实机，二者不得互相替代。

## A 层 · 本地自动化（`python3 tests/run_tests.py`，每模块独立子进程）

> 运行状态以 `python3 tests/run_tests.py` 实跑为准（模块数/用例数随代码演进，不冻结旧计数）；
> CAS 已 flock+RLock 锁内 CAS、坏 JSON 拒绝，多轮稳定；pytest 与规范入口等价。
> 已关闭的 T/D 项全部通过其对应回归；B 层 macOS 实机未验，不用 A 层代替。
> S01–S07 常驻服务在 Linux/loopback 自动验收关闭；macOS LaunchAgent/Finder/NetFS 为 B 层未执行。

| 编号 | 通过判定（修复后） | 实际脚本 | 结果（独立子进程） |
|---|---|---|---|
| T01 | 缺/错凭证 → 401+WWW-Authenticate；目标未配齐 → 503；均不触达上游、不注入 NAS 凭证；无 optional 匿名开关 | tests/test_proxy_unit.py::test_a01_*；tests/test_acceptance_proxy_contract.py::test_t01_* | 通过（A 层；B 层实机未验） |
| T02 | 超 `MAX_BODY_SIZE` 请求返回 413，不以空体转发，短体 400 | tests/test_proxy_unit.py::test_a02_*；test_acceptance_proxy_contract.py::test_t02_* | 通过（A 层；B 层实机未验） |
| T03 | 相同快照连续读取不落盘、健康仅内存、不重置池代次 | tests/test_state.py::test_read_is_pure_*；test_acceptance_proxy_contract.py::test_t03_* | 通过（A 层；B 层实机未验） |
| T04 | Depth:0 不返回 Depth:1 缓存列表 | tests/test_proxy_unit.py::test_a04_*；test_acceptance_proxy_contract.py::test_t04_* | 通过（A 层；B 层实机未验） |
| T05 | 命名空间 `<D:response>` 合法列表可进入缓存 | tests/test_proxy_unit.py::test_a05_*；test_acceptance_proxy_contract.py::test_t05_* | 通过（A 层；B 层实机未验） |
| T06 | MOVE/COPY 后源与目标目录缓存均失效 | tests/test_proxy_unit.py::test_a06_*；test_acceptance_proxy_contract.py::test_t06_* | 通过（A 层；B 层实机未验） |
| T07 | 非本应用 WebDAV 卷不被认领、不被卸载 | tests/runtime/test_mount_identity.py | 通过（client 18 项连续通过） |
| T08 | 自定义 mount_dir 须实测为挂载点才返回真 | tests/runtime/test_mount_identity.py | 通过（client 18 项连续通过） |
| T09 | 无端口/失败分支清空旧 connected | tests/runtime/test_state_reset.py | 通过（client 18 项连续通过） |
| T10 | 存储非空时界面刷新不抛 NameError（直接取 main.py 模块级 human_size） | tests/runtime/test_gui_refresh.py | 通过（client 18 项连续通过）；GUI 渲染见 B 层 |
| T11 | CERT_REQUIRED+主机名校验；`tls_server_name` 必填（SNI+hostname），`MWDAV_TLS_*` env 优先，CA/证书/密钥可选绝对路径+本地兜底；不自动学习、127.0.0.1 不豁免；**无 tls_verify=false/MWDAV_TLS_VERIFY 跳过开关**；缺 CA/客户端证书 fail-closed；文档无"确保真 NAS"类宣称 | tests/test_tls.py；tests/test_acceptance_tls_policy.py；tests/security/test_tls_policy_docs.py | 通过；真实设备握手见 B 层 |
| T12 | HEAD 保留上游 Content-Length | tests/test_proxy_unit.py::test_a12_*；test_acceptance_proxy_contract.py::test_t12_* | 通过（A 层；B 层实机未验） |
| T13 | 缓存超上限强制淘汰到上限内 | tests/test_proxy_unit.py::test_a13_*；test_acceptance_proxy_contract.py::test_t13_* | 通过（A 层；B 层实机未验） |
| T14 | 可能已提交的写操作不盲重放；握手前失败才重试并刷新代次 | tests/test_proxy_unit.py::test_a14_*；test_acceptance_dynamic_rotation.py::test_t14_*（真实进程上游计数=1，不重放） | 通过 |
| D01 | 无交错 A→B 切换两次 200（对照保持） | test_acceptance_dynamic_rotation.py::test_d01_* | 对照通过 |
| D02 | 读不回写；新端口不被旧快照覆盖，无 WRONG_VERSION_NUMBER | 进程内 test_acceptance_proxy_contract.py::test_t03_*；test_acceptance_dynamic_rotation.py::test_d02_*（代次不变、错误不回滚） | 通过 |
| D04 | 重试重新加载目标代次；错误处理不回写旧目标 | test_acceptance_dynamic_rotation.py::test_d04_*（不可达 A→发布 B→B 收到请求） | 通过 |
| D05 | mTLS 条件场景：匿名先探，mtls_required/模糊 EOF 且已配证书自动带证书升级一次，不折叠 False | tests/dynamic/test_discovery_auth_layers.py | 通过（client 18 项连续通过） |
| D06 | 401 认证失败与 TLS 失败区分，401 单列 auth_required 候选并重试 | tests/dynamic/test_discovery_auth_layers.py | 通过（client 18 项连续通过） |
| D07 | 快路径成功即发布目标，慢存储查询不阻塞；慢路径 SNI=server_name | tests/dynamic/test_fast_path_publish.py；tests/runtime/test_pool_sni.py | 通过（client 18 项连续通过） |
| D08 | `lsof` 端口发现必须把 PID、TCP、LISTEN 作为 AND 条件，不得混入系统其他 App 监听端口 | tests/runtime/test_tunnel_port_selection.py | 通过（命令契约 + 混合输出 fixture）；macOS 复核见 B-2 |
| M01–M03 | 配置覆盖/持久化/LaunchAgent 联动；launcher 双布局与 0755；断线/死挂载清理及恢复重挂 | tests/test_macos_contract_audit.py | 通过（离线契约）；LaunchAgent/NetFS 见 B 层 |
| M04–M08 | DAV 头确认、503/401 顺序与 close、`pgrep -x`、TLS 闭锁、first-confirmed 不等慢探针 | tests/test_macos_contract_audit.py | 通过（离线契约） |
| M09 | env server_name 真正用于 SNI；adopt 加锁；Expect 门禁；读体前并发槽；IPC reader 关闭；启动失败释放锁；代理 CLI 无凭证参数 | tests/test_macos_contract_audit.py | 通过（离线契约） |
| M10 | 手动挂载复位卸载意图；query 保留；原生 SSL 分类闭锁；helper 超时 kill/reap；stdin 凭证保留空白 | tests/test_macos_contract_audit.py | 通过（离线契约）；Objective-C 编译与真实 NetFS 见 B 层 |
| M11 | macOS runner 双架构编译并合并 Universal Mach-O；0755；arm64+x86_64；`--protocol`=v2；上传 artifact；仓库二进制复核同一契约 | `.github/workflows/netfs-universal.yml` | 通过（macOS CI 构建/协议/架构/权限）；真实 Finder/NAS 挂载仍见 B 层 |

## S 层 · 常驻服务架构（FR-5 / S01–S07，Linux/loopback 自动验收）

> Service API 冻结于 `specs/runtime.md` §8；下列已关闭项由 `tests/` 内独立服务生命周期/IPC/runtime
> 用例在 Linux/loopback 自动通过。macOS LaunchAgent 保活、Finder/NetFS 实机属 B 层，未执行。

| 编号 | 通过判定（runtime §8） | 实际脚本 | 结果 |
|---|---|---|---|
| S01 | `ResidentService` 单进程内嵌单 ProxyServer（`proxy_factory`，无独立子进程）；唯一发布者；单实例 flock，第二实例非零退出；`start/serve/stop` 干净释放端口/锁 | tests/test_acceptance_service_lifecycle.py | 已关闭（Linux/loopback 自动） |
| S02 | discovery/proxy/mount/state/ipc 五模块经构造注入 Protocol 交互，不跨边界直写；`adopt(snap)` 采纳、`publish_target` CAS；生产 `SystemMount` 将服务当前 DiscoveryView 的目标/凭证送入显式凭证挂载入口，不回读独立空连接状态 | tests/test_service_runtime.py；tests/test_production_mount_adapter.py | 已关闭（Linux/loopback 自动） |
| S03 | GUI（无参 main.py）仅 IPC 客户端：`status` 只读、`get_config` 读取非敏感配置、其余白名单命令，不持有/发布服务状态 | tests/test_acceptance_service_ipc.py；tests/test_macos_contract_audit.py | 已关闭（Linux/loopback 自动） |
| S04a | GUI 断开 IPC 后服务循环继续，A→B 二次发布被采纳（自动部分） | tests/test_acceptance_service_lifecycle.py（注入轮换断言二次发布） | 已关闭（Linux/loopback 自动） |
| S04b | macOS LaunchAgent 登录拉起/KeepAlive 保活、Finder/NetFS 实机挂载 | B 层实机 | 未执行（B 层） |
| S05 | `stop` 命令/SIGTERM 幂等：drain→join→停代理→unlink→释放锁→退出码 0；与关窗分离，重复 stop 不报错 | tests/test_acceptance_service_lifecycle.py | 已关闭（Linux/loopback 自动） |
| S06 | 目录 0700、`mwdav.sock` 0600、属主 uid；白名单外命令 `ok=false` 零副作用；无 publish/任意凭证/代理透传；status 脱敏无 username/password/私钥/CA | tests/test_acceptance_service_ipc.py | 已关闭（Linux/loopback 自动） |
| S07 | `status_view()`/`status` 仅白名单键且只读内存视图，不触发 refresh/publish/写；写操作走显式命令 | tests/test_acceptance_service_ipc.py | 已关闭（Linux/loopback 自动） |

## B 层 · macOS 手工实机（待执行人登记）

> 环境：真实 macOS（11+，含 15.7+）、已登录官方 App、在线 NAS；执行人记录日期/系统版本/结果。

| # | 手工步骤 | 通过判定 | 结果 |
|---|---|---|---|
| B-1 | 官方 App 在线，打开助手，挂载 | Finder 出现 127.0.0.1，可浏览/上传/下载/打开 | 未执行 |
| B-2 | 触发隧道端口轮换（重连 App/网络切换），观察代理 | 自动跟随新端口，无 502/WRONG_VERSION_NUMBER，无需重启助手 | 未执行 |
| B-3 | 上传 >200MB 大文件 / 下载大文件 | 无空文件、无内存暴涨、校验和一致 | 未执行 |
| B-4 | 切换 macOS 其他用户账户访问回环 | 按 SR-1 方案：被拒绝或隔离，不能借代理读写 NAS | 未执行 |
| B-5 | 其他 WebDAV 卷已挂载时运行助手自愈 | 不误认、不误卸他卷 | 未执行 |
| B-6 | 断开隧道再恢复 | 死挂载被清理、恢复后自动重挂；用户主动卸载后不被自动挂回 | 未执行 |
| B-7 | 15.7+ NetFS 自动挂载（入站恒 require/fail-closed） | NetFS 携带凭证且自动挂载成功；若 NetFS 不送凭证导致失败，记为**兼容性失败/未完成**，不得用匿名或 optional 绕过 | 未执行（兼容性失败开放项） |
| B-8 | Apple Silicon 原生运行 | Universal helper 已由 macOS CI 验证含 arm64+x86_64；仍需在 Apple Silicon 上做 Finder/NetFS 真实挂载 | 构建门禁通过；实机挂载未执行 |
| B-9 | macOS 实机：关闭 GUI 窗口后观察常驻服务 | 服务（LaunchAgent 保活）继续发现/跟随/自愈；Finder/NetFS 挂载保持 | 未执行（B 层；自动部分见 S04a） |

## 验收签署

| 项 | 执行人 | 日期 | 结论 |
|---|---|---|---|
| A 层全绿 |  |  |  |
| B 层 |  |  |  |
