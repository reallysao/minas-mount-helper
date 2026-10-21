# docs/ 目录导航（SDD）

本仓库采用规格驱动（SDD）：先规格、后设计、再实现、以验收关闭。阅读顺序如下。

## 主链

| 文件 | 内容 |
|---|---|
| [sdd/requirements.md](sdd/requirements.md) | **主规格**：产品目标、功能/安全/非功能需求（FR/SR/NFR） |
| [sdd/architecture.md](sdd/architecture.md) | 总体分层、模块职责、跨模块契约（不含模块内部设计） |
| [sdd/traceability-matrix.md](sdd/traceability-matrix.md) | 14 项离线缺陷 T01–T14 + 动态端口 D01–D07 逐条需求/分组/测试/验收映射 |
| [sdd/acceptance.md](sdd/acceptance.md) | A 层本地自动化 + B 层 macOS 手工实机验收标准 |
| [sdd/process.md](sdd/process.md) | 变更流程、DoD 门禁、测试分层纪律 |
| [security/threat-model.md](security/threat-model.md) | 威胁模型、访问边界、凭证轮换提示（不展示敏感值） |

## 模块设计

| 文件 | 覆盖代码 |
|---|---|
| [specs/proxy.md](specs/proxy.md) | 数据面：`mwdav_proxy.py`（入站控制、转发、缓存、连接池、写重试、TLS 策略） |
| [specs/runtime.md](specs/runtime.md) | 控制面：`main.py`、`app_core.py`、`conn_manager.py`（发现、凭证、目标发布、挂载身份、界面） |

## 规则

- 新增/修改行为必须先登记 traceability 矩阵与 requirements，再写代码。
- A 层测试在 `tests/`；默认离线，不连真实 NAS、不挂载、不用真实凭证。
- 历史 ZIP `MiNAS-Mount-Helper-v1.4.zip` 保持原样。`mountapp/netfs_mount` 按 M11 由 macOS CI
  双架构编译、Universal 合并并校验后受控替换，不接受手工生成的未验证二进制。
