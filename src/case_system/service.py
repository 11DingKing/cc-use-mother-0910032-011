"""案件领域服务：状态机、权限、审计与时间线。

所有写操作在单个 SQLite 事务中完成“业务写入 + 审计落库”，
任何查看（GET）与导出也先写审计再返回数据，保证审计不依赖调用方自觉。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from .errors import CaseError, ConflictError, NotFoundError, PermissionError, ValidationError
from .redaction import Redactor
from .storage import Storage, new_id, utcnow

# 操作 -> 允许的办案角色
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "case.create": {"机构合规员", "监管人员"},
    "party.add": {"机构合规员", "监管人员"},
    "service.add": {"机构合规员", "监管人员"},
    "statement.submit": {"机构合规员", "执业人员", "监管人员", "当事人"},
    "evidence.receive": {"机构合规员", "监管人员"},
    "evidence.supplement": {"机构合规员", "监管人员"},
    "preservation.record": {"机构合规员", "监管人员", "复核专家"},
    "case.link": {"机构合规员", "监管人员"},
    "case.transition": {"机构合规员", "执业人员", "监管人员", "复核专家"},
    "case.view": {"机构合规员", "执业人员", "监管人员", "复核专家", "当事人"},
    "case.export": {"监管人员", "复核专家", "机构合规员"},
    "audit.view": {"监管人员", "复核专家"},
}

# 调解动作只在处置中可用，且不改变状态（可重复，每次留痕）
MEDIATION_ACTION = "调解"

# 导出载体类型
EXPORT_KINDS = {"full", "evidence_pack", "timeline"}


class Actor:
    """请求人：办案角色或当事人。"""

    def __init__(self, actor_id: str, role: str, party_id: str | None = None) -> None:
        if role == "当事人" and not party_id:
            raise ValidationError("当事人请求必须携带 party_id")
        if role != "当事人" and role not in {
            "机构合规员", "执业人员", "监管人员", "复核专家",
        }:
            raise ValidationError(f"未知角色：{role}")
        self.id = actor_id
        self.role = role
        self.party_id = party_id


class CaseService:
    def __init__(self, storage: Storage | str | Path = ":memory:") -> None:
        self.db = storage if isinstance(storage, Storage) else Storage(storage)

    def close(self) -> None:
        self.db.close()

    # -- 内部工具 --------------------------------------------------------

    def _require(self, permission: str, actor: Actor) -> None:
        if actor.role not in ROLE_PERMISSIONS[permission]:
            raise PermissionError(f"角色 {actor.role} 无权执行 {permission}")

    def _get_case_row(self, case_id: str) -> dict[str, Any]:
        case = self.db.query_one("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return case

    def _audit(self, actor: Actor, action: str, resource_type: str,
               result: str, resource_id: str | None = None, case_id: str | None = None,
               purpose: str | None = None, detail: dict[str, Any] | None = None) -> None:
        self.db.insert_audit({
            "audit_id": new_id("aud"),
            "actor_id": actor.id,
            "actor_role": actor.role,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "case_id": case_id,
            "purpose": purpose,
            "result": result,
            "detail": detail or {},
            "created_at": utcnow(),
        })

    def _txn(self, work: Callable[[], Any]) -> Any:
        with self.db.lock:
            try:
                result = work()
                self.db.commit()
                return result
            except Exception:
                self.db.rollback()
                raise

    # -- 案件登记 --------------------------------------------------------

    def create_case(self, actor: Actor, title: str, advertised_service: str | None = None,
                    complainant: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require("case.create", actor)

        def job() -> dict[str, Any]:
            now = utcnow()
            case_id = new_id("case")
            self.db.insert_case({
                "case_id": case_id,
                "title": title,
                "complainant_party_id": None,
                "state": "登记",
                "advertised_service": advertised_service,
                "created_by": actor.id,
                "created_at": now,
            })
            if complainant:
                party_id = new_id("pty")
                self.db.insert_party({
                    "party_id": party_id,
                    "case_id": case_id,
                    "side": "complainant",
                    "name": complainant.get("name"),
                    "phone": complainant.get("phone"),
                    "id_card": complainant.get("id_card"),
                    "address": complainant.get("address"),
                    "created_at": now,
                })
                self.db.set_complainant(case_id, party_id)
            self.db.insert_state_event({
                "event_id": new_id("evt"),
                "case_id": case_id,
                "action": "登记",
                "from_state": None,
                "to_state": "登记",
                "actor_id": actor.id,
                "actor_role": actor.role,
                "detail": {"title": title},
                "created_at": now,
            })
            self._audit(actor, "case.create", "case", "success",
                        resource_id=case_id, case_id=case_id)
            return self._get_case_row(case_id)

        return self._txn(job)

    def add_party(self, actor: Actor, case_id: str, side: str,
                  party: dict[str, Any]) -> dict[str, Any]:
        self._require("party.add", actor)
        if side not in {"complainant", "respondent"}:
            raise ValidationError("side 只能是 complainant 或 respondent")
        self._get_case_row(case_id)

        def job() -> dict[str, Any]:
            party_id = new_id("pty")
            row = {
                "party_id": party_id,
                "case_id": case_id,
                "side": side,
                "name": party.get("name"),
                "phone": party.get("phone"),
                "id_card": party.get("id_card"),
                "address": party.get("address"),
                "created_at": utcnow(),
            }
            self.db.insert_party(row)
            if side == "complainant" and self._get_case_row(case_id)["complainant_party_id"] is None:
                self.db.set_complainant(case_id, party_id)
            self._audit(actor, "party.add", "party", "success",
                        resource_id=party_id, case_id=case_id)
            return row

        return self._txn(job)

    def add_service(self, actor: Actor, case_id: str, name: str,
                    advertised: str | None = None, actual: str | None = None) -> dict[str, Any]:
        """登记涉事服务：宣传内容与实际服务分别留档，供不一致比对。"""
        self._require("service.add", actor)
        self._get_case_row(case_id)

        def job() -> dict[str, Any]:
            svc = {
                "service_id": new_id("svc"),
                "case_id": case_id,
                "name": name,
                "advertised": advertised,
                "actual": actual,
                "created_at": utcnow(),
            }
            self.db.insert_service(svc)
            self._audit(actor, "service.add", "service", "success",
                        resource_id=svc["service_id"], case_id=case_id)
            return svc

        return self._txn(job)

    # -- 陈述版本（只追加） -----------------------------------------------

    def submit_statement(self, actor: Actor, case_id: str, content: str,
                         source_channel: str, supersedes_id: str | None = None) -> dict[str, Any]:
        self._require("statement.submit", actor)
        self._get_case_row(case_id)
        if not content or not content.strip():
            raise ValidationError("陈述内容不能为空")
        if not source_channel:
            raise ValidationError("来源渠道不能为空")

        def job() -> dict[str, Any]:
            if supersedes_id:
                old = self.db.query_one(
                    "SELECT * FROM statements WHERE statement_id=? AND case_id=?",
                    (supersedes_id, case_id),
                )
                if old is None:
                    raise NotFoundError("被替代的陈述版本不存在")
            version_no = self.db.next_statement_version(case_id)
            st = {
                "statement_id": new_id("stm"),
                "case_id": case_id,
                "version_no": version_no,
                "content": content,
                "source_channel": source_channel,
                "submitted_by": actor.id,
                "supersedes_id": supersedes_id,
                "created_at": utcnow(),
            }
            self.db.insert_statement(st)
            self._audit(actor, "statement.submit", "statement", "success",
                        resource_id=st["statement_id"], case_id=case_id,
                        detail={"version_no": version_no,
                                "supersedes": supersedes_id})
            return st

        return self._txn(job)

    # -- 证据接收与后补材料 -----------------------------------------------

    def _validate_digest(self, sha256: str) -> None:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256.lower()):
            raise ValidationError("sha256 必须是 64 位十六进制")

    def receive_evidence(self, actor: Actor, case_id: str, kind: str, title: str,
                         source_channel: str, sha256: str, byte_size: int,
                         storage_ref: str | None = None, metadata: dict[str, Any] | None = None,
                         linked_statement_id: str | None = None) -> dict[str, Any]:
        """接收文件摘要（聊天截图、收费记录等），并立即做接收保全。"""
        self._require("evidence.receive", actor)
        self._get_case_row(case_id)
        if not kind or not title or not source_channel:
            raise ValidationError("证据类型、标题、来源渠道均不能为空")
        self._validate_digest(sha256)
        if not isinstance(byte_size, int) or byte_size < 0:
            raise ValidationError("byte_size 必须是非负整数")

        def job() -> dict[str, Any]:
            if linked_statement_id:
                st = self.db.query_one(
                    "SELECT 1 FROM statements WHERE statement_id=? AND case_id=?",
                    (linked_statement_id, case_id),
                )
                if st is None:
                    raise NotFoundError("关联的陈述不存在")
            now = utcnow()
            ev_id = new_id("evd")
            ev = {
                "evidence_id": ev_id,
                "case_id": case_id,
                "kind": kind,
                "title": title,
                "source_channel": source_channel,
                "current_sha256": sha256.lower(),
                "byte_size": byte_size,
                "storage_ref": storage_ref,
                "metadata": metadata or {},
                "linked_statement_id": linked_statement_id,
                "received_by": actor.id,
                "created_at": now,
            }
            self.db.insert_evidence(ev)
            self._write_preservation(actor, case_id, ev_id, "接收固定",
                                     f"接收 {kind}：{title}", sha256.lower())
            self._audit(actor, "evidence.receive", "evidence", "success",
                        resource_id=ev_id, case_id=case_id,
                        detail={"sha256": sha256.lower(), "version_no": 1})
            ev["versions"] = self.db.list_evidence_versions(ev_id)
            return ev

        return self._txn(job)

    def supplement_evidence(self, actor: Actor, evidence_id: str, sha256: str,
                            byte_size: int, note: str,
                            storage_ref: str | None = None) -> dict[str, Any]:
        """后补材料：不覆盖原证据，追加一个新版本并做保全。

        典型场景：同一段聊天的补充截图、收费记录的完整账单。
        旧版本的哈希与字节数永久保留在 evidence_versions。
        """
        self._require("evidence.supplement", actor)
        self._validate_digest(sha256)
        if not isinstance(byte_size, int) or byte_size < 0:
            raise ValidationError("byte_size 必须是非负整数")
        ev = self.db.query_one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
        if ev is None:
            raise NotFoundError("证据不存在")
        if ev["current_sha256"] == sha256.lower():
            raise ConflictError("补充材料与当前版本哈希相同，未产生新版本")
        case_id = ev["case_id"]

        def job() -> dict[str, Any]:
            version_no = self.db.next_evidence_version(evidence_id)
            now = utcnow()
            self.db.append_evidence_version(
                evidence_id, version_no, sha256.lower(), byte_size, note, actor.id, now,
            )
            if storage_ref:
                self.db.conn.execute(
                    "UPDATE evidence SET storage_ref=? WHERE evidence_id=?",
                    (storage_ref, evidence_id),
                )
            self._write_preservation(
                actor, case_id, evidence_id, f"补充材料 v{version_no}",
                note, sha256.lower(),
            )
            self._audit(actor, "evidence.supplement", "evidence", "success",
                        resource_id=evidence_id, case_id=case_id,
                        detail={"version_no": version_no, "sha256": sha256.lower()})
            return {
                "evidence_id": evidence_id,
                "case_id": case_id,
                "version_no": version_no,
                "sha256": sha256.lower(),
                "byte_size": byte_size,
                "versions": self.db.list_evidence_versions(evidence_id),
            }

        return self._txn(job)

    def _write_preservation(self, actor: Actor, case_id: str, evidence_id: str | None,
                            action: str, detail: str, evidence_sha256: str | None) -> dict[str, Any]:
        rec = {
            "record_id": new_id("prs"),
            "case_id": case_id,
            "evidence_id": evidence_id,
            "action": action,
            "custodian": actor.id,
            "detail": detail,
            "evidence_sha256": evidence_sha256,
            "prev_hash": self.db.latest_preservation_hash(case_id),
            "created_at": utcnow(),
        }
        self.db.insert_preservation(rec)
        return rec

    def record_preservation(self, actor: Actor, case_id: str, action: str,
                            detail: str, evidence_id: str | None = None) -> dict[str, Any]:
        """独立保全动作（如现场封存、移交、校验复核），同样串入哈希链。"""
        self._require("preservation.record", actor)
        self._get_case_row(case_id)
        digest = None
        if evidence_id:
            ev = self.db.query_one("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,))
            if ev is None:
                raise NotFoundError("证据不存在")
            if ev["case_id"] != case_id:
                raise ValidationError("证据不属于该案件")
            digest = ev["current_sha256"]

        def job() -> dict[str, Any]:
            rec = self._write_preservation(actor, case_id, evidence_id, action, detail, digest)
            self._audit(actor, "preservation.record", "preservation", "success",
                        resource_id=rec["record_id"], case_id=case_id,
                        detail={"action": action})
            return rec

        return self._txn(job)

    # -- 重复投诉关联 -----------------------------------------------------

    def link_cases(self, actor: Actor, case_id_a: str, case_id_b: str,
                   reason: str) -> dict[str, Any]:
        """关联重复投诉。关联不合并案件：两边各自保留独立诉求、证据与状态。"""
        self._require("case.link", actor)
        if case_id_a == case_id_b:
            raise ValidationError("不能关联同一案件")
        if not reason or not reason.strip():
            raise ValidationError("关联理由不能为空")
        self._get_case_row(case_id_a)
        self._get_case_row(case_id_b)

        def job() -> dict[str, Any]:
            small, large = sorted((case_id_a, case_id_b))
            existing = self.db.query_one(
                "SELECT * FROM case_links WHERE case_id_a=? AND case_id_b=?",
                (small, large),
            )
            if existing:
                raise ConflictError("两案已存在关联")
            link = {
                "link_id": new_id("lnk"),
                "case_id_a": case_id_a,
                "case_id_b": case_id_b,
                "reason": reason,
                "created_by": actor.id,
                "created_at": utcnow(),
            }
            self.db.insert_link(link)
            self._audit(actor, "case.link", "case_link", "success",
                        resource_id=link["link_id"], case_id=case_id_a,
                        detail={"other_case": case_id_b, "reason": reason})
            self._audit(actor, "case.link", "case_link", "success",
                        resource_id=link["link_id"], case_id=case_id_b,
                        detail={"other_case": case_id_a, "reason": reason})
            return link

        return self._txn(job)

    # -- 状态机 -----------------------------------------------------------

    def _load_transitions(self) -> dict[str, Any]:
        contract_path = Path(__file__).resolve().parents[2] / "domain" / "contract.json"
        return json.loads(contract_path.read_text(encoding="utf-8"))["state_transitions"]

    def transition(self, actor: Actor, case_id: str, action: str,
                   detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行调解 / 转执法 / 撤回 / 复开 / 受理等带状态约束的动作。"""
        self._require("case.transition", actor)
        transitions = self._load_transitions()
        if action not in transitions:
            raise ValidationError(f"未知状态动作：{action}")

        def job() -> dict[str, Any]:
            case = self._get_case_row(case_id)
            current = case["state"]
            rule = transitions[action]
            if current not in rule["from"]:
                raise ConflictError(
                    f"状态约束冲突：不能在「{current}」状态执行「{action}」，"
                    f"允许的来源状态：{'、'.join(rule['from'])}"
                )
            now = utcnow()
            event = {
                "event_id": new_id("evt"),
                "case_id": case_id,
                "action": action,
                "from_state": current,
                "to_state": rule["to"],
                "actor_id": actor.id,
                "actor_role": actor.role,
                "detail": detail or {},
                "created_at": now,
            }
            self.db.insert_state_event(event)
            self.db.update_case_state(case_id, rule["to"])
            if action == "复开":
                self.db.increment_reopened(case_id)
            self._write_preservation(
                actor, case_id, None, f"状态动作：{action}",
                f"{current} -> {rule['to']}", None,
            )
            self._audit(actor, "case.transition", "case", "success",
                        resource_id=case_id, case_id=case_id,
                        detail={"action": action, "from": current, "to": rule["to"]})
            return event

        return self._txn(job)

    # -- 查看（裁剪 + 审计） ----------------------------------------------

    def _redactor(self, actor: Actor) -> Redactor:
        return Redactor(actor.role, actor.party_id)

    def _check_case_visibility(self, actor: Actor, case: dict[str, Any]) -> None:
        if actor.role != "当事人":
            return
        owner_ids = {p["party_id"] for p in self.db.list_parties(case["case_id"])}
        if actor.party_id not in owner_ids:
            raise PermissionError("当事人只能查看自己作为当事人的案件")

    def get_case(self, actor: Actor, case_id: str, purpose: str | None = None) -> dict[str, Any]:
        self._require("case.view", actor)
        with self.db.lock:
            case = self._get_case_row(case_id)
            self._check_case_visibility(actor, case)
            redactor = self._redactor(actor)
            parties = [redactor.redact_party(p) for p in self.db.list_parties(case_id)]
            self._audit(actor, "case.view", "case", "success", resource_id=case_id,
                        case_id=case_id, purpose=purpose)
            self.db.commit()
        return {
            "case": case,
            "parties": parties,
            "services": self.db.list_services(case_id),
            "state": case["state"],
        }

    def list_evidence(self, actor: Actor, case_id: str,
                      purpose: str | None = None) -> list[dict[str, Any]]:
        self._require("case.view", actor)
        with self.db.lock:
            case = self._get_case_row(case_id)
            self._check_case_visibility(actor, case)
            evidence = self.db.list_evidence(case_id)
            for ev in evidence:
                ev["versions"] = self.db.list_evidence_versions(ev["evidence_id"])
            self._audit(actor, "evidence.list", "evidence", "success",
                        resource_id=case_id, case_id=case_id, purpose=purpose,
                        detail={"count": len(evidence)})
            self.db.commit()
        return evidence

    def get_timeline(self, actor: Actor, case_id: str,
                     purpose: str | None = None) -> dict[str, Any]:
        """完整时间线：登记、陈述版本、证据来源与版本、保全、状态动作、关联，按时间归一排序。"""
        self._require("case.view", actor)
        case = self._get_case_row(case_id)
        self._check_case_visibility(actor, case)
        redactor = self._redactor(actor)

        with self.db.lock:
            items: list[tuple[str, str, dict[str, Any]]] = []

            items.append((case["created_at"], "case_created", {
                "case_id": case_id, "title": case["title"],
                "advertised_service": case["advertised_service"],
                "created_by": case["created_by"],
            }))
            for p in self.db.list_parties(case_id):
                items.append((p["created_at"], "party_added", redactor.redact_party(p)))
            for s in self.db.list_services(case_id):
                items.append((s["created_at"], "service_recorded", s))
            for st in self.db.list_statements(case_id):
                items.append((st["created_at"], "statement_version", {
                    "statement_id": st["statement_id"],
                    "version_no": st["version_no"],
                    "content": st["content"],
                    "source_channel": st["source_channel"],
                    "submitted_by": st["submitted_by"],
                    "supersedes_id": st["supersedes_id"],
                }))
            for ev in self.db.list_evidence(case_id):
                versions = self.db.list_evidence_versions(ev["evidence_id"])
                items.append((ev["created_at"], "evidence_received", {
                    "evidence_id": ev["evidence_id"],
                    "kind": ev["kind"],
                    "title": ev["title"],
                    "source_channel": ev["source_channel"],
                    "sha256": ev["current_sha256"],
                    "byte_size": ev["byte_size"],
                    "version_no": len(versions),
                    "versions": versions,
                    "linked_statement_id": ev["linked_statement_id"],
                    "metadata": ev["metadata"],
                }))
            for rec in self.db.list_preservation(case_id):
                items.append((rec["created_at"], "preservation", {
                    "record_id": rec["record_id"],
                    "action": rec["action"],
                    "custodian": rec["custodian"],
                    "detail": rec["detail"],
                    "evidence_id": rec["evidence_id"],
                    "evidence_sha256": rec["evidence_sha256"],
                    "prev_hash": rec["prev_hash"],
                    "record_hash": rec["record_hash"],
                }))
            for ev in self.db.list_state_events(case_id):
                items.append((ev["created_at"], "state_event", ev))
            for link in self.db.list_links(case_id):
                other = link["case_id_b"] if link["case_id_a"] == case_id else link["case_id_a"]
                items.append((link["created_at"], "case_linked", {
                    "link_id": link["link_id"],
                    "related_case_id": other,
                    "reason": link["reason"],
                    "created_by": link["created_by"],
                }))

            items.sort(key=lambda x: x[0])
            chain = self.db.verify_preservation_chain(case_id)
            self._audit(actor, "timeline.view", "timeline", "success",
                        resource_id=case_id, case_id=case_id, purpose=purpose,
                        detail={"items": len(items), "chain_intact": chain["intact"]})
            self.db.commit()
        return {
            "case_id": case_id,
            "state": case["state"],
            "reopened_count": case["reopened_count"],
            "preservation_chain": chain,
            "events": [{"at": at, "type": kind, "data": data} for at, kind, data in items],
        }

    # -- 导出（留审计 + 哈希链校验） ---------------------------------------

    def export_case(self, actor: Actor, case_id: str, kind: str = "full",
                    purpose: str | None = None) -> dict[str, Any]:
        self._require("case.export", actor)
        if kind not in EXPORT_KINDS:
            raise ValidationError(f"导出类型必须是 {sorted(EXPORT_KINDS)}")
        case = self._get_case_row(case_id)
        redactor = self._redactor(actor)

        def job() -> dict[str, Any]:
            evidence = self.db.list_evidence(case_id)
            for ev in evidence:
                ev["versions"] = self.db.list_evidence_versions(ev["evidence_id"])
            chain = self.db.verify_preservation_chain(case_id)
            if not chain["intact"]:
                raise ConflictError(
                    f"证据保全哈希链在 {chain['broken_at']} 处断裂，禁止导出"
                )
            payload: dict[str, Any] = {
                "export_kind": kind,
                "exported_at": utcnow(),
                "exported_by": {"actor_id": actor.id, "role": actor.role},
                "case_id": case_id,
            }
            if kind == "full":
                payload.update({
                    "case": case,
                    "parties": [redactor.redact_party(p) for p in self.db.list_parties(case_id)],
                    "services": self.db.list_services(case_id),
                    "statements": self.db.list_statements(case_id),
                    "evidence": evidence,
                    "state_events": self.db.list_state_events(case_id),
                    "links": self.db.list_links(case_id),
                    "preservation": self.db.list_preservation(case_id),
                })
            elif kind == "evidence_pack":
                payload.update({
                    "evidence": evidence,
                    "preservation": self.db.list_preservation(case_id),
                })
            elif kind == "timeline":
                tl = self._build_timeline(case_id, redactor)
                payload["timeline"] = tl
            payload["preservation_chain"] = chain
            self._audit(actor, "case.export", "export", "success",
                        resource_id=case_id, case_id=case_id, purpose=purpose,
                        detail={"kind": kind, "chain_length": chain["length"]})
            return payload

        return self._txn(job)

    def _build_timeline(self, case_id: str, redactor: Redactor) -> list[dict[str, Any]]:
        """与 get_timeline 相同的组装逻辑，但不写审计（导出自身已写）。"""
        case = self._get_case_row(case_id)
        items: list[tuple[str, str, dict[str, Any]]] = []
        items.append((case["created_at"], "case_created", {
            "case_id": case_id, "title": case["title"],
            "advertised_service": case["advertised_service"],
            "created_by": case["created_by"],
        }))
        for p in self.db.list_parties(case_id):
            items.append((p["created_at"], "party_added", redactor.redact_party(p)))
        for s in self.db.list_services(case_id):
            items.append((s["created_at"], "service_recorded", s))
        for st in self.db.list_statements(case_id):
            items.append((st["created_at"], "statement_version", st))
        for ev in self.db.list_evidence(case_id):
            versions = self.db.list_evidence_versions(ev["evidence_id"])
            row = dict(ev)
            row["versions"] = versions
            items.append((ev["created_at"], "evidence_received", row))
        for rec in self.db.list_preservation(case_id):
            items.append((rec["created_at"], "preservation", rec))
        for ev in self.db.list_state_events(case_id):
            items.append((ev["created_at"], "state_event", ev))
        for link in self.db.list_links(case_id):
            other = link["case_id_b"] if link["case_id_a"] == case_id else link["case_id_a"]
            items.append((link["created_at"], "case_linked", {
                "link_id": link["link_id"], "related_case_id": other,
                "reason": link["reason"], "created_by": link["created_by"],
            }))
        items.sort(key=lambda x: x[0])
        return [{"at": at, "type": kind, "data": data} for at, kind, data in items]

    # -- 审计查询 ---------------------------------------------------------

    def list_audit(self, actor: Actor, case_id: str | None = None,
                   limit: int = 100, purpose: str | None = None) -> list[dict[str, Any]]:
        self._require("audit.view", actor)
        with self.db.lock:
            if case_id:
                self._get_case_row(case_id)
            rows = self.db.list_audit(case_id=case_id, limit=limit)
            self._audit(actor, "audit.view", "audit", "success",
                        resource_id=case_id, case_id=case_id, purpose=purpose,
                        detail={"returned": len(rows)})
            self.db.commit()
        return rows

    # -- 便捷封装：异常 -> 审计拒绝 ---------------------------------------

    def audited_action(self, actor: Actor, fn: Callable[[], Any], action: str,
                       resource_type: str, case_id: str | None = None) -> Any:
        """供 HTTP 层使用：业务失败也记录 denied 审计。"""
        try:
            return fn()
        except CaseError as exc:
            with self.db.lock:
                self._audit(actor, action, resource_type, "denied", case_id=case_id,
                            detail={"error": str(exc), "error_type": type(exc).__name__})
                self.db.commit()
            raise
