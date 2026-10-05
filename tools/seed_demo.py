"""生成消费者投诉证据链演示数据并输出关键结果。

用法：
    PYTHONPATH=src python3 tools/seed_demo.py [数据库路径]

不传路径时使用 data/demo.sqlite3，可用以下命令启动服务查看：
    PYTHONPATH=src python3 -m case_system.api --db data/demo.sqlite3
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_system import CaseSystem
from case_system.database import Database


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(db_path: str) -> None:
    system = CaseSystem(Database(db_path))

    # 案件 A：宣传与实际服务不一致，三类分散渠道证据
    a = system.create_case(
        service_name="康养年卡（宣传含每月8次上门护理，实际为0次）",
        claim="要求退还年卡费用 12800 元并按宣传承诺履约",
        complainant={"name": "王秀兰", "phone": "13812345678", "id_no": "110101196001011234", "address": "东城区某街1号"},
        respondent={"name": "康泽康养服务有限公司", "phone": "010-66668888", "id_no": "91110101MA01ABCD2X", "contact": "张某"},
        internal_note="宣传页与合同附件条款冲突，需重点比对",
        actor_role="机构合规员",
        case_no="0910032-011-A",
    )
    aid = a["case_id"]
    pid = a["complainant_id"]

    system.add_evidence(aid, "聊天截图", "微信聊天：销售承诺每月8次上门", h("聊天截图原件"), "当事人",
                        actor_party=pid, file_name="wechat_01.png", summary="销售称“放心，一个月至少上门八次”")
    system.add_evidence(aid, "知情同意", "电子知情同意书（未载明上门次数）", h("知情同意原件"), "当事人",
                        actor_party=pid, file_name="consent.pdf")
    system.add_evidence(aid, "收费记录", "刷卡小票 12800 元", h("收费记录原件"), "机构合规员", file_name="receipt.txt")
    system.add_statement(aid, "销售当面承诺每月上门护理八次，合同是后来签的", "当事人", actor_party=pid)

    system.transition(aid, "提交核验", "机构合规员")
    system.transition(aid, "核验通过", "机构合规员")
    system.transition(aid, "启动调解", "执业人员")
    p1 = system.preserve(aid, "机构合规员")

    # 后补材料：不覆盖旧证据，进入第二次保全
    system.add_evidence(aid, "聊天截图", "完整聊天记录（补传，含时间戳）", h("完整聊天记录"), "机构合规员",
                        file_name="wechat_full.png", supersedes_id=system.list_evidence(aid)[0]["evidence_id"])
    system.add_statement(aid, "补充：宣传页也写了上门护理，现已找到原件", "当事人", actor_party=pid, change_note="找到宣传页")
    system.transition(aid, "调解不成立", "执业人员")
    p2 = system.preserve(aid, "监管人员")

    # 案件 B：重复投诉，独立诉求
    b = system.create_case(
        service_name="同一康养年卡产品",
        claim="仅要求解除合同并退还未消费部分 6000 元",
        complainant={"name": "李建国", "phone": "13900001111", "id_no": "110101195507078888"},
        actor_role="机构合规员",
        case_no="0910032-011-B",
    )
    system.link_cases(aid, b["case_id"], "同一产品同一宣传话术，重复投诉合并研判", "监管人员")
    system.transition(b["case_id"], "提交核验", "机构合规员")

    # 导出与审计演示
    bundle = system.export_case(aid, "监管人员", reason="行政检查调取")
    report = system.verify_chains(aid)

    print("=" * 60)
    print("案件 A：", system.view_case(aid, "监管人员")["case_no"], "| 状态：", system.view_case(aid, "监管人员")["state"])
    print("案件 B：", system.view_case(b["case_id"], "监管人员")["case_no"], "| 状态：", system.view_case(b["case_id"], "监管人员")["state"])
    print("-" * 60)
    print("保全链：第1次", p1["record_hash"][:16], "→ 第2次", p2["record_hash"][:16])
    print("导出指纹：", bundle["export"]["payload_sha256"])
    print("链完整性：", "完好" if report["ok"] else "已被破坏", f"（保全记录 {report['preservation_records']} 条）")
    print("时间线事件数：", len(system.timeline(aid, "监管人员")["events"]))
    print("投诉人视图（对方脱敏）：", json.dumps(
        {k: system.view_case(aid, "当事人", party_id=pid)[k] for k in ("respondent_name", "respondent_phone", "internal_note")},
        ensure_ascii=False))
    print("数据库：", db_path)


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / "data" / "demo.sqlite3")
    main(target)
