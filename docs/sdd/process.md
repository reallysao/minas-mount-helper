# SDD 开发流程（Specification-Driven Development）

本仓库采用"先规格、后设计、再实现、以验收关闭"的顺序。任何代码改动都必须能回溯到
`docs/sdd/requirements.md` 的需求编号与 `docs/sdd/traceability-matrix.md` 的缺陷编号。

## 1. 模块边界（按文件，不按临时人员分工）

| 模块 | 文件 | 设计文档 |
|---|---|---|
| 数据面（本地回环代理） | `mountapp/mwdav_proxy.py`、`state_store.py`、`tls_transport.py` | `docs/specs/proxy.md` |
| 控制面（发现/发布/挂载/界面） | `mountapp/main.py`、`app_core.py`、`conn_manager.py` | `docs/specs/runtime.md` |
| 文档/测试基础设施 | `README.md`、`使用说明.md`、`AGENTS.md`、`CONTRIBUTING.md`、`docs/`（除两份 specs）、`.github/`、`tests/`、根目录 `probe_ports.py` 与 `test_*.py` 的凭证隔离 | 本目录 |

- `docs/specs/proxy.md`、`docs/specs/runtime.md` 分别由对应模块维护者编写；
  评审只核对其与主规格、矩阵一致，不代写内容。
- 历史 ZIP `MiNAS-Mount-Helper-v1.4.zip` 保持原样；`mountapp/netfs_mount` 仅按 M11 从
  macOS CI 的已验证 Universal artifact 受控替换。

## 2. 变更流程（每个需求/缺陷走一遍）

1. **登记**：在 `traceability-matrix.md` 新增/更新一行（编号、现象、根因、需求、验收层）。
2. **规格**：在 `requirements.md` 确认或新增需求（FR/SR/NFR），可验证、无占位。
3. **设计**：在对应 `docs/specs/*.md` 写模块设计（接口、数据结构、错误处理、并发模型）；
   跨模块接口在两份设计中保持一致。
4. **实现**：按设计改对应文件；不在实现里新增未登记需求。
5. **回归测试**：在 `tests/` 新增/更新 A 层脚本，先让它在基线失败、修复后通过（特征→回归）。
6. **验收**：A 层本地自动化全绿；B 层手工项在 `acceptance.md` 记录结果或显式标注未验。
7. **文档同步**：README/使用说明的宣称与实现一致；用真实测试结果把矩阵状态更新为 `已关闭`。
8. **提交**：测试全绿、文档一致后，经审查正常 commit 并提 PR，提交信息关联矩阵编号。

## 3. 门禁（Definition of Done）

- [ ] 矩阵中该项状态推进到 `已关闭`，且回归脚本存在于 `tests/`。
- [ ] `python3 tests/run_tests.py`（默认，离线）全绿；不触网、不挂载、不用真实凭证。
- [ ] 无新增硬编码凭证；旧根探针默认拒绝执行（见 `tests/README.md` 隔离说明）。
- [ ] 文档安全声明与代码一致（尤其 mTLS ≠ 服务器验证、未接入功能不宣称）。
- [ ] 历史 ZIP 未修改；`netfs_mount` 若变化，必须来自 M11 macOS CI artifact 且通过双架构/协议/权限校验。
- [ ] B 层未完成项已在发布说明/acceptance.md 显式标注"未实机验证"。

## 4. 测试分层

| 层 | 位置 | 运行方式 | 能否连真实 NAS/挂载 |
|---|---|---|---|
| A1 离线单元/契约 | `tests/`（security/contract/runtime/dynamic 等子目录） | `python3 tests/run_tests.py` | 否（mock/夹具/临时文件/回环临时证书） |
| B 手工实机 | 无脚本，清单在 `acceptance.md` | 真实 macOS + 官方 App + NAS | 是，人工执行 |

根目录旧 `test_*.py` / `probe_ports.py` 是历史人工探针，**不属于 A 层**，默认不被
pytest/run_tests 收集；需 `--allow-live` + `MINAS_TEST_*` 环境凭证才人工运行。

## 5. 协作纪律

- 不轮换、不复制、不在新文件中书写历史暴露的真实凭证；只在安全说明提示所有者轮换。
- 不改历史 ZIP；`netfs_mount` 仅从 M11 macOS CI 已验证 artifact 受控更新。
- 不把 Linux 离线通过说成 macOS 实机通过；未实机项显式标注。
- 评审报告等有价值产物存入个人 artifacts 目录，并在交付回复中给出路径。
