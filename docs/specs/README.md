# specs/ 模块设计目录

本目录存放模块级详细设计，对齐 `../sdd/requirements.md` 的需求编号。

| 文件 | 覆盖代码 |
|---|---|
| `proxy.md` | 数据面：`mountapp/mwdav_proxy.py`（入站控制、转发、缓存、连接池、写重试、TLS 策略） |
| `runtime.md` | 控制面：`mountapp/main.py`、`app_core.py`、`conn_manager.py`（发现、凭证、目标发布、挂载身份、界面） |

## 设计文档必须回答

1. 模块对外接口/数据结构（尤其与另一模块的跨模块契约，见 `../sdd/architecture.md` §3）。
2. 并发模型与状态归属（谁是唯一写者、generation 如何流转）。
3. 错误分类与处理（connect/tls/http/dav 分层；写操作"可能已提交"如何处理）。
4. 对应缺陷编号（T01–T14、D02/D04/D05/D06/D07）在本模块的落点。
5. 本模块 A 层回归测试清单（与 traceability 矩阵一致）。
