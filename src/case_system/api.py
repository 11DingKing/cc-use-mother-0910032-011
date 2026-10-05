"""零依赖 JSON HTTP 接口（标准库 http.server）。

身份通过请求头传递：
- ``X-Role``：机构合规员 / 执业人员 / 监管人员 / 复核专家 / 当事人
- ``X-Party-Id``：角色为当事人时的当事人 ID

所有查看与导出都会在服务层落审计；越权访问返回 403 且同样留痕。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from .database import Database
from .errors import CaseSystemError, ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from .models import CASE_ROLES, resolve_role
from .service import CaseSystem

DEFAULT_PORT = 8080


def create_system(db_path: str = ":memory:") -> CaseSystem:
    return CaseSystem(Database(db_path))


class CaseHTTPHandler(BaseHTTPRequestHandler):
    system: CaseSystem = None  # type: ignore[assignment] 由 factory 注入到子类

    server_version = "CaseEvidenceSystem/1.0"

    # ------------------------------------------------------------ 工具

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _identity(self) -> tuple[str, Optional[str]]:
        raw = self.headers.get("X-Role", "")
        if not raw:
            raise PermissionDeniedError("缺少 X-Role 请求头")
        try:
            role = resolve_role(unquote(raw))
        except ValueError as exc:
            raise ValidationError(str(exc))
        party_id = self.headers.get("X-Party-Id") or None
        return role, party_id

    def _require_case_role(self) -> tuple[str, Optional[str]]:
        role, party_id = self._identity()
        if role not in CASE_ROLES:
            raise PermissionDeniedError("该接口仅办案角色可访问")
        return role, party_id

    def _handle_errors(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except ValidationError as exc:
            self._send_json(400, {"error": "validation_error", "message": str(exc)})
        except PermissionDeniedError as exc:
            self._send_json(403, {"error": "permission_denied", "message": str(exc)})
        except NotFoundError as exc:
            self._send_json(404, {"error": "not_found", "message": str(exc)})
        except ConflictError as exc:
            self._send_json(409, {"error": "conflict", "message": str(exc)})
        except CaseSystemError as exc:
            self._send_json(400, {"error": "case_error", "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静化，审计以系统内记录为准
        return

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:
        self._handle_errors(lambda: self._route_get())

    def do_POST(self) -> None:
        self._handle_errors(lambda: self._route_post())

    def _route_get(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        sys = self.system
        ip = self.client_address[0] if self.client_address else None

        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/cases":
            role, party_id = self._identity()
            items = sys.list_cases(role, party_id, state=query.get("state"), service_keyword=query.get("q"), client_ip=ip)
            self._send_json(200, {"count": len(items), "cases": items})
            return
        if path == "/audit":
            role, party_id = self._require_case_role()
            self._send_json(200, {"entries": sys.list_audit(limit=int(query.get("limit", 200)))})
            return

        m = re.fullmatch(r"/cases/([^/]+)", path)
        if m:
            role, party_id = self._identity()
            self._send_json(200, sys.view_case(m.group(1), role, party_id, ip))
            return
        m = re.fullmatch(r"/cases/([^/]+)/timeline", path)
        if m:
            role, party_id = self._identity()
            self._send_json(200, sys.timeline(m.group(1), role, party_id, ip))
            return
        m = re.fullmatch(r"/cases/([^/]+)/export", path)
        if m:
            role, party_id = self._identity()
            reason = query.get("reason")
            self._send_json(200, sys.export_case(m.group(1), role, party_id, reason, ip))
            return
        m = re.fullmatch(r"/cases/([^/]+)/exports", path)
        if m:
            self._require_case_role()
            self._send_json(200, {"exports": sys.list_exports(m.group(1))})
            return
        m = re.fullmatch(r"/cases/([^/]+)/links", path)
        if m:
            role, party_id = self._identity()
            sys.require_viewer(m.group(1), role, party_id, "查看关联案件", ip)
            self._send_json(200, {"links": sys.list_links(m.group(1))})
            return
        m = re.fullmatch(r"/cases/([^/]+)/preservations", path)
        if m:
            role, party_id = self._identity()
            sys.require_viewer(m.group(1), role, party_id, "查看保全记录", ip)
            self._send_json(200, {"preservations": sys.list_preservations(m.group(1))})
            return
        m = re.fullmatch(r"/cases/([^/]+)/audit", path)
        if m:
            self._require_case_role()
            self._send_json(200, {"entries": sys.list_audit(m.group(1))})
            return
        self._send_json(404, {"error": "not_found", "message": f"无此路径：{path}"})

    def _route_post(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        body = self._read_json()
        sys = self.system

        if path == "/cases":
            role, party_id = self._identity()
            case = sys.create_case(
                service_name=body.get("service_name", ""),
                claim=body.get("claim", ""),
                complainant=body.get("complainant") or {},
                respondent=body.get("respondent"),
                internal_note=body.get("internal_note"),
                actor_role=role,
                actor_party=party_id,
                case_no=body.get("case_no"),
            )
            self._send_json(201, case)
            return

        m = re.fullmatch(r"/cases/([^/]+)/evidence", path)
        if m:
            role, party_id = self._identity()
            evidence = sys.add_evidence(
                case_id=m.group(1),
                channel=body.get("channel", ""),
                title=body.get("title", ""),
                file_sha256=body.get("file_sha256", ""),
                actor_role=role,
                actor_party=party_id,
                file_name=body.get("file_name"),
                file_size=body.get("file_size", 0),
                content_text=body.get("content_text"),
                summary=body.get("summary"),
                supersedes_id=body.get("supersedes_id"),
            )
            self._send_json(201, evidence)
            return

        m = re.fullmatch(r"/cases/([^/]+)/statements", path)
        if m:
            role, party_id = self._identity()
            statement = sys.add_statement(
                case_id=m.group(1),
                content=body.get("content", ""),
                actor_role=role,
                actor_party=party_id,
                change_note=body.get("change_note"),
            )
            self._send_json(201, statement)
            return

        m = re.fullmatch(r"/cases/([^/]+)/links", path)
        if m:
            role, party_id = self._identity()
            result = sys.link_cases(
                case_id=m.group(1),
                linked_case_id=body.get("linked_case_id", ""),
                reason=body.get("reason", ""),
                actor_role=role,
                actor_party=party_id,
            )
            self._send_json(201, result)
            return

        m = re.fullmatch(r"/cases/([^/]+)/transitions", path)
        if m:
            role, party_id = self._identity()
            result = sys.transition(
                case_id=m.group(1),
                action=body.get("action", ""),
                actor_role=role,
                actor_party=party_id,
                note=body.get("note"),
            )
            self._send_json(200, result)
            return

        m = re.fullmatch(r"/cases/([^/]+)/preservations", path)
        if m:
            role, party_id = self._identity()
            self._send_json(201, sys.preserve(m.group(1), role, party_id))
            return

        m = re.fullmatch(r"/cases/([^/]+)/verify", path)
        if m:
            self._require_case_role()
            self._send_json(200, sys.verify_chains(m.group(1)))
            return

        self._send_json(404, {"error": "not_found", "message": f"无此路径：{path}"})


def build_server(db_path: str, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    system = create_system(db_path)

    class _Handler(CaseHTTPHandler):
        pass

    _Handler.system = system
    server = ThreadingHTTPServer((host, port), _Handler)
    server.case_system = system  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="消费者投诉证据链案件系统")
    parser.add_argument("--db", default="data/case_system.sqlite3", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port)
    print(f"案件系统已启动：http://{args.host}:{args.port}（数据库：{args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
