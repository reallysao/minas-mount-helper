# 小米智能存储 Finder 挂载助手

把「小米智能存储」NAS 直接挂载到 Mac 的 Finder，**异地（NAS 不在本机局域网）也能正常浏览、上传、下载、打开文件**。

## 功能特性

- ✅ 复用官方 App 的 P2P 隧道，无需配置路由器/端口转发
- ✅ 本地稳定地址 `http://127.0.0.1:18445`，Finder 原生挂载
- ✅ 自动提取 WebDAV 凭证，无需手工输入
- ✅ 隧道端口动态变化时自动跟随
- ✅ 断线自动重连、死挂载自动清理
- ✅ 登录时自动启动、连接后自动挂载
- ✅ 存储空间用量按需刷新（点「刷新」拉取一次，不后台轮询）
- ✅ 挂载改用系统 `mount_webdav`，不再触发「不安全的连接」认证弹窗
- ✅ 大目录性能优化（连接复用、元数据快速响应、PROPFIND 缓存）

> 说明：`mountapp/sync_engine.py`（同步模块）当前**未被主程序加载**，本版本入口不提供同步面板，
> 该能力未接入，勿当作已可用功能。

## 工作原理

```
小米NAS ──P2P隧道──▶ 官方App(sso_login 进程, 本地动态端口)
                          │
                          ▼
              本地反向代理 http://127.0.0.1:18445
              （自动注入凭证，全方法透传 DAV 1,2）
                          │
                          ▼
              Finder 原生挂载 → ~/Volumes/127.0.0.1
```

- 复用官方 App 已建立的 P2P 隧道（`sso_login` 进程）
- WebDAV 凭证从官方 App 的本地缓存（LevelDB）自动提取
- 常驻服务内嵌代理（`main.py --service` 前台 `serve()`，无独立代理子进程）；服务是唯一配置发布者，
  持续发现/跟随/A→B 二次发布/自愈。无参 `main.py` 是连接该服务的受保护 Unix socket IPC 客户端；
  `stop` 命令或 SIGTERM 幂等关停。Linux/loopback 自动验收通过；macOS LaunchAgent 保活与 Finder/NetFS
  实机为 B 层未测。

## 系统要求

- macOS 11.0+
- 已安装并登录官方「小米智能存储」App（必须，隧道依赖它）
- **无需安装任何依赖**：应用内置 Python 运行时（PyInstaller 打包），Intel 与 Apple Silicon 通用

## 安装

### 方式一：直接下载（推荐）

从 [GitHub Releases](https://github.com/reallysao/minas-mount-helper/releases) 下载最新版 `.app`，拖入 `/Applications/`。

### 方式二：从源码构建

```bash
git clone https://github.com/reallysao/minas-mount-helper.git
cd minas-mount-helper

# 需要 Python 3.10+ 与 PyInstaller（仅构建用到，运行不需要）
python3 -m pip install pyinstaller
./build_app.sh
```

产物在 `build/小米智能存储挂载助手.app`。PyInstaller 不能跨架构编译：在 Intel 机器上得到 x86_64 包，
在 Apple Silicon 机器上得到 arm64 包。

## 使用

1. 确保已登录并运行官方「小米智能存储」App
2. 打开 `/Applications/小米智能存储挂载助手.app`
3. 状态变为「在线」后点 **挂载**（勾选「连接后自动挂载」则自动完成）
4. Finder 侧边栏出现 `127.0.0.1`，即可像本地磁盘一样使用

> 首次打开如被 Gatekeeper 拦截：右键应用 → 打开 → 再点「打开」。

> 挂载点在 `~/Volumes/127.0.0.1`（个人目录下），不是系统 `/Volumes`。想让它像外接硬盘那样出现在 Finder「位置」里，可执行一次：
> ```bash
> sudo mkdir -p /Volumes/127.0.0.1 && sudo chown "$USER":staff /Volumes/127.0.0.1
> ```

## 加入交流群

有问题、想反馈 bug 或交流使用心得，直接用**小红书扫码进群**：

<img src="docs/images/xiaohongshu-group.jpg" width="320" alt="小红书群聊二维码">

> 该二维码 28 天内有效（至 2026-12-15）。若已过期，请在 [Issues](https://github.com/reallysao/minas-mount-helper/issues) 留言。

## 安全说明

> 本分支处于 SDD 加固阶段，以下区分「已成立的事实」与「目标要求/待实机验证」，
> 完整威胁模型见 `docs/security/threat-model.md`，规格见 `docs/sdd/requirements.md`。

### 数据传输

- 流量经官方 P2P 隧道 + HTTPS，外部网络只能看到加密流量，看不到文件内容。
- **客户端证书 ≠ 服务器验证**：携带官方 App 客户端证书不等于校验了对端身份，
  "双向认证确保连接的是真正 NAS" 这一说法不成立。服务端身份校验是独立事项：
  代码已落地 `CERT_REQUIRED` + 主机名校验。`server_name` 优先取自显式配置
  （env `MWDAV_TLS_SERVER_NAME` / config 的 `tls_server_name`）；未配置时自动从
  本地隧道端口服务器证书读取 subject CN（仅限 127.0.0.1 回环身份探测，最终校验
  仍是严格 CA 验证 + 主机名匹配，不存在绕过），因此**用户无需手工配置任何设备 ID**，
  更换设备/重绑账号也能自适应。

### 访问控制

- 代理只监听 `127.0.0.1`（本机回环），不对外开端口；但**回环不等于信任**，
  绑定回环本身不能隔离同机其他用户/进程。
- 入站认证策略（2026-11-17 调整，为消除 macOS NetFS 认证弹窗）：
  - **带 `Authorization` 但校验不通过** → 401；目标未配齐 → 503；
  - **完全不带凭证** → 视为本机回环访问直接放行。原因：macOS NetFS 收到任何 `401 + WWW-Authenticate`
    挑战就会拉起 NetAuthAgent 弹「需要认证」，配合同步阻塞式挂载会造成「点完一个还有一个」的弹窗风暴；
  - 代价：同机其他进程可经该回环代理匿名访问 NAS。需要严格隔离时，请不要常驻运行本服务，
    或改用显式账号直连 WebDAV 自行挂载。
- 含密码的状态文件目标权限 600、目录 700；官方 App 客户端私钥应收紧为 600。

### 在公司电脑上使用

| 威胁场景 | 状态 |
|---|---|
| 公司局域网其他电脑扫描/访问 | ✅ 代理只监听回环，外部无法直连 |
| 同机其他用户账户/进程 | ⚠️ 回环代理对匿名请求放行（为消除认证弹窗），同机进程可经代理访问 NAS；需要隔离时请勿常驻运行 |
| 同事使用你已解锁的电脑 | ⚠️ 可访问，离开前务必锁屏（⌃⌘Q） |
| 公司 IT 管理员（MDM/管理员权限） | ⚠️ 理论上可读，管理员本就可访问该电脑一切内容 |

**建议**：敏感个人文件尽量不长期存放在公司设备可访问的位置；离开工位锁屏；不用时在应用里点「卸载」。

## 本版本限制（务必阅读）

- **上传请求体上限 200MiB**：超过 `MAX_REQUEST_BODY` 的 PUT 返回 `413`，不空体转发、连接关闭。
- **下载响应体上限 64MiB**：`MAX_RESPONSE_BODY` 硬限，上游响应超限即中止并 `507`、关闭上游连接；
  v1 不做流式 spool/背压，超大文件下载明确失败而非静默截断。
- **拒绝 chunked 写方法**：`Transfer-Encoding: chunked` 的写请求返回 `501`（v1 不做流式解码）。
- **挂载后端改用系统 `mount_webdav`**（2026-11-17）：不再经 `netfs_mount` 的 `NetFSMountURLSync`，
  避免同步阻塞式确认框；卸载改用 `diskutil unmount force`。`netfs_mount` 源码与二进制仍保留，主流程不再调用。
- **TLS 受信身份必须显式配置**：`tls_server_name` 必填（同时用于 SNI 与主机名校验），
  `MWDAV_TLS_SERVER_NAME/CA_FILE/CERT_FILE/KEY_FILE` 环境变量优先；**无 `tls_verify=false`/`MWDAV_TLS_VERIFY` 跳过开关**，
  缺失受信配置 fail-closed，不从对端证书或连接池自动学习身份。

## 已知限制

- 需要官方「小米智能存储」App 正在运行（P2P 隧道由它维护）
- 隧道偶发瞬时抖动（几秒），稍后自动恢复
- 移动端/远端设备需保持在线，NAS 关机则无法访问
- 大目录首次加载可能较慢（WebDAV 协议特性），已做性能优化
- GUI 窗口关闭只断开 IPC 客户端，常驻服务继续发现/跟随/自愈；macOS LaunchAgent 保活与 Finder/NetFS 实机为 B 层未测
- 仓库内历史 `MiNAS-Mount-Helper-v1.4.zip` 为旧发布包，与当前 HEAD 源码存在漂移，
  且含旧代码；**以 HEAD 源码为准**，本分支不改写历史 ZIP
- 本应用为第三方工具，非小米官方出品

## 开发与测试

- 离线测试：`python3 tests/run_tests.py`（不连真实 NAS、不挂载、不用真实凭证）。
- SDD 文档：`docs/`（主规格、设计、验收、traceability 矩阵、威胁模型）。
- 根目录 `probe_ports.py`、`test_*.py` 是历史人工探针，默认拒绝执行，需 `--allow-live` + 环境凭证。

## 项目结构

```
minas-mount-helper/
├── README.md                 # 本文件
├── 使用说明.md                # 详细使用说明与修复记录
├── AGENTS.md / CONTRIBUTING.md  # 协作须知与贡献流程
├── build_app.sh              # 一键构建内置运行时的 .app
├── docs/                     # SDD 规格/设计/验收/矩阵/威胁模型 + 文档配图
│   ├── sdd/                  # requirements / architecture / acceptance / process / matrix
│   ├── specs/                # proxy.md（数据面）、runtime.md（控制面）
│   ├── security/             # 威胁模型
│   └── images/               # 文档配图（含交流群二维码）
├── tests/                    # 离线自动化（run_tests.py 入口；CI 只跑这里）
├── .github/workflows/        # CI（仅离线脚本）
├── mountapp/                 # 核心源码
├── probe_ports.py            # 历史人工探针（默认拒绝，需 --allow-live）
├── test_*.py                 # 历史人工探针（默认拒绝，需 --allow-live）
└── MiNAS-Mount-Helper-v1.4.zip  # 历史发布包（常规改动不改写，后续可受控重建）
```

## 免责声明

本软件为第三方开源工具，与小米公司无任何关联。使用本软件所产生的任何风险由用户自行承担。请遵守小米智能存储的使用条款。

## 许可证

MIT License

## 修复记录（2026-10-22，通用化 + 挂载状态修复）

| 问题 | 根因 | 修复 |
|---|---|---|
| 挂载实际成功但 App 显示未挂载/离线 | `parse_mount_table` 解析 WebDAV URL 源（`http://127.0.0.1:18445/`）时把 host 误解析成 `http`、port 解析成 `None`，导致挂载认领失败 | 挂载表解析支持 `scheme://host:port/path` 格式，正确提取 host/port |
| 需手工配置 `tls_server_name`（设备 ID），换设备/换账号即失效 | 严格 TLS 要求显式服务器名，但设备 ID 因设备而异且官方 App 不提供配置入口 | 新增 `discover_server_name`：本地回环身份探测自动读取隧道证书 subject CN（`nas.*`）；未配置时自动获取，无需任何手工配置；仍保持 CERT_REQUIRED fail-closed |
| 启动器依赖豆包沙箱 Python 路径，其他 Mac 无法运行 | launcher 优先查找 `~/Library/Application Support/Doubao/sandbox_runtime/...`（本机专属） | 重写启动器：Homebrew（Intel/Apple Silicon）→ PATH → 系统 查找链；GUI 单实例匹配精确化（不再误拦常驻服务） |
| 代理 CA 兜底路径仅认标准安装位置 | 小米 App 若装在非标准位置则 CA 缺失 | CA 兜底增加 `~/Library/Application Support/*/cert` 多路径扫描 |

## 修复记录（2026-10-22，v1.6b：内置运行时修复"无法打开"）

| 问题 | 根因 | 修复 |
|---|---|---|
| 下载安装后双击无反应、图形界面不出现 | 启动器要求系统存在 Python 3.10+；多数 Mac 只有系统 3.9，且旧启动器在找不到时静默用 3.9 运行导致直接崩溃 | **应用内置 Python 运行时**（PyInstaller 打包，含 tkinter/ssl/netfs_mount 调用链），双击即用、零依赖；Intel/Apple Silicon 双架构通用（netfs_mount 为 universal binary） |

## 修复记录（2026-11-17：认证弹窗风暴、存储信息空转）

| 问题 | 根因 | 修复 |
|---|---|---|
| 弹出大量「不安全的连接 / 需要认证」对话框，点完一个还有一个，电脑卡顿 | 代理对所有未认证请求（含 `OPTIONS` 与无凭证 `PROPFIND`）返回 `401 + WWW-Authenticate`，macOS NetFS 收到挑战即拉起 NetAuthAgent；`netfs_mount` 的同步挂载被该框阻塞 40s；自愈循环卸完立刻重挂，无冷却无上限，每次重挂再弹一次 | `OPTIONS` 免认证返回 `200`；本机回环的匿名请求放行（带凭证仍校验，失败 401）；挂载改用 `mount_webdav`、卸载用 `diskutil unmount force`；重挂加指数退避（30s→300s）与连续 3 次失败熔断 |
| 存储空间长期显示「获取失败」 | 后台每 60s 轮询、实际每 5 分钟向 NAS 侧逐端口做 TLS 探测，接口/端口变化即失败，失败后继续空转；探测单端口最多 5s 超时 | 移除后台轮询，改为 IPC `storage` 按需拉取（UI 点「刷新」才请求一次）；取不到时显示「点刷新获取」，不再报错 |
| 挂载点在 `/Volumes` 下创建失败 | `/Volumes` 属 root，普通进程 `mkdir` 报 Permission denied | 默认挂载点改为 `~/Volumes/127.0.0.1` |
| 启动器依赖本机私有 Python 路径，别人下载后无法运行 | 修复期为了让热更新生效，临时硬编码了某台机器沙箱内的 Python 3.14 | 全部逻辑改为走内置 `mwdav_runtime`（PyInstaller 打包、自带解释器），源码不再出现任何本机路径 |

## 致谢

感谢以下朋友对本项目的帮助：

- **MilkChan01**（[@MilkChan01](https://github.com/MilkChan01)）—— 提交 PR #2：修复端口选择回归、补充文档契约与静态审计、构建并验证 Universal 版 netfs_mount 等多项改进
- **🍀浮生戚戚** —— 测试与反馈支持
