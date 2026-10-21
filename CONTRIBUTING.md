# 贡献指南

本项目采用规格驱动（SDD）：**先规格、后设计、再实现、以验收关闭**。不要先改代码再补文档。

## 开始之前

1. 读 `docs/sdd/requirements.md`（主规格）与 `docs/sdd/traceability-matrix.md`（缺陷/需求映射）。
2. 新缺陷/新行为先在矩阵登记编号与验收层，再动手。
3. 改动前对照 `docs/specs/proxy.md`（数据面）与 `docs/specs/runtime.md`（控制面）的既有设计，
   跨模块接口改动需同步两份设计。

## 提交一个改动

1. **规格**：在 requirements.md 确认/新增可验证需求（FR/SR/NFR），无空泛占位。
2. **设计**：在对应 `docs/specs/*.md` 写清接口、并发模型、错误处理。
3. **实现**：不引入未登记需求；不改动历史发布物。
4. **回归测试**：在 `tests/` 加离线用例（mock/临时目录/回环临时证书），先在基线失败、修复后通过。
5. **验收**：`python3 tests/run_tests.py` 全绿；B 层手工项在 acceptance.md 登记或标注未验。
6. **文档同步**：README/使用说明宣称必须与实现一致；用真实测试结果把矩阵状态推进到 `已关闭`。
7. 测试全绿、文档一致后，经审查正常 commit 并提 PR，提交信息关联矩阵编号。

## 测试约定

- A 层离线全部位于 `tests/`，由 `python3 tests/run_tests.py` 与 pytest 收集；
  各子目录按主题组织，不按临时分工独占。
- 禁止：读 `/Applications/小米智能存储.app/...` 真实证书、连真实 NAS、挂载/卸载、
  在测试里写真实密码或调用根目录历史探针。
- 诊断日志不得含密码、Authorization、私钥、完整用户文件路径。

## 安全红线

- 不新增硬编码凭证；历史暴露凭证提示所有者轮换，本仓库不代为轮换、不复制。
- 入站恒 require/fail-closed；不实现 optional 匿名。
- TLS 不提供跳过校验开关；`tls_server_name` 必填、env 优先、fail-closed。
- 历史 ZIP `MiNAS-Mount-Helper-v1.4.zip` 保持不变。`mountapp/netfs_mount` 仅通过 M11 macOS CI
  受控重建：arm64/x86_64、Universal、0755、v2 协议全部验证后方可替换。

## 验收纪律

- 不把 Linux 离线通过说成 macOS 实机通过；未实机项在 acceptance.md 显式标注。
