# AGENTS.md — 仓库协作须知

## 仓库是什么

macOS 第三方工具：把「小米智能存储」官方 App 的 P2P 隧道暴露的 WebDAV，经本地回环代理
`127.0.0.1:18445` 供 Finder/NetFS 原生挂载。非小米官方出品。

## 常用命令

```bash
# 离线自动化（默认：不连真实 NAS、不挂载、不用真实凭证）
python3 tests/run_tests.py -v
pytest                       # 等价；pytest.ini 已把 testpaths 限定到 tests/

# 构建 netfs_mount（需要 Xcode Command Line Tools，macOS）
clang -framework Foundation -framework NetFS -o mountapp/netfs_mount mountapp/netfs_mount.m
```

## SDD 文档入口（先规格后代码）

- 主规格 `docs/sdd/requirements.md`；需求↔缺陷↔测试映射 `docs/sdd/traceability-matrix.md`
- 验收 `docs/sdd/acceptance.md`（A 层离线 / B 层 macOS 实机，不得互相替代）
- 流程门禁 `docs/sdd/process.md`；威胁模型 `docs/security/threat-model.md`
- 模块设计：`docs/specs/proxy.md`（数据面）、`docs/specs/runtime.md`（控制面）
- 改动先在矩阵登记编号，需求可验证、无空泛占位；修复后用真实测试结果更新矩阵状态，不要留待定映射。

## 安全约束

1. TLS：`tls_server_name` 必填受信配置（SNI+hostname），`MWDAV_TLS_*` 环境变量优先，
   `CERT_REQUIRED`+主机名校验、fail-closed；**不存在 `tls_verify=false`/`MWDAV_TLS_VERIFY` 跳过开关**。
2. 入站恒 require/fail-closed：缺/错凭证 401、目标未配齐 503，无匿名/optional 降级；
   NetFS 不传凭证记兼容性失败（B-7），不以关闭认证绕过。
3. 不在源码/文档/测试书写真实账号、密码、私钥、证书文件名 uid；历史已暴露凭证提示所有者轮换。
4. 默认测试离线；根目录 `probe_ports.py`、`test_*.py` 是历史人工探针，默认拒绝（exit 2），
   需 `--allow-live` + `MINAS_TEST_*` 环境凭证，CI/入口永不调用。
5. 不宣称"客户端证书确保连接真正 NAS"（客户端认证 ≠ 服务器验证）；未接入功能（如 sync_engine）
   不得在用户文档宣称可用。

## 发布物与提交

- 历史发布 ZIP `MiNAS-Mount-Helper-v1.4.zip` 不在常规改动中改写。`mountapp/netfs_mount` 仅允许按
  M11 受控流程由 macOS CI 从 `netfs_mount.m` 双架构编译、校验并替换；禁止手工编辑或提交未验证二进制。
- 测试通过、文档与实现一致后，经审查可正常 commit 并提 PR；提交信息对应矩阵编号。
- macOS 实机项（Finder/NetFS/多用户/Apple Silicon）属 B 层，本地 Linux 无法替代，未验须显式标注。
