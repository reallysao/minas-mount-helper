# 总体设计（Architecture Overview）

> 本文件只定义模块边界、职责划分与跨模块契约；模块内部设计分别写入
> `docs/specs/proxy.md`（数据面）与 `docs/specs/runtime.md`（控制面）。本文件不替代二者。

## 1. 现状基线分层（已随 T/D 项 A 层关闭；目标见 §1b）

```
┌─────────────────────────── 控制面（C: main.py / app_core.py / conn_manager.py） ──────────────────────────┐
│  Tkinter 界面 │ AppCore 编排（挂载/自愈/自启动）│ ConnManager（PID/端口发现、凭证、存储统计）                  │
│  唯一目标快照写者：发现 → 校验 → 原子发布（generation + updated_at），慢查询异步不阻塞发布（FR-1）          │
└───────────────┬──────────────────────────────────────────────────────────────────────────────────────────┘
                │ 不可变目标快照（只读消费；读取无副作用；原子替换；schema/version/generation）
                ▼
┌─────────────────────────── 数据面（P: mwdav_proxy.py 独立进程） ────────────────────────────────────────┐
│  回环 :18445 入站访问控制（SR-1）│ 路径/Destination 改写 │ 流式转发+背压（FR-2.6）                      │
│  PROPFIND 契约缓存（Depth/属性/代次键、命名空间中立、双端失效、硬容量）（FR-2.3/2.4）                      │
│  HEAD 语义（FR-2.5）│ 写操作安全重试（FR-2.7）│ 连接池按身份分桶（FR-2.8）│ 分层诊断（NFR-2）            │
└───────────────┬──────────────────────────────────────────────────────────────────────────────────────────┘
                │ HTTPS（服务器身份策略见 SR-3；客户端证书按 fail-closed 加载）
                ▼
        官方 App / sso_login P2P 隧道（仓库外，本项目不逆向）→ 小米 NAS
```

Finder / NetFS 挂载只与本地数据面对话，不直接接触控制面。

## 1b. 常驻服务架构（FR-5 / S01–S07；Linux/loopback 自动验收已关闭；Service API 见 `specs/runtime.md` §8）

```
┌──────────────── ResidentService（单一 PID，内嵌单 ProxyServer，无独立代理子进程；单实例 flock） ───────────────┐
│  discovery(DiscoveryBackend) │ proxy(内嵌, SR-1 入站/缓存/写安全重试/连接池, adopt(snap)) │ mount(MountBackend) │
│  state publish(publish_target CAS, last_generation) │ ipc server(0700 目录/0600 mwdav.sock, newline JSON)    │
└───────▲───────────────────────────────────────────────┬──────────────────────────────────────────────────────────┘
        │ status 脱敏只读；get_config 非敏感配置          │ 白名单: status/get_config/mount/unmount/open_finder/refresh/set_config/stop
        └────────────────────────────────────────────────┼──────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────▼──────────────────────────────────────────────────────────┐
│  GUI：main.py 无参 = IPC 客户端（展示 + 下发命令；退出不断服务、不持有服务状态）；--service = 前台 serve()       │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
Finder / NetFS ── 本地回环 ──▶ 内嵌 proxy ── HTTPS(SR-3) ──▶ 官方 App P2P 隧道 ──▶ NAS
```

- `main.py --service` → 前台 `serve()` 供 per-user LaunchAgent 拉起保活；GUI 退出只断 IPC，服务循环/跟随/自愈继续。
- `stop` 命令或 SIGTERM 幂等关停（drain→join→停代理→释放锁/socket→退出码 0）；第二实例抢锁失败非零退出。
- 模块接口冻结于 `specs/runtime.md` §8，代理内部设计见 `specs/proxy.md`；本总览不代写二者正文。

## 2. 模块职责矩阵

| 模块 | 分组 | 职责 | 明确不做 |
|---|---|---|---|
| conn_manager.py | 控制面 | 隧道发现、凭证读取（LevelDB/状态文件兜底）、分层探测（connect/tls/http/dav）、可选存储统计异步化 | 不写代理目标快照；不盲目折叠探测结果 |
| app_core.py | 控制面 | 目标发布、MountIdentity 挂载认领、幂等挂载/卸载、串行自愈、自启动 | 不替别的 WebDAV 卷做决定；不把 mount_dir 字符串当已挂 |
| main.py | 控制面 | 界面展示、发送用户意图；通用工具函数从共享模块导入 | 不内联未导入函数；不宣称未接入功能 |
| mwdav_proxy.py | 数据面 | 本地回环代理：入站控制、转发、缓存、连接池、写安全重试 | 不回写目标配置；不盲重试写操作；不关闭服务器校验而不声明 |
| sync_engine.py | 未接入 | 同步模块源码留存，现状是孤儿模块 | 未接入前不得在用户文档宣称可用 |

### 2.1 目标服务内部模块（FR-5，S02；Service API 见 `specs/runtime.md` §8）

`ResidentService` 经构造注入后端，模块只经 Protocol 接口交互：

| 服务内模块 | 接口（冻结，见 runtime §8） | 明确不做 |
|---|---|---|
| discovery | `DiscoveryBackend.refresh(force)→DiscoveryView`、`poll_storage()` | 不直接改挂载；不向 IPC 喂任意目标 |
| proxy（内嵌 ProxyServer，单实例） | `start/stop/is_running/adopt(snap)`，`proxy_factory(port)` 注入 | 不另起子进程；不回写配置 |
| mount/self-heal | `MountBackend.ensure_capable/mount/unmount/own_mountpoint` | 不误认/误卸他卷；fake 不碰真实 /sbin/mount 与 netfs |
| state publish | `publish_target(expected_generation)` CAS、`last_generation()`、`status_view()` | 不被 GUI 直接写；健康仅内存 |
| ipc server | newline-delimited JSON，白名单 `status/get_config/mount/unmount/open_finder/refresh/set_config/stop` | 无 publish/任意凭证命令/代理透传/RCE 面 |

## 3. 跨模块契约（两份 specs 必须一致）

### 3.1 目标快照（控制面 → 数据面）
- 载体：本地文件（schema_version=2），原子替换；字段含 `generation:int, published_at, host, port,
  base, username, password, tls{ca_file,cert_file,key_file,server_name,verify}`。
- 常驻控制面（ResidentService）唯一写，带 `expected_generation` CAS 拒旧代次；GUI 只经 IPC；数据面只读；
  读取不得调用持久化/重置/回写（修 T03、D02）；健康（connected/error/闭锁）只在内存不落盘。
- TLS 配置入口：`config.json -> tls_server_name`（必填，SNI+hostname）、`tls_ca_file/
  tls_cert_file/tls_key_file`（可选绝对路径，本地官方目录兜底）；`MWDAV_TLS_SERVER_NAME/
  CA_FILE/CERT_FILE/KEY_FILE` env 优先；**无 verify=false 跳过开关**，未配置 server_name fail-closed。
- 数据面启动/每请求读取不可变快照；连接池按 `(host,port,generation)` 分桶。

### 3.2 探测结果（conn_manager → AppCore）
- 端口枚举先用 `lsof -a` 将 `sso_login` PID、TCP socket 与 LISTEN 状态做交集；禁止用 lsof
  默认的 OR 选择语义把其他 App 监听端口加入候选（D08）。
- 每个候选端口返回分层结果 `{phase: connect|tls|http|dav, http_status, dav_capable,
  client_cert_loaded, error_type}`，401/mTLS/非 DAV 分别标注为"候选需认证/条件性"，
  不得统一折叠为 False（修 D05、D06）。

### 3.3 挂载身份（AppCore 内部）
- MountIdentity = 规范化源 URL + 端口 + 设备标识 + 挂载点；认领/卸载/自愈前校验（修 T07、T08）。

## 4. 生命周期（现状基线 vs 常驻服务目标形态）

### 4.1 现状基线（已随 T/D 项 A 层关闭，临时形态）

- 形态：GUI 控制面会话 + 独立数据面代理进程；该临时形态**不提供常驻后台服务**。
- 会话内：控制面唯一发布目标（CAS），快路径发现即发布，慢存储查询异步不阻塞；串行健康自愈。
- GUI 退出：`stop()` 只停本线程/慢任务，不杀独立代理；磁盘保留最后目标，代理静态服务已建挂载，
  退出后不再发现新端口、不跟随轮换、不自愈。此为现状限制，不是目标形态。

### 4.2 常驻服务（FR-5 / S01–S07，Linux/loopback 自动验收已关闭）

- 单一服务进程内嵌单 ProxyServer；`main.py --service` 前台 `serve()`；服务是唯一配置发布者，
  发现/跟随/A→B 二次发布/自愈在服务内持续运行。
- GUI（无参 `main.py`）仅为受保护 Unix socket IPC 客户端；关闭窗口只断连接，服务继续。
- `stop` 命令或 SIGTERM 幂等关停（drain 在途→join 线程→停内嵌代理→unlink socket→释放 flock→退出码 0），
  与"关闭窗口"分离；第二实例抢 `service.lock` 失败即非零退出。
- macOS LaunchAgent 登录拉起/KeepAlive 保活与 Finder/NetFS 实机挂载属 B 层，未执行，不宣称已通过。

## 5. 明确排除

- 不实现微服务/分布式；不改动 NAS、路由器、官方 App。
- 不修复历史 v1.4 ZIP 内代码；只在文档说明"发布包与 HEAD 存在漂移，以 HEAD 为准"。
- 不在本仓库验证官方 P2P 隧道内部安全；T11/SR-3 只约束本仓库代码与措辞。
