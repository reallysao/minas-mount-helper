# 小米智能存储 Finder 挂载助手

把「小米智能存储」NAS 直接挂载到 Mac 的 Finder，**异地（NAS 不在本机局域网）也能正常浏览、上传、下载、打开文件**。

## 功能特性

- ✅ 复用官方 App 的 P2P 隧道，无需配置路由器/端口转发
- ✅ 本地稳定地址 `http://127.0.0.1:18445`，Finder 原生挂载
- ✅ 自动提取 WebDAV 凭证，无需手工输入
- ✅ 隧道端口动态变化时自动跟随
- ✅ 断线自动重连、死挂载自动清理
- ✅ 登录时自动启动、连接后自动挂载
- ✅ 存储空间用量实时显示
- ✅ 大目录性能优化（连接复用、元数据快速响应、PROPFIND 缓存）

## 工作原理

```
小米NAS ──P2P隧道──▶ 官方App(sso_login 进程, 本地动态端口)
                          │
                          ▼
              本地反向代理 http://127.0.0.1:18445
              （自动注入凭证，全方法透传 DAV 1,2）
                          │
                          ▼
              Finder NetFS 原生挂载 → /Volumes/127.0.0.1
```

- 复用官方 App 已建立的 P2P 隧道（`sso_login` 进程）
- WebDAV 凭证从官方 App 的本地缓存（LevelDB）自动提取
- 本地代理是独立进程，App 退出/重启后已挂载的卷不受影响

## 系统要求

- macOS 11.0+
- 已安装并登录官方「小米智能存储」App（必须，隧道依赖它）
- Python 3.9+（运行时自动选择支持 Ed25519 的解释器）

## 安装

### 方式一：直接下载（推荐）

从 [GitHub Releases](https://github.com/reallysao/minas-mount-helper/releases) 下载最新版 `.app`，拖入 `/Applications/`。

### 方式二：从源码构建

```bash
git clone https://github.com/reallysao/minas-mount-helper.git
cd minas-mount-helper/mountapp

# 编译 netfs_mount（需要 Xcode Command Line Tools）
clang -framework Foundation -framework NetFS -o netfs_mount netfs_mount.m

# 构建 app bundle（参考 使用说明.md）
```

## 使用

1. 确保已登录并运行官方「小米智能存储」App
2. 打开 `/Applications/小米智能存储挂载助手.app`
3. 状态变为「在线」后点 **挂载**（勾选「连接后自动挂载」则自动完成）
4. Finder 侧边栏出现 `127.0.0.1`，即可像本地磁盘一样使用

> 首次打开如被 Gatekeeper 拦截：右键应用 → 打开 → 再点「打开」。

## 安全说明

### 数据传输安全

- NAS ↔ Mac 全程 TLS 加密（P2P 隧道 + HTTPS）
- 客户端证书（Ed25519）双向认证，确保连接的是真正的 NAS
- 公司网络只能看到加密流量，看不到文件内容

### 访问控制

- 代理只监听 `127.0.0.1`（本机回环），不对外开任何端口
- 代理强制入站 Basic 认证，无凭证请求一律 401
- 含密码的状态文件权限 `600`，目录权限 `700`
- 官方 App 客户端私钥权限 `600`

### 在公司电脑上使用

| 威胁场景 | 状态 |
|---|---|
| 公司局域网其他电脑扫描/访问 | ✅ 安全，代理只监听回环 |
| 同机其他用户账户 | ✅ 已加固，凭证文件 600 权限 |
| 同事使用你已解锁的电脑 | ⚠️ 可访问，离开前务必锁屏（⌃⌘Q） |
| 公司 IT 管理员（MDM/管理员权限） | ⚠️ 理论上可读，管理员本就可访问该电脑一切内容 |

**建议**：敏感个人文件尽量不长期存放在公司设备可访问的位置；离开工位锁屏；不用时在应用里点「卸载」。

## 已知限制

- 需要官方「小米智能存储」App 正在运行（P2P 隧道由它维护）
- 隧道偶发瞬时抖动（几秒），稍后自动恢复
- 移动端/远端设备需保持在线，NAS 关机则无法访问
- 大目录首次加载可能较慢（WebDAV 协议特性），已做性能优化
- 本应用为第三方工具，非小米官方出品

## 项目结构

```
minas-mount-helper/
├── README.md                 # 本文件
├── 使用说明.md                # 详细使用说明与修复记录
├── 界面预览.png               # 应用界面截图
├── mountapp/                 # 核心源码
│   ├── main.py               # Tkinter 界面主程序
│   ├── app_core.py           # 应用核心逻辑（连接管理、挂载自愈）
│   ├── conn_manager.py       # 连接管理器（凭证提取、端口探测）
│   ├── mwdav_proxy.py        # WebDAV 反向代理（性能优化版）
│   ├── netfs_mount           # 编译好的 NetFS 挂载助手（x86_64）
│   └── netfs_mount.m         # NetFS 挂载助手源码（Objective-C）
└── tests/                    # 测试脚本（可选）
```

## 免责声明

本软件为第三方开源工具，与小米公司无任何关联。使用本软件所产生的任何风险由用户自行承担。请遵守小米智能存储的使用条款。

## 许可证

MIT License
