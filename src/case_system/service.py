"""案件系统业务服务。

所有写操作在单个 SQLite 连接锁内完成并同时写入时间线事件；
任何查看/导出/越权拒绝都追加全局审计哈希链。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .database import GENESIS_HASH, Database
from .errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from .models import (
    ALL_ROLES,
    CASE_ROLES,
    EVIDENCE_CHANNELS,
    PARTY_COMPLAINANT,
    PARTY_RESPONDENT,
    ROLE_PARTY,
    STATES,
    find_rule,
)
from .projection import project_case, viewer_key


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def row_to_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class CaseSystem:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- 基础

    def _conn(self):
        return self.db.conn()

    def _party_side(self, case: dict[str, Any], party_id: Optional[str]) -> Optional[str]:
        if not party_id:
            return None
        if party_id == case["complainant_id"]:
            return PARTY_COMPLAINANT
        if party_id == case.get("respondent_id"):
            return PARTY_RESPONDENT
        return None

    def _get_case(self, case_id: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM cases WHERE case_id = ?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return row_to_dict(row)

    def _add_event(
        self,
        case_id: str,
        event_type: str,
        actor_role: str,
        actor_party: Optional[str],
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        conn = self._conn()
        seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM case_events WHERE case_id = ?", (case_id,)).fetchone())["s"]
        conn.execute(
            "INSERT INTO case_events (event_id, case_id, seq, event_type, from_state, to_state,"
            " actor_role, actor_party, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                new_id(),
                case_id,
                seq,
                event_type,
                from_state,
                to_state,
                actor_role,
                actor_party,
                canonical_json(detail) if detail is not None else None,
                utc_now(),
            ),
        )

    def audit(
        self,
        actor_role: str,
        action: str,
        object_type: str,
        object_id: Optional[str],
        result: str,
        actor_party: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        client_ip: Optional[str] = None,
    ) -> dict[str, Any]:
        """追加一条全局审计记录（哈希链）。"""
        conn = self._conn()
        seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM audit_log").fetchone())["s"]
        last = conn.execute("SELECT record_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last["record_hash"] if last else GENESIS_HASH
        created_at = utc_now()
        basis = canonical_json(
            {
                "prev": prev_hash,
                "seq": seq,
                "actor_role": actor_role,
                "actor_party": actor_party,
                "action": action,
                "object_type": object_type,
                "object_id": object_id,
                "result": result,
                "detail": detail or {},
                "created_at": created_at,
            }
        )
        record_hash = sha256_text(basis)
        audit_id = new_id()
        conn.execute(
            "INSERT INTO audit_log (audit_id, seq, actor_role, actor_party, action, object_type, object_id,"
            " result, detail_json, client_ip, prev_hash, record_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                audit_id,
                seq,
                actor_role,
                actor_party,
                action,
                object_type,
                object_id,
                result,
                canonical_json(detail) if detail is not None else None,
                client_ip,
                prev_hash,
                record_hash,
                created_at,
            ),
        )
        return {"audit_id": audit_id, "seq": seq, "record_hash": record_hash, "created_at": created_at}

    def _authorize_viewer(self, case: dict[str, Any], role: str, party_id: Optional[str]) -> Optional[str]:
        """返回查看者在本案中的当事人身份；无权时抛异常（调用方负责留 denied 审计）。"""
        if role in CASE_ROLES:
            return None
        if role != ROLE_PARTY:
            raise ValidationError(f"未知角色：{role}")
        side = self._party_side(case, party_id)
        if side is None:
            raise PermissionDeniedError("当事人与该案件无关，禁止查看")
        return side

    # ---------------------------------------------------------------- 登记

    def create_case(
        self,
        service_name: str,
        claim: str,
        complainant: dict[str, Any],
        respondent: Optional[dict[str, Any]] = None,
        internal_note: Optional[str] = None,
        actor_role: str = "机构合规员",
        actor_party: Optional[str] = None,
        case_no: Optional[str] = None,
    ) -> dict[str, Any]:
        """登记新投诉案件。投诉人信息必填；被投诉人可后补但不影响既有材料。"""
        if not service_name or not str(service_name).strip():
            raise ValidationError("涉事服务不能为空")
        if not claim or not str(claim).strip():
            raise ValidationError("独立诉求不能为空")
        if actor_role not in CASE_ROLES and actor_role != ROLE_PARTY:
            raise ValidationError(f"未知角色：{actor_role}")
        for required in ("name",):
            if not complainant.get(required):
                raise ValidationError(f"投诉人缺少字段：{required}")

        now = utc_now()
        case_id = new_id()
        complainant_id = new_id()
        with self.db.lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO parties (party_id, side, name, phone, id_no, address, contact, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    complainant_id,
                    PARTY_COMPLAINANT,
                    complainant["name"],
                    complainant.get("phone"),
                    complainant.get("id_no"),
                    complainant.get("address"),
                    complainant.get("contact"),
                    now,
                ),
            )
            respondent_id = None
            if respondent:
                if not respondent.get("name"):
                    raise ValidationError("被投诉人缺少名称")
                respondent_id = new_id()
                conn.execute(
                    "INSERT INTO parties (party_id, side, name, phone, id_no, address, contact, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        respondent_id,
                        PARTY_RESPONDENT,
                        respondent["name"],
                        respondent.get("phone"),
                        respondent.get("id_no"),
                        respondent.get("address"),
                        respondent.get("contact"),
                        now,
                    ),
                )
            if case_no is None:
                case_no = f"CS-{now[:10].replace('-', '')}-{case_id[:8].upper()}"
            state = "登记"
            conn.execute(
                "INSERT INTO cases (case_id, case_no, complainant_id, respondent_id, service_name, claim,"
                " internal_note, state, created_by_role, created_by_party, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    case_no,
                    complainant_id,
                    respondent_id,
                    service_name.strip(),
                    claim.strip(),
                    internal_note,
                    state,
                    actor_role,
                    actor_party,
                    now,
                    now,
                ),
            )
            self._add_event(case_id, "案件登记", actor_role, actor_party, None, state, {"case_no": case_no, "service_name": service_name})
            self.audit(actor_role, "登记案件", "case", case_id, "allowed", actor_party, {"case_no": case_no})
            conn.commit()
        return self.get_case_raw(case_id)

    def get_case_raw(self, case_id: str) -> dict[str, Any]:
        """含当事人字段的完整案件行（内部使用，不做裁剪）。"""
        case = self._get_case(case_id)
        conn = self._conn()
        comp = row_to_dict(conn.execute("SELECT * FROM parties WHERE party_id = ?", (case["complainant_id"],)).fetchone())
        case["complainant"] = comp
        for key, prefix in (("name", "complainant_name"), ("phone", "complainant_phone"), ("id_no", "complainant_id_no"), ("address", "complainant_address")):
            case[prefix] = comp[key]
        resp = None
        if case.get("respondent_id"):
            resp = row_to_dict(conn.execute("SELECT * FROM parties WHERE party_id = ?", (case["respondent_id"],)).fetchone())
            for key, prefix in (("name", "respondent_name"), ("phone", "respondent_phone"), ("id_no", "respondent_id_no"), ("contact", "respondent_contact")):
                case[prefix] = resp[key]
        case["respondent"] = resp
        return case

    # ---------------------------------------------------------------- 材料

    def add_evidence(
        self,
        case_id: str,
        channel: str,
        title: str,
        file_sha256: str,
        actor_role: str,
        actor_party: Optional[str] = None,
        file_name: Optional[str] = None,
        file_size: int = 0,
        content_text: Optional[str] = None,
        summary: Optional[str] = None,
        supersedes_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """接收一份文件摘要作为证据材料。

        证据只增：即使是对旧材料的更正/后补，也通过 ``supersedes_id``
        指向旧证据，旧记录原样保留。
        """
        if channel not in EVIDENCE_CHANNELS:
            raise ValidationError(f"未知证据渠道：{channel}，允许：{('、'.join(EVIDENCE_CHANNELS))}")
        if not file_sha256 or len(file_sha256) != 64:
            raise ValidationError("file_sha256 必须为 64 位十六进制摘要")
        if not title or not title.strip():
            raise ValidationError("证据标题不能为空")

        with self.db.lock:
            case = self._get_case(case_id)
            if actor_role == ROLE_PARTY:
                side = self._party_side(case, actor_party)
                if side is None:
                    self.audit(actor_role, "提交证据", "case", case_id, "denied", actor_party, {"channel": channel})
                    self._conn().commit()
                    raise PermissionDeniedError("当事人与该案件无关，禁止提交材料")
            elif actor_role not in CASE_ROLES:
                raise ValidationError(f"未知角色：{actor_role}")

            if supersedes_id:
                old = self._conn().execute(
                    "SELECT evidence_id FROM evidence_items WHERE evidence_id = ? AND case_id = ?",
                    (supersedes_id, case_id),
                ).fetchone()
                if old is None:
                    raise ValidationError("supersedes_id 指向的原证据不存在或不属于本案")

            evidence_id = new_id()
            now = utc_now()
            self._conn().execute(
                "INSERT INTO evidence_items (evidence_id, case_id, channel, title, file_name, file_sha256,"
                " file_size, content_text, summary, submitted_by_role, submitted_by_party, supersedes_id, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    case_id,
                    channel,
                    title.strip(),
                    file_name,
                    file_sha256.lower(),
                    int(file_size or 0),
                    content_text,
                    summary,
                    actor_role,
                    actor_party,
                    supersedes_id,
                    now,
                ),
            )
            self._add_event(
                case_id,
                "证据提交",
                actor_role,
                actor_party,
                detail={"evidence_id": evidence_id, "channel": channel, "title": title, "supersedes_id": supersedes_id},
            )
            self.audit(actor_role, "提交证据", "evidence", evidence_id, "allowed", actor_party, {"case_id": case_id, "channel": channel})
            self._conn().commit()
        return self.get_evidence(evidence_id)

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        row = self._conn().execute("SELECT * FROM evidence_items WHERE evidence_id = ?", (evidence_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"证据不存在：{evidence_id}")
        return row_to_dict(row)

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM evidence_items WHERE case_id = ? ORDER BY created_at, rowid", (case_id,)
        ).fetchall()
        return [row_to_dict(r) for r in rows]

    def add_statement(
        self,
        case_id: str,
        content: str,
        actor_role: str,
        actor_party: Optional[str] = None,
        change_note: Optional[str] = None,
    ) -> dict[str, Any]:
        """追加一个陈述版本。历史版本永不覆盖。"""
        if not content or not content.strip():
            raise ValidationError("陈述内容不能为空")
        with self.db.lock:
            case = self._get_case(case_id)
            if actor_role == ROLE_PARTY:
                if self._party_side(case, actor_party) is None:
                    self.audit(actor_role, "提交陈述", "case", case_id, "denied", actor_party)
                    self._conn().commit()
                    raise PermissionDeniedError("当事人与该案件无关，禁止提交陈述")
            elif actor_role not in CASE_ROLES:
                raise ValidationError(f"未知角色：{actor_role}")

            conn = self._conn()
            version_no = (conn.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM statements WHERE case_id = ?", (case_id,)).fetchone())["v"]
            statement_id = new_id()
            now = utc_now()
            conn.execute(
                "INSERT INTO statements (statement_id, case_id, version_no, content, change_note,"
                " submitted_by_role, submitted_by_party, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (statement_id, case_id, version_no, content.strip(), change_note, actor_role, actor_party, now),
            )
            self._add_event(
                case_id, "陈述补充", actor_role, actor_party,
                detail={"statement_id": statement_id, "version_no": version_no, "change_note": change_note},
            )
            self.audit(actor_role, "提交陈述", "statement", statement_id, "allowed", actor_party, {"case_id": case_id, "version_no": version_no})
            conn.commit()
        return row_to_dict(conn.execute("SELECT * FROM statements WHERE statement_id = ?", (statement_id,)).fetchone())

    def list_statements(self, case_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM statements WHERE case_id = ? ORDER BY version_no", (case_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

    # ---------------------------------------------------------------- 关联

    def link_cases(
        self,
        case_id: str,
        linked_case_id: str,
        reason: str,
        actor_role: str,
        actor_party: Optional[str] = None,
    ) -> dict[str, Any]:
        """关联重复投诉。双向写入；两案各自保留独立诉求与材料。"""
        if case_id == linked_case_id:
            raise ValidationError("案件不能与自身关联")
        if not reason or not reason.strip():
            raise ValidationError("关联理由不能为空")
        if actor_role not in CASE_ROLES:
            if actor_role == ROLE_PARTY:
                raise PermissionDeniedError("当事人无权建立案件关联")
            raise ValidationError(f"未知角色：{actor_role}")
        with self.db.lock:
            case_a = self._get_case(case_id)
            case_b = self._get_case(linked_case_id)
            conn = self._conn()
            now = utc_now()
            inserted = False
            for a, b in ((case_id, linked_case_id), (linked_case_id, case_id)):
                exists = conn.execute(
                    "SELECT 1 FROM case_links WHERE case_id = ? AND linked_case_id = ?", (a, b)
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO case_links (case_id, linked_case_id, reason, created_by, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (a, b, reason.strip(), actor_role, now),
                    )
                    inserted = True
            if inserted:
                for c, other in ((case_id, linked_case_id), (linked_case_id, case_id)):
                    self._add_event(c, "重复投诉关联", actor_role, actor_party, detail={"linked_case_id": other, "reason": reason})
                self.audit(actor_role, "关联案件", "case", case_id, "allowed", actor_party, {"linked_case_id": linked_case_id, "reason": reason})
            conn.commit()
        return {"case_id": case_id, "linked_case_id": linked_case_id, "reason": reason, "already_linked": not inserted}

    def list_links(self, case_id: str) -> list[dict[str, Any]]:
        self._get_case(case_id)
        rows = self._conn().execute(
            "SELECT cl.*, c.case_no, c.state, c.claim FROM case_links cl"
            " JOIN cases c ON c.case_id = cl.linked_case_id WHERE cl.case_id = ? ORDER BY cl.created_at",
            (case_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]

    # ---------------------------------------------------------------- 状态机

    def transition(
        self,
        case_id: str,
        action: str,
        actor_role: str,
        actor_party: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        """执行受约束的状态流转（调解/转执法/撤回/复开/归档等）。"""
        rule = find_rule(action)
        with self.db.lock:
            case = self._get_case(case_id)
            current = case["state"]

            # 当事人动作仅限投诉人本人（提交核验、撤回）
            if actor_role == ROLE_PARTY:
                side = self._party_side(case, actor_party)
                if side != PARTY_COMPLAINANT:
                    self.audit(actor_role, action, "case", case_id, "denied", actor_party, {"from_state": current})
                    self._conn().commit()
                    raise PermissionDeniedError("仅投诉人本人可执行该动作")
            elif actor_role not in ALL_ROLES:
                raise ValidationError(f"未知角色：{actor_role}")

            if not rule.allowed(current, actor_role):
                self.audit(actor_role, action, "case", case_id, "denied", actor_party, {"from_state": current, "reason": "状态或角色不允许"})
                self._conn().commit()
                if current not in rule.sources:
                    raise ConflictError(f"状态[{current}]不允许动作[{action}]（允许的源状态：{'、'.join(sorted(rule.sources))}）")
                raise PermissionDeniedError(f"角色[{actor_role}]无权执行动作[{action}]")

            now = utc_now()
            dest = rule.dest
            conn = self._conn()
            conn.execute("UPDATE cases SET state = ?, updated_at = ? WHERE case_id = ?", (dest, now, case_id))
            self._add_event(case_id, action, actor_role, actor_party, current, dest, {"note": note})
            self.audit(actor_role, action, "case", case_id, "allowed", actor_party, {"from_state": current, "to_state": dest, "note": note})
            conn.commit()
            return {"case_id": case_id, "action": action, "from_state": current, "to_state": dest, "acted_at": now}

    # ---------------------------------------------------------------- 保全

    def _build_snapshot(self, case_id: str, seq: int, state_locked: str) -> dict[str, Any]:
        conn = self._conn()
        evidence = [
            {k: row[k] for k in ("evidence_id", "channel", "title", "file_name", "file_sha256", "file_size", "supersedes_id", "submitted_by_role", "created_at")}
            for row in conn.execute("SELECT * FROM evidence_items WHERE case_id = ? ORDER BY created_at, rowid", (case_id,))
        ]
        statements = [
            {k: row[k] for k in ("statement_id", "version_no", "content", "change_note", "submitted_by_role", "created_at")}
            for row in conn.execute("SELECT * FROM statements WHERE case_id = ? ORDER BY version_no", (case_id,))
        ]
        links = [r["linked_case_id"] for r in conn.execute("SELECT linked_case_id FROM case_links WHERE case_id = ? ORDER BY created_at", (case_id,))]
        case = self._get_case(case_id)
        return {
            "case_id": case_id,
            "case_no": case["case_no"],
            "state": state_locked,
            "service_name": case["service_name"],
            "claim": case["claim"],
            "seq": seq,
            "evidence": evidence,
            "statements": statements,
            "links": links,
        }

    def preserve(self, case_id: str, actor_role: str, actor_party: Optional[str] = None) -> dict[str, Any]:
        """对当前案件材料做一次证据保全：全量快照 + 哈希链串联。"""
        if actor_role not in CASE_ROLES:
            raise PermissionDeniedError("仅办案角色可执行证据保全")
        with self.db.lock:
            case = self._get_case(case_id)
            conn = self._conn()
            seq = (conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM preservations WHERE case_id = ?", (case_id,)).fetchone())["s"]
            prev = conn.execute(
                "SELECT COALESCE((SELECT record_hash FROM preservations WHERE case_id = ? ORDER BY seq DESC LIMIT 1), ?) AS p",
                (case_id, GENESIS_HASH),
            ).fetchone()["p"]
            now = utc_now()
            snapshot = self._build_snapshot(case_id, seq, case["state"])
            snapshot_json = canonical_json(snapshot)
            snapshot_hash = sha256_text(snapshot_json)
            record_hash = sha256_text(prev + f"{seq}" + case["state"] + snapshot_hash + actor_role + now)
            preservation_id = new_id()
            conn.execute(
                "INSERT INTO preservations (preservation_id, case_id, seq, state_locked, snapshot_json,"
                " prev_hash, record_hash, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (preservation_id, case_id, seq, case["state"], snapshot_json, prev, record_hash, actor_role, now),
            )
            self._add_event(case_id, "证据保全", actor_role, actor_party, detail={"preservation_id": preservation_id, "seq": seq, "snapshot_sha256": snapshot_hash})
            self.audit(actor_role, "证据保全", "preservation", preservation_id, "allowed", actor_party, {"case_id": case_id, "seq": seq})
            conn.commit()
        return {
            "preservation_id": preservation_id,
            "case_id": case_id,
            "seq": seq,
            "state_locked": case["state"],
            "snapshot_sha256": snapshot_hash,
            "prev_hash": prev,
            "record_hash": record_hash,
            "created_at": now,
        }

    def list_preservations(self, case_id: str) -> list[dict[str, Any]]:
        self._get_case(case_id)
        rows = self._conn().execute(
            "SELECT preservation_id, case_id, seq, state_locked, prev_hash, record_hash, created_by, created_at"
            " FROM preservations WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]

    def require_viewer(
        self,
        case_id: str,
        role: str,
        party_id: Optional[str],
        action: str = "查看案件材料",
        client_ip: Optional[str] = None,
    ) -> Optional[str]:
        """校验查看者能否访问该案；无权时留 denied 审计并抛异常。返回当事人身份。"""
        with self.db.lock:
            case = self._get_case(case_id)
            try:
                return self._authorize_viewer(case, role, party_id)
            except PermissionDeniedError:
                self.audit(role, action, "case", case_id, "denied", party_id, {"reason": "非本案当事人"}, client_ip)
                self._conn().commit()
                raise

    # ---------------------------------------------------------------- 查看

    def view_case(
        self,
        case_id: str,
        role: str,
        party_id: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> dict[str, Any]:
        """查看案件（留下审计，敏感字段按身份裁剪）。"""
        with self.db.lock:
            try:
                case_raw = self._get_case(case_id)
            except NotFoundError:
                self.audit(role, "查看案件", "case", case_id, "denied", party_id, {"reason": "不存在"}, client_ip)
                self._conn().commit()
                raise
            try:
                side = self._authorize_viewer(case_raw, role, party_id)
            except PermissionDeniedError:
                self.audit(role, "查看案件", "case", case_id, "denied", party_id, {"reason": "非本案当事人"}, client_ip)
                self._conn().commit()
                raise
            case = self.get_case_raw(case_id)
            result = project_case(case, role, side)
            # 嵌套当事人对象含完整敏感字段，统一移出投影结果（裁剪只作用于扁平字段）
            result.pop("complainant", None)
            result.pop("respondent", None)
            result["links"] = self.list_links(case_id)
            result["preservations"] = self.list_preservations(case_id)
            counts = self._conn().execute(
                "SELECT (SELECT COUNT(*) FROM evidence_items WHERE case_id = ?) AS evidence_count,"
                " (SELECT COUNT(*) FROM statements WHERE case_id = ?) AS statement_count",
                (case_id, case_id),
            ).fetchone()
            result["evidence_count"] = counts["evidence_count"]
            result["statement_count"] = counts["statement_count"]
            result["_viewer"] = viewer_key(role, side)
            self.audit(role, "查看案件", "case", case_id, "allowed", party_id, {"view": result["_viewer"], "redacted": result.get("_redacted_reason")}, client_ip)
            self._conn().commit()
            return result

    def timeline(
        self,
        case_id: str,
        role: str,
        party_id: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> dict[str, Any]:
        """完整时间线：案件事件 + 证据来源明细。"""
        with self.db.lock:
            try:
                case_raw = self._get_case(case_id)
            except NotFoundError:
                self.audit(role, "查看时间线", "case", case_id, "denied", party_id, {"reason": "不存在"}, client_ip)
                self._conn().commit()
                raise
            try:
                side = self._authorize_viewer(case_raw, role, party_id)
            except PermissionDeniedError:
                self.audit(role, "查看时间线", "case", case_id, "denied", party_id, {"reason": "非本案当事人"}, client_ip)
                self._conn().commit()
                raise

            events = [
                row_to_dict(r)
                for r in self._conn().execute(
                    "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq", (case_id,)
                )
            ]
            for event in events:
                if event.get("detail_json"):
                    event["detail"] = json.loads(event.pop("detail_json"))
                else:
                    event.pop("detail_json")
                    event["detail"] = None

            # 证据来源清单：渠道、提交者、摘要、指纹、与旧版本关系
            sources = []
            for ev in self.list_evidence(case_id):
                superseded = None
                if ev.get("supersedes_id"):
                    old = self._conn().execute(
                        "SELECT evidence_id, title, channel, created_at FROM evidence_items WHERE evidence_id = ?",
                        (ev["supersedes_id"],),
                    ).fetchone()
                    superseded = row_to_dict(old) if old else None
                sources.append(
                    {
                        "evidence_id": ev["evidence_id"],
                        "channel": ev["channel"],
                        "title": ev["title"],
                        "file_name": ev["file_name"],
                        "file_sha256": ev["file_sha256"],
                        "file_size": ev["file_size"],
                        "summary": ev["summary"],
                        "submitted_by_role": ev["submitted_by_role"],
                        "created_at": ev["created_at"],
                        "supersedes": superseded,
                    }
                )

            statements = [
                {k: row[k] for k in ("statement_id", "version_no", "content", "change_note", "submitted_by_role", "created_at")}
                for row in self.list_statements(case_id)
            ]
            self.audit(role, "查看时间线", "case", case_id, "allowed", party_id, {"events": len(events), "sources": len(sources)}, client_ip)
            self._conn().commit()
            return {
                "case_id": case_id,
                "case_no": case_raw["case_no"],
                "state": case_raw["state"],
                "viewer": viewer_key(role, side),
                "events": events,
                "evidence_sources": sources,
                "statement_versions": statements,
                "preservations": self.list_preservations(case_id),
            }

    def list_cases(
        self,
        role: str,
        party_id: Optional[str] = None,
        state: Optional[str] = None,
        service_keyword: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT c.*, p.name AS complainant_name FROM cases c JOIN parties p ON p.party_id = c.complainant_id WHERE 1=1"
        params: list[Any] = []
        with self.db.lock:
            if role == ROLE_PARTY:
                if not party_id:
                    raise PermissionDeniedError("当事人查询需提供 party_id")
                sql += " AND (c.complainant_id = ? OR c.respondent_id = ?)"
                params.extend([party_id, party_id])
            elif role not in CASE_ROLES:
                raise ValidationError(f"未知角色：{role}")
            if state:
                if state not in STATES:
                    raise ValidationError(f"未知状态：{state}")
                sql += " AND c.state = ?"
                params.append(state)
            if service_keyword:
                sql += " AND c.service_name LIKE ?"
                params.append(f"%{service_keyword}%")
            sql += " ORDER BY c.created_at"
            rows = self._conn().execute(sql, params).fetchall()
            items = [row_to_dict(r) for r in rows]
            # 列表同样裁剪敏感字段：当事人/执业人员不可见内部备注
            if role in (ROLE_PARTY, "执业人员"):
                for item in items:
                    item["internal_note"] = None
            self.audit(
                role, "查询案件列表", "case", None, "allowed", party_id,
                {"count": len(items), "state": state, "q": service_keyword}, client_ip,
            )
            self._conn().commit()
        return items

    # ---------------------------------------------------------------- 导出

    def export_case(
        self,
        case_id: str,
        role: str,
        party_id: Optional[str] = None,
        reason: Optional[str] = None,
        client_ip: Optional[str] = None,
    ) -> dict[str, Any]:
        """导出案件包：按身份裁剪，载荷指纹固化并登记。"""
        with self.db.lock:
            case_raw = self._get_case(case_id)
            try:
                side = self._authorize_viewer(case_raw, role, party_id)
            except PermissionDeniedError:
                self.audit(role, "导出案件", "case", case_id, "denied", party_id, {"reason": "非本案当事人"}, client_ip)
                self._conn().commit()
                raise

            bundle = self.timeline(case_id, role, party_id, client_ip)
            case_view = project_case(self.get_case_raw(case_id), role, side)
            case_view.pop("complainant", None)
            case_view.pop("respondent", None)
            payload = {
                "exported_at": utc_now(),
                "redaction_view": viewer_key(role, side),
                "redaction_note": case_view.get("_redacted_reason"),
                "case": case_view,
                "timeline": bundle["events"],
                "evidence_sources": bundle["evidence_sources"],
                "statement_versions": bundle["statement_versions"],
                "preservations": bundle["preservations"],
            }
            payload_text = canonical_json(payload)
            digest = sha256_text(payload_text)
            export_id = new_id()
            now = utc_now()
            conn = self._conn()
            conn.execute(
                "INSERT INTO export_records (export_id, case_id, actor_role, actor_party, reason,"
                " redaction_view, payload_sha256, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (export_id, case_id, role, party_id, reason, payload["redaction_view"], digest, now),
            )
            self._add_event(case_id, "案件导出", role, party_id, detail={"export_id": export_id, "reason": reason, "payload_sha256": digest})
            self.audit(role, "导出案件", "case", case_id, "allowed", party_id, {"export_id": export_id, "reason": reason, "payload_sha256": digest}, client_ip)
            conn.commit()
            payload["export"] = {"export_id": export_id, "reason": reason, "payload_sha256": digest, "created_at": now}
            return payload

    def list_exports(self, case_id: str) -> list[dict[str, Any]]:
        self._get_case(case_id)
        rows = self._conn().execute("SELECT * FROM export_records WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()
        return [row_to_dict(r) for r in rows]

    # ---------------------------------------------------------------- 审计与校验

    def list_audit(self, case_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        """查询审计记录（默认仅办案角色可调，鉴权在 API 层）。"""
        conn = self._conn()
        if case_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE (object_type = 'case' AND object_id = ?)"
                " OR detail_json LIKE ? ORDER BY seq DESC LIMIT ?",
                (case_id, f'%"case_id":"{case_id}"%', limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [row_to_dict(r) for r in rows]

    def verify_chains(self, case_id: str) -> dict[str, Any]:
        """重算案件保全链与全局审计链，检测任何底层篡改。"""
        conn = self._conn()
        self._get_case(case_id)

        breaks: list[dict[str, Any]] = []
        prev = GENESIS_HASH
        preservations = conn.execute("SELECT * FROM preservations WHERE case_id = ? ORDER BY seq", (case_id,)).fetchall()
        for row in preservations:
            if row["prev_hash"] != prev:
                breaks.append({"chain": "preservation", "seq": row["seq"], "problem": "prev_hash 断链"})
            snapshot_hash = sha256_text(row["snapshot_json"])
            expected = sha256_text(row["prev_hash"] + f"{row['seq']}" + row["state_locked"] + snapshot_hash + row["created_by"] + row["created_at"])
            if expected != row["record_hash"]:
                breaks.append({"chain": "preservation", "seq": row["seq"], "problem": "record_hash 不匹配"})
            prev = row["record_hash"]

        audit_breaks = 0
        prev = GENESIS_HASH
        for row in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
            if row["prev_hash"] != prev:
                audit_breaks += 1
            detail = json.loads(row["detail_json"]) if row["detail_json"] else {}
            basis = canonical_json(
                {
                    "prev": row["prev_hash"],
                    "seq": row["seq"],
                    "actor_role": row["actor_role"],
                    "actor_party": row["actor_party"],
                    "action": row["action"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "result": row["result"],
                    "detail": detail,
                    "created_at": row["created_at"],
                }
            )
            if sha256_text(basis) != row["record_hash"]:
                audit_breaks += 1
            prev = row["record_hash"]

        return {
            "case_id": case_id,
            "preservation_records": len(preservations),
            "preservation_intact": not any(b["chain"] == "preservation" for b in breaks),
            "audit_breaks": audit_breaks,
            "breaks": breaks,
            "ok": not breaks and audit_breaks == 0,
        }
