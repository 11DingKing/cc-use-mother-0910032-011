"""案件系统核心领域逻辑测试。"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_system import CaseSystem
from case_system.database import Database, GENESIS_HASH
from case_system.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from case_system.models import (
    ROLE_COMPLIANCE,
    ROLE_PRACTITIONER,
    ROLE_REGULATOR,
    ROLE_REVIEWER,
    ROLE_PARTY,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CaseSystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.system = CaseSystem(Database(":memory:"))

    def _make_case(self, claim: str = "要求退还宣传与实际服务差价") -> dict:
        return self.system.create_case(
            service_name="老年康养年卡（宣传含上门护理，实际无）",
            claim=claim,
            complainant={"name": "王秀兰", "phone": "13812345678", "id_no": "110101196001011234", "address": "北京市东城区某街1号"},
            respondent={"name": "康泽康养服务有限公司", "phone": "010-66668888", "id_no": "91110101MA01ABCD2X", "contact": "张某"},
            internal_note="宣传材料疑似与合同条款冲突，重点比对",
            actor_role=ROLE_COMPLIANCE,
        )

    # ---------------------------------------------------------- 状态机

    def test_full_mediation_flow_with_state_constraints(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        self.assertEqual(case["state"], "登记")

        # 登记状态不能直接启动调解
        with self.assertRaises(ConflictError):
            self.system.transition(cid, "启动调解", ROLE_COMPLIANCE)

        self.system.transition(cid, "提交核验", ROLE_COMPLIANCE)
        # 执业人员无权核验通过
        with self.assertRaises(PermissionDeniedError):
            self.system.transition(cid, "核验通过", ROLE_PRACTITIONER)
        self.system.transition(cid, "核验通过", ROLE_COMPLIANCE)
        self.system.transition(cid, "启动调解", ROLE_PRACTITIONER)
        self.system.transition(cid, "调解成立", ROLE_PRACTITIONER)
        view = self.system.view_case(cid, ROLE_REGULATOR)
        self.assertEqual(view["state"], "已调解")

        # 调解成立后不可撤回
        with self.assertRaises(ConflictError):
            self.system.transition(cid, "撤回", ROLE_COMPLIANCE)

        self.system.transition(cid, "归档", ROLE_REVIEWER)
        # 已归档不能直接再调解，必须复开
        with self.assertRaises(ConflictError):
            self.system.transition(cid, "启动调解", ROLE_COMPLIANCE)

    def test_withdraw_and_reopen_constraints(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        self.system.transition(cid, "提交核验", ROLE_COMPLIANCE)
        self.system.transition(cid, "核验通过", ROLE_COMPLIANCE)
        self.system.transition(cid, "撤回", ROLE_COMPLIANCE)
        self.assertEqual(self.system.view_case(cid, ROLE_REGULATOR)["state"], "已撤回")

        # 执业人员无权复开
        with self.assertRaises(PermissionDeniedError):
            self.system.transition(cid, "复开", ROLE_PRACTITIONER)
        result = self.system.transition(cid, "复开", ROLE_REGULATOR)
        self.assertEqual(result["from_state"], "已撤回")
        self.assertEqual(result["to_state"], "处置中")

    def test_enforcement_only_regulator(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        self.system.transition(cid, "提交核验", ROLE_COMPLIANCE)
        self.system.transition(cid, "核验通过", ROLE_REGULATOR)
        with self.assertRaises(PermissionDeniedError):
            self.system.transition(cid, "转执法", ROLE_COMPLIANCE)
        self.system.transition(cid, "转执法", ROLE_REGULATOR)
        self.assertEqual(self.system.view_case(cid, ROLE_COMPLIANCE)["state"], "已转执法")
        self.system.transition(cid, "作出决定", ROLE_REVIEWER)

    def test_complainant_party_can_submit_and_withdraw(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        pid = case["complainant_id"]
        self.system.transition(cid, "提交核验", ROLE_PARTY, actor_party=pid)
        self.system.transition(cid, "撤回", ROLE_PARTY, actor_party=pid)
        # 被投诉人不能撤回
        rid = case["respondent_id"]
        self.system.transition(cid, "复开", ROLE_REGULATOR)
        with self.assertRaises(PermissionDeniedError):
            self.system.transition(cid, "撤回", ROLE_PARTY, actor_party=rid)

    # ---------------------------------------------------------- 证据

    def test_evidence_append_only_and_supersede_keeps_original(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        first = self.system.add_evidence(
            cid, "聊天截图", "微信聊天截图（宣传承诺上门护理）", digest("截图v1"),
            ROLE_PARTY, actor_party=case["complainant_id"], file_name="chat1.png", summary="销售承诺每周两次上门",
        )
        second = self.system.add_evidence(
            cid, "聊天截图", "聊天截图补充（完整对话）", digest("截图v2"),
            ROLE_COMPLIANCE, file_name="chat1_full.png", supersedes_id=first["evidence_id"],
        )
        items = self.system.list_evidence(cid)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["file_sha256"], digest("截图v1"))
        self.assertEqual(items[1]["supersedes_id"], first["evidence_id"])

        # 数据库层面拒绝更新/删除，后补材料无法覆盖原证据
        conn = self.system.db.conn()
        with self.assertRaises(sqlite3.Error):
            conn.execute("UPDATE evidence_items SET file_sha256 = ? WHERE evidence_id = ?", (digest("伪造"), first["evidence_id"]))
        with self.assertRaises(sqlite3.Error):
            conn.execute("DELETE FROM evidence_items WHERE evidence_id = ?", (first["evidence_id"],))

        self.assertEqual(self.system.get_evidence(first["evidence_id"])["file_sha256"], digest("截图v1"))
        self.assertEqual(second["supersedes_id"], first["evidence_id"])

    def test_scattered_channels_all_recorded(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        for channel, body in (
            ("聊天截图", "聊天记录"),
            ("知情同意", "电子知情同意书"),
            ("收费记录", "刷卡小票与发票"),
        ):
            self.system.add_evidence(cid, channel, f"{channel}材料", digest(body), ROLE_COMPLIANCE)
        with self.assertRaises(ValidationError):
            self.system.add_evidence(cid, "不存在的渠道", "x", digest("x"), ROLE_COMPLIANCE)
        channels = {e["channel"] for e in self.system.list_evidence(cid)}
        self.assertEqual(channels, {"聊天截图", "知情同意", "收费记录"})

    def test_statement_versions_never_overwritten(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        v1 = self.system.add_statement(cid, "初次陈述：宣传有上门护理", ROLE_PARTY, actor_party=case["complainant_id"])
        v2 = self.system.add_statement(cid, "补充：合同里没有该条款，是销售口头承诺", ROLE_PARTY, actor_party=case["complainant_id"], change_note="回忆起细节")
        self.assertEqual(v1["version_no"], 1)
        self.assertEqual(v2["version_no"], 2)
        versions = self.system.list_statements(cid)
        self.assertEqual(len(versions), 2)
        self.assertIn("初次陈述", versions[0]["content"])

    # ---------------------------------------------------------- 关联

    def test_duplicate_complaints_linked_but_independent(self) -> None:
        a = self._make_case("诉求A：退一赔三")
        b = self.system.create_case(
            service_name="同一老年康养年卡",
            claim="诉求B：仅要求解除合同并退款",
            complainant={"name": "李建国", "phone": "13900001111"},
            actor_role=ROLE_COMPLIANCE,
        )
        link = self.system.link_cases(a["case_id"], b["case_id"], "同一服务同一宣传话术，疑似重复投诉", ROLE_REGULATOR)
        self.assertFalse(link["already_linked"])
        # 双向关联
        self.assertEqual([l["linked_case_id"] for l in self.system.list_links(a["case_id"])], [b["case_id"]])
        self.assertEqual([l["linked_case_id"] for l in self.system.list_links(b["case_id"])], [a["case_id"]])

        # 各自独立诉求保留；推进 A 不影响 B
        self.system.transition(a["case_id"], "提交核验", ROLE_COMPLIANCE)
        self.assertEqual(self.system.view_case(a["case_id"], ROLE_REGULATOR)["claim"], "诉求A：退一赔三")
        self.assertEqual(self.system.view_case(b["case_id"], ROLE_REGULATOR)["state"], "登记")
        self.assertEqual(self.system.view_case(b["case_id"], ROLE_REGULATOR)["claim"], "诉求B：仅要求解除合同并退款")

        # 当事人无权建立关联
        with self.assertRaises(PermissionDeniedError):
            self.system.link_cases(a["case_id"], b["case_id"], "x", ROLE_PARTY, actor_party=a["complainant_id"])

    def test_cannot_link_self(self) -> None:
        case = self._make_case()
        with self.assertRaises(ValidationError):
            self.system.link_cases(case["case_id"], case["case_id"], "自关联", ROLE_REGULATOR)

    # ---------------------------------------------------------- 保全与哈希链

    def test_preservation_chain_and_tamper_detection(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        self.system.add_evidence(cid, "收费记录", "发票", digest("发票"), ROLE_COMPLIANCE)
        p1 = self.system.preserve(cid, ROLE_COMPLIANCE)
        self.assertEqual(p1["seq"], 1)
        self.assertEqual(p1["prev_hash"], GENESIS_HASH)

        # 后补材料进入第二快照，第一快照内容不变
        self.system.add_evidence(cid, "知情同意", "同意书", digest("同意书"), ROLE_COMPLIANCE)
        p2 = self.system.preserve(cid, ROLE_REGULATOR)
        self.assertEqual(p2["seq"], 2)
        self.assertEqual(p2["prev_hash"], p1["record_hash"])

        report = self.system.verify_chains(cid)
        self.assertTrue(report["ok"])

        # 伪造一条哈希不一致的保全记录，校验必须断链
        conn = self.system.db.conn()
        conn.execute(
            "INSERT INTO preservations (preservation_id, case_id, seq, state_locked, snapshot_json,"
            " prev_hash, record_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("forged", cid, 99, "处置中", "{}", p2["record_hash"], "f" * 64, ROLE_REGULATOR, "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        report = self.system.verify_chains(cid)
        self.assertFalse(report["ok"])
        self.assertTrue(any(b["chain"] == "preservation" for b in report["breaks"]))

    # ---------------------------------------------------------- 审计与裁剪

    def test_every_view_export_and_denial_is_audited(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        self.system.view_case(cid, ROLE_COMPLIANCE, client_ip="10.0.0.1")
        self.system.export_case(cid, ROLE_REGULATOR, reason="行政检查调取")

        # 无关当事人查看被拒绝，且拒绝也留痕
        outsider = self.system.create_case(
            service_name="其他服务", claim="其他诉求", complainant={"name": "外人甲"}, actor_role=ROLE_COMPLIANCE
        )
        with self.assertRaises(PermissionDeniedError):
            self.system.view_case(cid, ROLE_PARTY, party_id=outsider["complainant_id"])

        entries = self.system.list_audit(cid)
        actions = [(e["action"], e["result"]) for e in entries]
        self.assertIn(("查看案件", "allowed"), actions)
        self.assertIn(("导出案件", "allowed"), actions)
        self.assertIn(("查看案件", "denied"), actions)
        # 审计链连续
        self.assertTrue(self.system.verify_chains(cid)["ok"])
        # 导出登记固化了载荷指纹
        exports = self.system.list_exports(cid)
        self.assertEqual(len(exports), 1)
        self.assertEqual(len(exports[0]["payload_sha256"]), 64)

    def test_sensitive_fields_are_redacted_by_party_and_role(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        pid = case["complainant_id"]
        rid = case["respondent_id"]

        complainant_view = self.system.view_case(cid, ROLE_PARTY, party_id=pid)
        self.assertEqual(complainant_view["complainant_phone"], "13812345678")  # 本人完整
        self.assertIn("*", complainant_view["respondent_name"])               # 对方脱敏
        self.assertNotEqual(complainant_view["respondent_id_no"], "91110101MA01ABCD2X")
        self.assertIsNone(complainant_view["internal_note"])                  # 当事人看不到内部备注

        respondent_view = self.system.view_case(cid, ROLE_PARTY, party_id=rid)
        self.assertIn("*", respondent_view["complainant_name"])
        self.assertNotEqual(respondent_view["complainant_phone"], "13812345678")

        practitioner_view = self.system.view_case(cid, ROLE_PRACTITIONER)
        self.assertNotEqual(practitioner_view["complainant_id_no"], "110101196001011234")
        self.assertIsNone(practitioner_view["internal_note"])

        compliance_view = self.system.view_case(cid, ROLE_COMPLIANCE)
        self.assertEqual(compliance_view["complainant_id_no"], "110101196001011234")
        self.assertEqual(compliance_view["respondent_id_no"], "91110101MA01ABCD2X")
        self.assertEqual(compliance_view["internal_note"], "宣传材料疑似与合同条款冲突，重点比对")

        # 投影结果不得夹带未裁剪的嵌套当事人对象
        self.assertNotIn("complainant", complainant_view)
        self.assertNotIn("respondent", complainant_view)

        # 被投诉人的导出包同样裁剪投诉人敏感信息
        export = self.system.export_case(cid, ROLE_PARTY, party_id=rid)
        self.assertIn("*", export["case"]["complainant_name"])
        self.assertNotEqual(export["case"]["complainant_phone"], "13812345678")
        self.assertNotIn("complainant", export["case"])

    def test_timeline_shows_full_history_and_sources(self) -> None:
        case = self._make_case()
        cid = case["case_id"]
        ev = self.system.add_evidence(cid, "聊天截图", "截图", digest("截图"), ROLE_COMPLIANCE)
        self.system.add_evidence(cid, "聊天截图", "截图补充", digest("截图2"), ROLE_COMPLIANCE, supersedes_id=ev["evidence_id"])
        self.system.add_statement(cid, "陈述v1", ROLE_COMPLIANCE)
        self.system.transition(cid, "提交核验", ROLE_COMPLIANCE)
        self.system.preserve(cid, ROLE_COMPLIANCE)

        tl = self.system.timeline(cid, ROLE_REGULATOR)
        event_types = [e["event_type"] for e in tl["events"]]
        self.assertEqual(event_types[0], "案件登记")
        self.assertIn("证据提交", event_types)
        self.assertIn("提交核验", event_types)
        self.assertIn("证据保全", event_types)
        sources = tl["evidence_sources"]
        self.assertEqual(sources[1]["supersedes"]["evidence_id"], ev["evidence_id"])  # 证据来源与版本关系
        self.assertEqual(sources[0]["channel"], "聊天截图")
        self.assertEqual(len(tl["statement_versions"]), 1)
        self.assertEqual(len(tl["preservations"]), 1)

    def test_not_found_is_audited(self) -> None:
        with self.assertRaises(NotFoundError):
            self.system.view_case("missing-id", ROLE_REGULATOR)
        entries = self.system.list_audit(limit=10)
        self.assertTrue(any(e["result"] == "denied" and e["object_id"] == "missing-id" for e in entries))

    def test_validation_on_create(self) -> None:
        with self.assertRaises(ValidationError):
            self.system.create_case(service_name="", claim="x", complainant={"name": "王"}, actor_role=ROLE_COMPLIANCE)
        with self.assertRaises(ValidationError):
            self.system.create_case(service_name="服务", claim="", complainant={"name": "王"}, actor_role=ROLE_COMPLIANCE)
        with self.assertRaises(ValidationError):
            self.system.create_case(service_name="服务", claim="诉求", complainant={}, actor_role=ROLE_COMPLIANCE)


if __name__ == "__main__":
    unittest.main()
