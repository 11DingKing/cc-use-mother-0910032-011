# 消费者投诉证据链案件系统

面向"项目宣传与实际服务不一致"类消费者投诉的后端案件系统。解决三类痛点：

1. **证据分散且易被覆盖**：聊天截图、知情同意书、收费记录来自不同渠道；系统只追加、不更新，后补材料生成新版本，原证据哈希永久保留；
2. **重复投诉独立诉求易丢失**：重复投诉可关联但不合并，两案各自保留诉求、证据与状态；
3. **敏感信息与处置过程需可控**：查看/导出全部留审计，调解/转执法/撤回/复开受状态机约束，敏感字段按办案角色与当事人归属裁剪。

仅依赖 Python 3.11+ 标准库（SQLite + http.server）。

## 目录

- `domain/contract.json`：领域角色、状态、**状态迁移表**、不变量与样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/case_system/`：案件系统后端。
  - `storage.py`：SQLite 只追加存储 + 证据保全哈希链；
  - `redaction.py`：按角色/当事人归属的敏感字段裁剪；
  - `service.py`：状态机、权限、审计、时间线与导出领域服务；
  - `httpapp.py`：HTTP 接口（标准库）。
- `tools/check_contract.py`：命令行契约摘要检查。
- `tests/`：契约回归、领域服务（20 例）、HTTP 端到端（7 例）测试。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json
```

## 启动服务

```bash
PYTHONPATH=src python3 -m case_system.httpapp --db data/cases.db --port 8080
```

身份通过请求头绑定（内网/演示方案，生产应替换为网关注入）：
`X-Actor-Id`、`X-Actor-Role`（`compliance` 机构合规员 / `practitioner` 执业人员 /
`regulator` 监管人员 / `reviewer` 复核专家 / `party` 当事人），
当事人还需 `X-Party-Id`。建议查看类请求附 `purpose` 说明事由，会写入审计。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/cases` | 建档（可带投诉人） |
| POST | `/cases/{id}/parties` | 追加当事人（complainant/respondent） |
| POST | `/cases/{id}/services` | 登记涉事服务（宣传内容 vs 实际服务） |
| POST | `/cases/{id}/statements` | 提交陈述版本（`supersedes_id` 指向前版本） |
| POST | `/cases/{id}/evidence` | 接收证据摘要（sha256/字节数/来源渠道），自动接收保全 |
| POST | `/evidence/{id}/supplement` | 后补材料：追加版本，不覆盖原证据 |
| POST | `/cases/{id}/preservation` | 独立保全动作（封存/移交/校验），串入哈希链 |
| POST | `/cases/{id}/links` | 关联重复投诉（不合并） |
| POST | `/cases/{id}/transitions` | 状态动作（见下） |
| GET | `/cases/{id}` | 案件详情（字段裁剪 + 审计） |
| GET | `/cases/{id}/evidence` | 证据清单含全部版本 |
| GET | `/cases/{id}/timeline` | 完整时间线 + 保全链校验结果 |
| GET | `/cases/{id}/export?kind=full\|evidence_pack\|timeline` | 导出（断链时 409 禁止导出） |
| GET | `/audit?case_id=...` | 审计查询（仅监管/复核） |

状态动作：`提交核验`、`受理`、`调解`（可重复，不离处置中）、`调解结案`、`转执法`、
`撤回`、`复开`（复开次数累计）、`归档`。来源状态不满足契约时返回 409。

## 四项不变量如何落地

- **证据保全版本**：`evidence_versions` 只追加；主表仅移动当前版本指针；
  每次接收/补充/状态动作写入 `preservation_records`，记录含上一条哈希，
  形成按案件的 SHA-256 哈希链，导出前强制重算校验。
- **重复投诉关联**：`case_links` 为多对多关联，案件表、证据、陈述、状态互不共享。
- **案件状态约束**：迁移规则唯一来源是 `domain/contract.json` 的 `state_transitions`，
  服务层运行时读取并校验，测试直接对契约断言。
- **敏感访问审计**：所有 GET/导出在服务层内写审计（含调用人、用途、结果），
  越权与业务拒绝记 `denied`；`name/phone/id_card/address` 按角色等级裁剪，
  当事人对本人信息全可见、对方仅见姓名，被裁字段列入 `_redacted`。
