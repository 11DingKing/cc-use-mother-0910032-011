"""HTTP 接口端到端测试：真实起服、真实请求。"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_system.httpapp import CaseHTTPHandler
from case_system.service import CaseService


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class HTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db_path = self.tmp.name
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.service = CaseService(self.db_path)
        handler = type("H", (CaseHTTPHandler,), {"service": self.service})
        self.server = HTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        Path(self.db_path).unlink(missing_ok=True)

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    COMPLIANCE_HEADERS = {"X-Actor-Id": "u1", "X-Actor-Role": "compliance"}
    REGULATOR_HEADERS = {"X-Actor-Id": "u2", "X-Actor-Role": "regulator"}
    PRACTITIONER_HEADERS = {"X-Actor-Id": "u3", "X-Actor-Role": "practitioner"}

    def test_full_flow_over_http(self) -> None:
        # 建档（含投诉人）
        status, case = self.request("POST", "/cases", {
            "title": "宣传免费实则收费",
            "advertised_service": "到店即享免费护理",
            "complainant": {"name": "王芳", "phone": "13800001111", "id_card": "ID-X"},
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)
        cid = case["case_id"]

        # 涉事服务：宣传 vs 实际
        status, svc = self.request("POST", f"/cases/{cid}/services", {
            "name": "护理套餐", "advertised": "免费护理", "actual": "收费 3000 元",
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)

        # 被申请人
        status, party = self.request("POST", f"/cases/{cid}/parties", {
            "side": "respondent", "name": "某美容店", "phone": "10086",
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)

        # 陈述版本
        status, st1 = self.request("POST", f"/cases/{cid}/statements", {
            "content": "店员口头说免费", "source_channel": "电话记录",
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)
        status, st2 = self.request("POST", f"/cases/{cid}/statements", {
            "content": "店员微信明确承诺免费并赠送年卡",
            "source_channel": "微信", "supersedes_id": st1["statement_id"],
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)

        # 三类分散渠道证据
        for kind, title, channel in (
            ("chat_screenshot", "聊天截图", "微信"),
            ("consent", "知情同意书", "线下签署"),
            ("receipt", "收费记录", "支付宝"),
        ):
            status, ev = self.request("POST", f"/cases/{cid}/evidence", {
                "kind": kind, "title": title, "source_channel": channel,
                "sha256": sha(title), "byte_size": 256,
                "linked_statement_id": st2["statement_id"],
            }, self.COMPLIANCE_HEADERS)
            self.assertEqual(status, 200, ev)
        chat_ev = sha("聊天截图")
        status, _ = self.request("POST", f"/cases/{cid}/evidence", {
            "kind": "chat_screenshot", "title": "聊天截图2", "source_channel": "微信",
            "sha256": chat_ev, "byte_size": 256,
        }, self.COMPLIANCE_HEADERS)

        # 后补材料不覆盖原证据
        status, evidence_list = self.request("GET", f"/cases/{cid}/evidence",
                                             headers=self.REGULATOR_HEADERS)
        target = next(e for e in evidence_list if e["title"] == "聊天截图")
        status, sup = self.request("POST", f"/evidence/{target['evidence_id']}/supplement", {
            "sha256": sha("完整聊天记录"), "byte_size": 900, "note": "补充完整上下文",
        }, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(sup["version_no"], 2)

        # 完整流程：核验 -> 受理 -> 调解 -> 调解结案
        for action in ("提交核验", "受理", "调解", "调解结案"):
            status, evt = self.request("POST", f"/cases/{cid}/transitions",
                                       {"action": action}, self.PRACTITIONER_HEADERS
                                       if action == "调解" else self.COMPLIANCE_HEADERS)
            self.assertEqual(status, 200, evt)

        # 时间线
        status, detail = self.request("GET", f"/cases/{cid}?purpose=" + urllib.parse.quote("日常核查"),
                                      headers=self.PRACTITIONER_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(detail["state"], "已决定")

        status, tl = self.request("GET", f"/cases/{cid}/timeline",
                                  headers=self.REGULATOR_HEADERS)
        self.assertEqual(status, 200)
        self.assertTrue(tl["preservation_chain"]["intact"])
        self.assertGreaterEqual(len(tl["events"]), 10)
        types = {e["type"] for e in tl["events"]}
        self.assertTrue({"service_recorded", "statement_version", "evidence_received",
                         "preservation", "state_event"} <= types)

        # 导出
        status, exported = self.request(
            "GET", f"/cases/{cid}/export?kind=full&purpose=" + urllib.parse.quote("督查"),
            headers=self.REGULATOR_HEADERS)
        self.assertEqual(status, 200)
        self.assertTrue(exported["preservation_chain"]["intact"])

        # 审计：每次查看/导出都在
        status, audit = self.request("GET", f"/audit?case_id={cid}",
                                     headers=self.REGULATOR_HEADERS)
        self.assertEqual(status, 200)
        actions = {a["action"] for a in audit}
        self.assertTrue({"case.view", "evidence.list", "timeline.view",
                         "case.export"} <= actions)
        export_audit = next(a for a in audit if a["action"] == "case.export")
        self.assertEqual(export_audit["purpose"], "督查")

    def test_permission_and_state_conflict_over_http(self) -> None:
        status, case = self.request("POST", "/cases", {"title": "权限案"},
                                    self.PRACTITIONER_HEADERS)
        self.assertEqual(status, 403)
        self.assertEqual(case["error_type"], "PermissionError")

        status, case = self.request("POST", "/cases", {"title": "状态案"},
                                    self.COMPLIANCE_HEADERS)
        cid = case["case_id"]
        # 登记状态直接"受理"违反状态约束
        status, err = self.request("POST", f"/cases/{cid}/transitions",
                                   {"action": "受理"}, self.COMPLIANCE_HEADERS)
        self.assertEqual(status, 409)
        self.assertEqual(err["error_type"], "ConflictError")

        # 缺少身份头
        status, err = self.request("GET", f"/cases/{cid}")
        self.assertEqual(status, 403)

        # 越权被拒也有审计（监管查看审计日志可见 denied）
        status, audit = self.request("GET", "/audit?limit=20",
                                     headers=self.REGULATOR_HEADERS)
        self.assertTrue(any(a["result"] == "denied" for a in audit))

    def test_redaction_over_http(self) -> None:
        status, case = self.request("POST", "/cases", {
            "title": "脱敏案",
            "complainant": {"name": "王芳", "phone": "13800001111", "id_card": "ID-X"},
        }, self.COMPLIANCE_HEADERS)
        cid = case["case_id"]

        status, comp = self.request("GET", f"/cases/{cid}",
                                    headers=self.COMPLIANCE_HEADERS)
        self.assertIsNone(comp["parties"][0]["phone"])

        status, reg = self.request("GET", f"/cases/{cid}",
                                   headers=self.REGULATOR_HEADERS)
        self.assertEqual(reg["parties"][0]["phone"], "13800001111")

        # 当事人视角：用 party_id 绑定，只能看到本人完整信息
        pid = case["complainant_party_id"]
        status, mine = self.request(
            "GET", f"/cases/{cid}",
            headers={"X-Actor-Id": "p1", "X-Actor-Role": "party", "X-Party-Id": pid})
        self.assertEqual(status, 200)
        self.assertEqual(mine["parties"][0]["phone"], "13800001111")

    def test_duplicate_complaints_linked(self) -> None:
        _, a = self.request("POST", "/cases", {"title": "投诉A：退款"},
                            self.COMPLIANCE_HEADERS)
        _, b = self.request("POST", "/cases", {"title": "投诉B：赔偿"},
                            self.COMPLIANCE_HEADERS)
        status, link = self.request("POST", f"/cases/{a['case_id']}/links",
                                    {"other_case_id": b["case_id"], "reason": "同一活动"},
                                    self.REGULATOR_HEADERS)
        self.assertEqual(status, 200)
        # 重复关联 409
        status, err = self.request("POST", f"/cases/{b['case_id']}/links",
                                   {"other_case_id": a["case_id"], "reason": "再来一次"},
                                   self.REGULATOR_HEADERS)
        self.assertEqual(status, 409)
        # A 的时间线能看到 B，且 A 的状态不受 B 影响
        _, tl = self.request("GET", f"/cases/{a['case_id']}/timeline",
                             headers=self.REGULATOR_HEADERS)
        self.assertTrue(any(e["data"].get("related_case_id") == b["case_id"]
                            for e in tl["events"]))

    def test_bad_json_and_unknown_route(self) -> None:
        req = urllib.request.Request(
            self.base + "/cases", data=b"{not-json", method="POST",
            headers={"Content-Type": "application/json", **self.COMPLIANCE_HEADERS},
        )
        try:
            urllib.request.urlopen(req)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

        status, _ = self.request("GET", "/nope", headers=self.REGULATOR_HEADERS)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
