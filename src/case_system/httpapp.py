"""HTTP 接口（标准库实现，无第三方依赖）。

身份通过请求头绑定（演示/内网部署用，生产环境应替换为网关签发的身份令牌）：
- X-Actor-Id：办案人员或当事人标识（必填）
- X-Actor-Role：机构合规员 / 执业人员 / 监管人员 / 复核专家 / 当事人（必填）
- X-Party-Id：角色为当事人时必填，用于按当事人归属裁剪

所有响应为 JSON。查看与导出在服务层内强制写审计；被拒绝的请求同样留痕。
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import CaseError, PermissionError
from .service import Actor, CaseService

CLIENT_ERRORS = (CaseError, KeyError, ValueError, TypeError)

# HTTP 头只能传 latin-1，角色用英文代码传递
ROLE_CODES: dict[str, str] = {
    "compliance": "机构合规员",
    "practitioner": "执业人员",
    "regulator": "监管人员",
    "reviewer": "复核专家",
    "party": "当事人",
}


class CaseHTTPHandler(BaseHTTPRequestHandler):
    service: CaseService

    # -- 基础收发 --------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _actor(self) -> Actor:
        actor_id = self.headers.get("X-Actor-Id")
        role_code = self.headers.get("X-Actor-Role")
        party_id = self.headers.get("X-Party-Id")
        if not actor_id or not role_code:
            raise PermissionError("缺少 X-Actor-Id / X-Actor-Role 请求头")
        role = ROLE_CODES.get(role_code)
        if role is None:
            raise PermissionError(
                f"未知角色代码：{role_code}，可选 {sorted(ROLE_CODES)}"
            )
        return Actor(actor_id=actor_id, role=role, party_id=party_id)

    def _purpose(self, query: dict[str, list[str]]) -> str | None:
        return (query.get("purpose") or [None])[0]

    def _handle(self, audit_action: str, resource_type: str,
                case_id: str | None, work: Callable[[], Any]) -> None:
        try:
            actor = self._actor()
        except PermissionError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        try:
            result = self.service.audited_action(
                actor, work, audit_action, resource_type, case_id=case_id,
            )
            self._send_json(200, result if result is not None else {"ok": True})
        except CaseError as exc:
            self._send_json(exc.status, {"error": str(exc),
                                         "error_type": type(exc).__name__})
        except CLIENT_ERRORS as exc:  # noqa: PERF203
            self._send_json(400, {"error": f"请求参数错误：{exc}",
                                  "error_type": type(exc).__name__})

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    # -- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        m = re.fullmatch(r"/cases/([^/]+)", path)
        if m:
            case_id = m.group(1)
            return self._handle("case.view", "case", case_id, lambda: self.service.get_case(
                self._actor(), case_id, purpose=self._purpose(query)))
        m = re.fullmatch(r"/cases/([^/]+)/evidence", path)
        if m:
            case_id = m.group(1)
            return self._handle("evidence.list", "evidence", case_id,
                                lambda: self.service.list_evidence(
                                    self._actor(), case_id, purpose=self._purpose(query)))
        m = re.fullmatch(r"/cases/([^/]+)/timeline", path)
        if m:
            case_id = m.group(1)
            return self._handle("timeline.view", "timeline", case_id,
                                lambda: self.service.get_timeline(
                                    self._actor(), case_id, purpose=self._purpose(query)))
        m = re.fullmatch(r"/cases/([^/]+)/export", path)
        if m:
            case_id = m.group(1)
            kind = (query.get("kind") or ["full"])[0]
            return self._handle("case.export", "export", case_id,
                                lambda: self.service.export_case(
                                    self._actor(), case_id, kind=kind,
                                    purpose=self._purpose(query)))
        if path == "/audit":
            case_id = (query.get("case_id") or [None])[0]
            limit = int((query.get("limit") or ["100"])[0])
            return self._handle("audit.view", "audit", case_id,
                                lambda: self.service.list_audit(
                                    self._actor(), case_id=case_id, limit=limit,
                                    purpose=self._purpose(query)))
        if path == "/health":
            return self._send_json(200, {"ok": True})
        self._send_json(404, {"error": f"无此路由：{self.path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        if path == "/cases":
            return self._handle("case.create", "case", None, lambda: self.service.create_case(
                self._actor(), title=body["title"],
                advertised_service=body.get("advertised_service"),
                complainant=body.get("complainant")))

        m = re.fullmatch(r"/cases/([^/]+)/parties", path)
        if m:
            case_id = m.group(1)
            return self._handle("party.add", "party", case_id, lambda: self.service.add_party(
                self._actor(), case_id, side=body["side"], party=body))

        m = re.fullmatch(r"/cases/([^/]+)/services", path)
        if m:
            case_id = m.group(1)
            return self._handle("service.add", "service", case_id,
                                lambda: self.service.add_service(
                                    self._actor(), case_id, name=body["name"],
                                    advertised=body.get("advertised"),
                                    actual=body.get("actual")))

        m = re.fullmatch(r"/cases/([^/]+)/statements", path)
        if m:
            case_id = m.group(1)
            return self._handle("statement.submit", "statement", case_id,
                                lambda: self.service.submit_statement(
                                    self._actor(), case_id, content=body["content"],
                                    source_channel=body["source_channel"],
                                    supersedes_id=body.get("supersedes_id")))

        m = re.fullmatch(r"/cases/([^/]+)/evidence", path)
        if m:
            case_id = m.group(1)
            return self._handle("evidence.receive", "evidence", case_id,
                                lambda: self.service.receive_evidence(
                                    self._actor(), case_id, kind=body["kind"],
                                    title=body["title"],
                                    source_channel=body["source_channel"],
                                    sha256=body["sha256"], byte_size=body["byte_size"],
                                    storage_ref=body.get("storage_ref"),
                                    metadata=body.get("metadata"),
                                    linked_statement_id=body.get("linked_statement_id")))

        m = re.fullmatch(r"/evidence/([^/]+)/supplement", path)
        if m:
            evidence_id = m.group(1)
            return self._handle("evidence.supplement", "evidence", None,
                                lambda: self.service.supplement_evidence(
                                    self._actor(), evidence_id, sha256=body["sha256"],
                                    byte_size=body["byte_size"], note=body["note"],
                                    storage_ref=body.get("storage_ref")))

        m = re.fullmatch(r"/cases/([^/]+)/preservation", path)
        if m:
            case_id = m.group(1)
            return self._handle("preservation.record", "preservation", case_id,
                                lambda: self.service.record_preservation(
                                    self._actor(), case_id, action=body["action"],
                                    detail=body["detail"],
                                    evidence_id=body.get("evidence_id")))

        m = re.fullmatch(r"/cases/([^/]+)/links", path)
        if m:
            case_id = m.group(1)
            return self._handle("case.link", "case_link", case_id,
                                lambda: self.service.link_cases(
                                    self._actor(), case_id, body["other_case_id"],
                                    reason=body["reason"]))

        m = re.fullmatch(r"/cases/([^/]+)/transitions", path)
        if m:
            case_id = m.group(1)
            return self._handle("case.transition", "case", case_id,
                                lambda: self.service.transition(
                                    self._actor(), case_id, action=body["action"],
                                    detail=body.get("detail")))

        self._send_json(404, {"error": f"无此路由：{self.path}"})


def build_handler(service: CaseService) -> type[CaseHTTPHandler]:
    return type("BoundHandler", (CaseHTTPHandler,), {"service": service})


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    service = CaseService(db_path)
    server = HTTPServer((host, port), build_handler(service))
    print(f"案件系统监听 http://{host}:{port}（数据库 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="消费者投诉证据链案件系统 HTTP 服务")
    parser.add_argument("--db", default="data/cases.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    serve(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
