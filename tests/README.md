# tests/ 离线自动化测试

本目录是唯一被默认测试入口（`python3 tests/run_tests.py`、pytest、CI）收集的测试位置。

## 运行

```bash
# 规范入口（CI 使用）：全部离线用例，每模块独立子进程，统计真实 Ran N / skipped；
# 未收集到用例（Ran=0）判失败；子进程带超时。
python3 tests/run_tests.py
python3 tests/run_tests.py -v
python3 tests/run_tests.py --pattern 'test_tls*.py'

# pytest 等价可用（pytest.ini 限定 testpaths=tests；计数以当前实跑为准），
# 但 CI 以 run_tests.py 为准。
pytest
```

## 安全边界（强制）

- 所有用例使用 mock / 临时目录 / 127.0.0.1 临时自签证书；**禁止**访问真实 NAS、
  真实官方 App 证书目录、执行挂载/卸载、读取真实用户配置。
- 仓库根目录的 `probe_ports.py` 与 `test_*.py` 是**历史人工探针**，不在此目录、
  不被收集；它们默认拒绝执行（exit 2），需 `--allow-live` + `MINAS_TEST_*` 环境凭证，
  且会连接真实隧道，只能在人工排障时显式运行。
- CI 只跑本目录离线用例，不调用根探针、不触发真实 NAS 或挂载。

## 目录与主题

| 位置 | 主题 | 覆盖矩阵编号 |
|---|---|---|
| `security/` | 探针默认拒绝、无硬编码凭证、TLS 策略文档冻结、用户文档诚实宣称 | T11、文档守卫 |
| 根级 `test_proxy_unit.py` / `test_state.py` / `test_tls.py` | 数据面契约、读零写、TLS 策略 | T01–T06、T11–T14、D02、D04 |
| 根级 `test_macos_contract_audit.py` | macOS 命令、LaunchAgent、DAV/HTTP/TLS/IPC/NetFS/生命周期文档契约反证 | M01–M10；M11 由 macOS CI 覆盖 |
| `runtime/` | 挂载身份、状态复位、GUI 刷新、慢路径 SNI、`lsof` PID/监听端口 AND 选择 | T07、T08、T09、T10、D07、D08、P2-5 |
| `dynamic/` | 发现分层认证、快路径发布 | D05、D06、D07 |
| `test_acceptance_*` | 独立静态/回环验收（评审者维护） | 全编号交叉复核 |

> 新增用例先在 `../docs/sdd/traceability-matrix.md` 登记编号与状态。
> macOS Finder/NetFS 实机兼容不属于本目录，见 `../docs/sdd/acceptance.md` B 层。

## 新增用例约定

1. 先在 traceability 矩阵登记编号，再写测试（特征 → 回归：基线先失败，修复后通过）。
2. 不读 `/Applications/小米智能存储.app/...` 真实证书；用 openssl 在 tmpdir 生成临时夹具。
3. 诊断输出不得包含密码、Authorization、私钥内容或完整用户文件路径。
4. 不依赖 macOS 专有命令作为 Linux CI 前提；专有行为标为 B 层手工。
