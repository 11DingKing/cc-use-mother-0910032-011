"""HTTP 接口端到端测试（标准库 urllib，零外部依赖）。"""
from __future__ import annotations

import hashlib
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from case_system.api import build_server


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server(":memory:", "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method: str, path: str, headers: dict | None = None, body: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        hdr = {"Content-Type": "application/json; charset=utf-8"}
        hdr.update(headers or {})
        conn.request(method, path, body=payload, headers=hdr)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_health_and_end_to_end_flow(self) -> None:
        status, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

        # 登记
        status, case = self.request("POST", "/cases", {"X-Role": "compliance"}, {
            "service_name": "健身私教课（宣传承诺随时退，实际拒绝）",
            "claim": "退还剩余课程费 6000 元",
            "complainant": {"name": "陈晨", "phone": "13711112222", "id_no": "310101199003034567"},
            "respondent": {"name": "闪电健身有限公司", "id_no": "91310101MA01XX99Y1"},
            "internal_note": "销售朋友圈截图与合同冲突",
        })
        self.assertEqual(status, 201)
        cid = case["case_id"]
        pid = case["complainant_id"]

        # 三类分散渠道材料
        for channel, text in (("聊天截图", "微信"), ("知情同意", "同意书"), ("收费记录", "小票")):
            status, _ = self.request("POST", f"/cases/{cid}/evidence", {"X-Role": "party", "X-Party-Id": pid}, {
                "channel": channel, "title": f"{channel}材料", "file_sha256": digest(text), "file_name": f"{text}.dat",
            })
            self.assertEqual(status, 201)

        # 陈述版本
        status, v1 = self.request("POST", f"/cases/{cid}/statements", {"X-Role": "party", "X-Party-Id": pid}, {
            "content": "销售承诺开课后不满意随时退款",
        })
        self.assertEqual(v1["version_no"], 1)

        # 状态流转
        status, _ = self.request("POST", f"/cases/{cid}/transitions", {"X-Role": "party", "X-Party-Id": pid}, {"action": "提交核验"})
        self.assertEqual(status, 200)
        status, body = self.request("POST", f"/cases/{cid}/transitions", {"X-Role": "compliance"}, {"action": "核验通过"})
        self.assertEqual(status, 200)

        # 非法流转 -> 409
        status, body = self.request("POST", f"/cases/{cid}/transitions", {"X-Role": "compliance"}, {"action": "归档"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")

        # 转执法被角色拒绝（合规员无权）-> 403 且留痕
        status, body = self.request("POST", f"/cases/{cid}/transitions", {"X-Role": "compliance"}, {"action": "转执法"})
        self.assertEqual(status, 403)

        # 启动调解 + 保全
        status, _ = self.request("POST", f"/cases/{cid}/transitions", {"X-Role": "compliance"}, {"action": "启动调解"})
        self.assertEqual(status, 200)
        status, pres = self.request("POST", f"/cases/{cid}/preservations", {"X-Role": "compliance"})
        self.assertEqual(status, 201)
        self.assertEqual(pres["seq"], 1)

        # 时间线
        status, tl = self.request("GET", f"/cases/{cid}/timeline", {"X-Role": "regulator"})
        self.assertEqual(status, 200)
        self.assertEqual(len(tl["evidence_sources"]), 3)
        self.assertGreaterEqual(len(tl["events"]), 5)

        # 导出（监管人员），并校验导出登记
        export_path = f"/cases/{quote(cid)}/export?" + urlencode({"reason": "行政检查"})
        status, bundle = self.request("GET", export_path, {"X-Role": "regulator"})
        self.assertEqual(status, 200)
        self.assertEqual(len(bundle["export"]["payload_sha256"]), 64)
        status, exports = self.request("GET", f"/cases/{cid}/exports", {"X-Role": "regulator"})
        self.assertEqual(len(exports["exports"]), 1)

        # 当事人视图被裁剪
        status, own = self.request("GET", f"/cases/{cid}", {"X-Role": "party", "X-Party-Id": pid})
        self.assertEqual(status, 200)
        self.assertEqual(own["complainant_phone"], "13711112222")
        self.assertIn("*", own["respondent_name"])
        self.assertIsNone(own["internal_note"])

        # 无身份头 -> 403
        status, body = self.request("GET", f"/cases/{cid}")
        self.assertEqual(status, 403)

        # 审计接口仅办案角色
        status, body = self.request("GET", "/audit", {"X-Role": "party", "X-Party-Id": pid})
        self.assertEqual(status, 403)
        status, audit = self.request("GET", f"/cases/{cid}/audit", {"X-Role": "reviewer"})
        self.assertEqual(status, 200)
        denied = [e for e in audit["entries"] if e["result"] == "denied"]
        self.assertGreaterEqual(len(denied), 1)

        # 链校验
        status, verify = self.request("POST", f"/cases/{cid}/verify", {"X-Role": "reviewer"})
        self.assertEqual(status, 200)
        self.assertTrue(verify["ok"])

    def test_duplicate_complaints_independent(self) -> None:
        def new_case(claim: str, who: str) -> str:
            status, case = self.request("POST", "/cases", {"X-Role": "compliance"}, {
                "service_name": "同一医美套餐", "claim": claim, "complainant": {"name": who},
            })
            self.assertEqual(status, 201)
            return case["case_id"]

        a = new_case("退一赔三", "甲")
        b = new_case("解除合同", "乙")
        status, link = self.request("POST", f"/cases/{a}/links", {"X-Role": "regulator"}, {
            "linked_case_id": b, "reason": "同一门店同一宣传",
        })
        self.assertEqual(status, 201)
        status, data = self.request("GET", f"/cases/{a}/links", {"X-Role": "compliance"})
        self.assertEqual(data["links"][0]["claim"], "解除合同")
        status, data = self.request("GET", f"/cases/{b}/links", {"X-Role": "compliance"})
        self.assertEqual(data["links"][0]["claim"], "退一赔三")

    def test_append_only_rejected_over_http_stack(self) -> None:
        # 数据库触发器在服务进程内同样生效：伪造写入后校验链断裂
        status, case = self.request("POST", "/cases", {"X-Role": "compliance"}, {
            "service_name": "服务", "claim": "诉求", "complainant": {"name": "丙"},
        })
        cid = case["case_id"]
        status, pres = self.request("POST", f"/cases/{cid}/preservations", {"X-Role": "regulator"})
        self.assertEqual(status, 201)

        conn = self.server.case_system.db.conn()
        conn.execute(
            "INSERT INTO preservations (preservation_id, case_id, seq, state_locked, snapshot_json,"
            " prev_hash, record_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("x", cid, 9, "登记", "{}", pres["record_hash"], "a" * 64, "监管人员", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        status, verify = self.request("POST", f"/cases/{cid}/verify", {"X-Role": "regulator"})
        self.assertFalse(verify["ok"])


if __name__ == "__main__":
    unittest.main()
