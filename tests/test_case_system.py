"""案件系统领域服务测试：覆盖四大不变量与状态机。"""
from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_system import CaseService, ConflictError, NotFoundError, PermissionError, ValidationError
from case_system.service import Actor


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


COMPLIANCE = Actor("u_compliance", "机构合规员")
REGULATOR = Actor("u_regulator", "监管人员")
PRACTITIONER = Actor("u_practitioner", "执业人员")
REVIEWER = Actor("u_reviewer", "复核专家")


class CaseServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = CaseService(":memory:")

    def tearDown(self) -> None:
        self.svc.close()

    # -- 基础登记 --------------------------------------------------------

    def test_create_case_with_services(self) -> None:
        case = self.svc.create_case(
            COMPLIANCE, "宣传与实际服务不一致",
            advertised_service="全年无限次免费护理",
            complainant={"name": "王芳", "phone": "13800001111", "id_card": "11010119900101001X"},
        )
        cid = case["case_id"]
        self.assertEqual(case["state"], "登记")
        svc = self.svc.add_service(
            COMPLIANCE, cid, name="美容护理套餐",
            advertised="全年无限次免费护理", actual="仅提供两次并加收耗材费",
        )
        self.assertTrue(svc["service_id"])

    def test_party_role_permission(self) -> None:
        case = self.svc.create_case(COMPLIANCE, "测试案")
        with self.assertRaises(PermissionError):
            self.svc.add_party(REVIEWER, case["case_id"], "respondent", {"name": "某机构"})

    # -- 陈述版本 --------------------------------------------------------

    def test_statement_versions_are_append_only(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "陈述版本案")["case_id"]
        v1 = self.svc.submit_statement(COMPLIANCE, cid, "对方口头承诺退款", "窗口陈述")
        v2 = self.svc.submit_statement(
            COMPLIANCE, cid, "对方书面承诺全额退款并赔偿", "12315 平台",
            supersedes_id=v1["statement_id"],
        )
        self.assertEqual((v1["version_no"], v2["version_no"]), (1, 2))
        self.assertEqual(v2["supersedes_id"], v1["statement_id"])
        rows = self.svc.db.list_statements(cid)
        self.assertEqual(len(rows), 2, "旧陈述必须保留，不能被覆盖")

    # -- 证据不变量：后补材料不覆盖原证据 ----------------------------------

    def test_supplement_keeps_original_version(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "证据案")["case_id"]
        h1, h2 = sha("聊天截图v1"), sha("完整聊天记录v2")
        ev = self.svc.receive_evidence(
            COMPLIANCE, cid, kind="chat_screenshot", title="微信聊天截图",
            source_channel="微信", sha256=h1, byte_size=1024,
        )
        eid = ev["evidence_id"]
        result = self.svc.supplement_evidence(
            COMPLIANCE, eid, sha256=h2, byte_size=2048, note="补充完整上下文截图",
        )
        self.assertEqual(result["version_no"], 2)
        versions = self.svc.db.list_evidence_versions(eid)
        self.assertEqual([v["sha256"] for v in versions], [h1, h2],
                         "原版本哈希必须永久保留")
        current = self.svc.db.query_one("SELECT * FROM evidence WHERE evidence_id=?", (eid,))
        self.assertEqual(current["current_sha256"], h2)

        with self.assertRaises(ConflictError):
            self.svc.supplement_evidence(COMPLIANCE, eid, sha256=h2, byte_size=2048,
                                         note="重复提交")

    def test_reject_bad_digest(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "哈希案")["case_id"]
        with self.assertRaises(ValidationError):
            self.svc.receive_evidence(
                COMPLIANCE, cid, kind="receipt", title="收费记录",
                source_channel="支付宝", sha256="abc", byte_size=10,
            )

    def test_evidence_from_mixed_channels_preserved(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "多渠道证据案")["case_id"]
        for kind, title, channel in (
            ("chat_screenshot", "聊天截图", "微信"),
            ("consent", "知情同意书", "线下签署"),
            ("receipt", "收费记录", "支付宝"),
        ):
            self.svc.receive_evidence(
                COMPLIANCE, cid, kind=kind, title=title, source_channel=channel,
                sha256=sha(title), byte_size=100,
            )
        evidence = self.svc.list_evidence(REGULATOR, cid)
        self.assertEqual({e["source_channel"] for e in evidence},
                         {"微信", "线下签署", "支付宝"})

    # -- 保全哈希链 -------------------------------------------------------

    def test_preservation_hash_chain(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "保全案")["case_id"]
        ev = self.svc.receive_evidence(
            COMPLIANCE, cid, "receipt", "收费记录", "支付宝", sha("r"), 512,
        )
        self.svc.record_preservation(REGULATOR, cid, "现场封存", "U盘封存于档案室")
        self.svc.supplement_evidence(COMPLIANCE, ev["evidence_id"], sha("r2"), 700, "补单")
        chain = self.svc.db.verify_preservation_chain(cid)
        self.assertTrue(chain["intact"])
        self.assertGreaterEqual(chain["length"], 3)

    def test_tampering_breaks_chain_and_blocks_export(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "篡改案")["case_id"]
        self.svc.receive_evidence(COMPLIANCE, cid, "receipt", "收费", "微信", sha("x"), 10)
        self.svc.db.conn.execute(
            "UPDATE preservation_records SET detail='被篡改' WHERE rowid=1"
        )
        self.svc.db.commit()
        chain = self.svc.db.verify_preservation_chain(cid)
        self.assertFalse(chain["intact"])
        with self.assertRaises(ConflictError):
            self.svc.export_case(REGULATOR, cid)

    # -- 状态机 -----------------------------------------------------------

    def test_state_transitions_constrained(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "状态案")["case_id"]
        # 登记 -> 待核验 -> 处置中
        self.svc.transition(COMPLIANCE, cid, "提交核验")
        self.svc.transition(COMPLIANCE, cid, "受理")
        # 调解可重复且不改变状态
        self.svc.transition(PRACTITIONER, cid, "调解", detail={"round": 1})
        self.svc.transition(PRACTITIONER, cid, "调解", detail={"round": 2})
        self.assertEqual(self.svc.db.query_one(
            "SELECT state FROM cases WHERE case_id=?", (cid,))["state"], "处置中")
        # 处置中不能直接归档（必须先决定/撤回）
        with self.assertRaises(ConflictError):
            self.svc.transition(COMPLIANCE, cid, "归档")
        # 转执法 -> 已决定 -> 归档
        self.svc.transition(COMPLIANCE, cid, "转执法", detail={"authority": "市场监管执法局"})
        self.svc.transition(COMPLIANCE, cid, "归档")
        self.assertEqual(self.svc.db.query_one(
            "SELECT state FROM cases WHERE case_id=?", (cid,))["state"], "已归档")

    def test_withdraw_and_reopen(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "撤回案")["case_id"]
        self.svc.transition(COMPLIANCE, cid, "提交核验")
        self.svc.transition(COMPLIANCE, cid, "撤回", detail={"reason": "双方自行和解"})
        case_row = self.svc.db.query_one("SELECT * FROM cases WHERE case_id=?", (cid,))
        self.assertEqual(case_row["state"], "已撤回")
        # 已撤回不能再撤回
        with self.assertRaises(ConflictError):
            self.svc.transition(COMPLIANCE, cid, "撤回")
        # 复开 -> 待核验，计数加一，证据与诉求仍在
        self.svc.transition(REGULATOR, cid, "复开", detail={"reason": "和解未履行"})
        case_row = self.svc.db.query_one("SELECT * FROM cases WHERE case_id=?", (cid,))
        self.assertEqual(case_row["state"], "待核验")
        self.assertEqual(case_row["reopened_count"], 1)
        events = self.svc.db.list_state_events(cid)
        self.assertEqual([e["action"] for e in events],
                         ["登记", "提交核验", "撤回", "复开"])

    def test_unknown_action_rejected(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "x")["case_id"]
        with self.assertRaises(ValidationError):
            self.svc.transition(COMPLIANCE, cid, "销案")

    # -- 重复投诉：关联但独立 ----------------------------------------------

    def test_duplicate_complaints_linked_but_independent(self) -> None:
        a = self.svc.create_case(COMPLIANCE, "投诉A：退款诉求")["case_id"]
        b = self.svc.create_case(COMPLIANCE, "投诉B：赔偿诉求")["case_id"]
        self.svc.receive_evidence(COMPLIANCE, a, "chat", "A的截图", "微信", sha("a"), 10)
        self.svc.receive_evidence(COMPLIANCE, b, "receipt", "B的账单", "支付宝", sha("b"), 20)
        link = self.svc.link_cases(REGULATOR, a, b, reason="同一机构同一宣传活动")
        self.assertTrue(link["link_id"])
        with self.assertRaises(ConflictError):
            self.svc.link_cases(REGULATOR, b, a, reason="重复建立")
        # 两边状态、证据、诉求互不影响
        self.svc.transition(COMPLIANCE, a, "提交核验")
        self.assertEqual(self.svc.db.query_one(
            "SELECT state FROM cases WHERE case_id=?", (b,))["state"], "登记")
        self.assertEqual(len(self.svc.db.list_evidence(a)), 1)
        self.assertEqual(len(self.svc.db.list_evidence(b)), 1)
        tl = self.svc.get_timeline(REGULATOR, a)
        self.assertTrue(any(e["type"] == "case_linked"
                            and e["data"]["related_case_id"] == b for e in tl["events"]))

    def test_cannot_link_self(self) -> None:
        a = self.svc.create_case(COMPLIANCE, "x")["case_id"]
        with self.assertRaises(ValidationError):
            self.svc.link_cases(REGULATOR, a, a, "自关联")

    # -- 脱敏 -------------------------------------------------------------

    def test_redaction_by_role(self) -> None:
        cid = self.svc.create_case(
            COMPLIANCE, "脱敏案",
            complainant={"name": "王芳", "phone": "13800001111",
                         "id_card": "11010119900101001X", "address": "某市某区"},
        )["case_id"]
        comp_view = self.svc.get_case(COMPLIANCE, cid)["parties"][0]
        self.assertIsNone(comp_view["phone"])
        self.assertIsNone(comp_view["id_card"])
        self.assertIn("phone", comp_view["_redacted"])
        self.assertEqual(comp_view["name"], "王芳", "姓名级信息对办案角色完整可见")

        reg_view = self.svc.get_case(REGULATOR, cid)["parties"][0]
        self.assertEqual(reg_view["phone"], "13800001111")
        self.assertEqual(reg_view["id_card"], "11010119900101001X")
        self.assertNotIn("_redacted", reg_view)

    def test_party_ownership_redaction_and_visibility(self) -> None:
        cid = self.svc.create_case(
            COMPLIANCE, "当事人案",
            complainant={"name": "王芳", "phone": "13800001111", "id_card": "IDW"},
        )["case_id"]
        respondent = self.svc.add_party(
            COMPLIANCE, cid, "respondent",
            {"name": "李某某", "phone": "101010", "id_card": "IDR"},
        )
        complainant_pid = self.svc.get_case(COMPLIANCE, cid)["case"]["complainant_party_id"]
        owner = Actor("p_wang", "当事人", party_id=complainant_pid)
        other = Actor("p_li", "当事人", party_id=respondent["party_id"])

        owner_parties = {p["side"]: p for p in self.svc.get_case(owner, cid)["parties"]}
        self.assertEqual(owner_parties["complainant"]["phone"], "13800001111",
                         "本人信息完整可见")
        self.assertIsNone(owner_parties["respondent"]["phone"], "对方联系方式不可见")
        self.assertIsNone(owner_parties["respondent"]["id_card"], "对方证件号不可见")
        self.assertEqual(owner_parties["respondent"]["name"], "李某某",
                         "对方姓名在纠纷中应可辨识，不打码")
        self.assertIn("phone", owner_parties["respondent"]["_redacted"])

        self.assertIsNotNone(self.svc.get_case(other, cid))
        cid2 = self.svc.create_case(COMPLIANCE, "无关案件")["case_id"]
        outsider = Actor("p_out", "当事人", party_id="pty_not_exists")
        with self.assertRaises(PermissionError):
            self.svc.get_case(outsider, cid2)

    def test_party_actor_requires_party_id(self) -> None:
        with self.assertRaises(ValidationError):
            Actor("p", "当事人")

    # -- 审计 -------------------------------------------------------------

    def test_every_view_and_export_audited(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "审计案")["case_id"]
        self.svc.get_case(REGULATOR, cid, purpose="日常核查")
        self.svc.get_timeline(REGULATOR, cid)
        self.svc.list_evidence(PRACTITIONER, cid)
        self.svc.export_case(REGULATOR, cid, kind="evidence_pack", purpose="专项检查")
        audit = self.svc.list_audit(REVIEWER, case_id=cid, limit=50)
        actions = [a["action"] for a in audit]
        self.assertIn("case.view", actions)
        self.assertIn("timeline.view", actions)
        self.assertIn("evidence.list", actions)
        self.assertIn("case.export", actions)
        purpose_entry = next(a for a in audit if a["action"] == "case.view")
        self.assertEqual(purpose_entry["purpose"], "日常核查")
        self.assertTrue(all(a["result"] == "success" for a in audit))

    def test_denied_access_audited(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "拒绝案")["case_id"]
        with self.assertRaises(PermissionError):
            self.svc.audited_action(
                PRACTITIONER,
                lambda: self.svc.create_case(PRACTITIONER, "越权建档"),
                "case.create", "case",
            )
        audit = self.svc.db.list_audit(limit=10)
        self.assertEqual(audit[0]["result"], "denied")
        self.assertIn("无权", audit[0]["detail"]["error"])

    def test_audit_view_restricted(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "x")["case_id"]
        with self.assertRaises(PermissionError):
            self.svc.list_audit(COMPLIANCE, cid)

    # -- 时间线与导出 ------------------------------------------------------

    def test_timeline_order_and_sources(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "时间线案",
                                  advertised_service="免费体验")["case_id"]
        self.svc.add_service(COMPLIANCE, cid, "体验服务", "免费体验", "实收 3000 元")
        self.svc.submit_statement(COMPLIANCE, cid, "承诺免费", "现场")
        ev = self.svc.receive_evidence(
            COMPLIANCE, cid, "consent", "知情同意书", "线下签署", sha("c"), 300,
        )
        self.svc.supplement_evidence(COMPLIANCE, ev["evidence_id"], sha("c2"), 320, "补签字页")
        self.svc.transition(COMPLIANCE, cid, "提交核验")
        tl = self.svc.get_timeline(REGULATOR, cid)
        types = [e["type"] for e in tl["events"]]
        self.assertEqual(types, [e["type"] for e in sorted(tl["events"], key=lambda x: x["at"])])
        self.assertIn("service_recorded", types)
        ev_item = next(e for e in tl["events"] if e["type"] == "evidence_received")
        self.assertEqual(ev_item["data"]["source_channel"], "线下签署")
        self.assertEqual(ev_item["data"]["version_no"], 2)
        self.assertTrue(tl["preservation_chain"]["intact"])

    def test_export_payload_and_missing_case(self) -> None:
        cid = self.svc.create_case(COMPLIANCE, "导办案")["case_id"]
        self.svc.receive_evidence(COMPLIANCE, cid, "chat", "截图", "微信", sha("q"), 10)
        pack = self.svc.export_case(REVIEWER, cid, kind="evidence_pack")
        self.assertIn("evidence", pack)
        self.assertNotIn("statements", pack)
        full = self.svc.export_case(REGULATOR, cid, kind="full")
        self.assertEqual(full["preservation_chain"]["intact"], True)
        with self.assertRaises(NotFoundError):
            self.svc.get_case(REGULATOR, "case_not_exist")
        with self.assertRaises(ValidationError):
            self.svc.export_case(REGULATOR, cid, kind="pdf")


if __name__ == "__main__":
    unittest.main()
