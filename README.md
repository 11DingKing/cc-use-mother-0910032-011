# 消费者投诉证据链案件系统

面向“宣传与实际服务不一致”类消费者投诉的后端案件系统。聊天截图、知情同意、收费记录等
分散渠道的材料统一接收并固化；后补材料不能覆盖原证据；任何查看与导出都留下防篡改审计。

## 领域约束（与 `domain/contract.json` 对齐）

- **证据保全版本**：证据、陈述、保全记录、案件事件、审计日志均为只增表，
  SQLite 触发器在数据库层面拒绝任何 UPDATE/DELETE。后补材料以 `supersedes_id`
  指向旧版本，旧记录原样保留；每次保全生成全量快照并按案件构成 SHA-256 哈希链。
- **重复投诉关联**：案件间双向关联、注明理由，两案各自保留独立诉求、材料与状态。
- **案件状态约束**：状态机固化为
  `登记 → 待核验 → 处置中 → 调解中 → 已调解 / 已决定 / 已转执法 / 已撤回 → 已归档`；
  调解必须先进“调解中”，撤回仅限处置终结前，已归档须先“复开”（仅监管人员/复核专家），
  转执法仅监管人员。非法流转返回 409，越权角色返回 403。
- **敏感访问审计**：查看、导出、越权拒绝、对象不存在的访问全部进入全局审计哈希链；
  敏感字段（联系方式、证件号、内部备注等）按“当事人本人/对方”和
  “合规员/执业人员/监管/复核”分别裁剪；导出载荷计算 SHA-256 指纹并登记。

## 目录

- `domain/contract.json`：领域角色、状态、动作、证据渠道与不变量。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/case_system/`：
  - `models.py`：角色、状态、状态机约束矩阵；
  - `database.py`：只增表结构、防篡改触发器、哈希链；
  - `service.py`：案件登记、材料接收、关联、流转、保全、导出、审计、链校验；
  - `projection.py`：敏感字段脱敏投影；
  - `api.py`：零依赖 JSON HTTP 接口（标准库 `http.server`）。
- `tools/check_contract.py`：契约摘要检查。
- `tools/seed_demo.py`：生成“康养年卡”宣传不一致案例的演示数据。
- `tests/`：领域逻辑与 HTTP 端到端回归测试。

## 快速开始

```bash
# 演示数据（两案关联、两轮保全、导出与链校验）
PYTHONPATH=src python3 tools/seed_demo.py data/demo.sqlite3

# 启动接口服务
PYTHONPATH=src python3 -m case_system.api --db data/case_system.sqlite3 --port 8080
```

身份通过请求头传递（HTTP 头不支持中文，使用编码或百分号编码中文）：
`X-Role: compliance | practitioner | regulator | reviewer | party`，
当事人另传 `X-Party-Id: <party_id>`。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 登记案件（涉事服务、独立诉求、双方当事人） |
| POST | `/cases/{id}/evidence` | 接收文件摘要（channel/title/file_sha256，可 supersedes_id） |
| POST | `/cases/{id}/statements` | 追加陈述版本（历史版本不覆盖） |
| POST | `/cases/{id}/links` | 关联重复投诉（双向、独立诉求保留） |
| POST | `/cases/{id}/transitions` | 状态流转（action：提交核验/启动调解/转执法/撤回/复开/归档…） |
| POST | `/cases/{id}/preservations` | 证据保全（全量快照 + 哈希链） |
| GET | `/cases/{id}` | 查看案件（审计留痕、字段裁剪） |
| GET | `/cases/{id}/timeline` | 完整时间线 + 证据来源 + 陈述版本 + 保全 |
| GET | `/cases/{id}/export?reason=` | 导出裁剪后的案件包并固化载荷指纹 |
| GET | `/cases/{id}/audit`、`/audit` | 审计记录（仅办案角色） |
| POST | `/cases/{id}/verify` | 重算保全链与审计链，检测篡改 |

示例：

```bash
curl -X POST http://127.0.0.1:8080/cases \
  -H 'Content-Type: application/json' -H 'X-Role: compliance' \
  -d '{"service_name":"康养年卡（宣传上门护理实际没有）","claim":"退还差价",
       "complainant":{"name":"王秀兰","phone":"13812345678"}}'

curl http://127.0.0.1:8080/cases/<case_id>/timeline -H 'X-Role: regulator'
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```
