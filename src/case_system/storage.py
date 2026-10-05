"""SQLite 存储层。

设计原则：
- 证据、陈述版本、状态事件、保全记录、审计日志均为只追加表，
  代码路径中不存在对这些表的 UPDATE/DELETE，后补材料只能产生新版本；
- 保全记录按案件形成哈希链，任何篡改都会断链；
- 时间戳统一使用 UTC ISO-8601（尾缀 Z），排序确定。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    complainant_party_id TEXT,
    state TEXT NOT NULL,
    advertised_service TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reopened_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    side TEXT NOT NULL CHECK(side IN ('complainant','respondent')),
    name TEXT,
    phone TEXT,
    id_card TEXT,
    address TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    service_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    name TEXT NOT NULL,
    advertised TEXT,
    actual TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS statements (
    statement_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    version_no INTEGER NOT NULL,
    content TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    supersedes_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, version_no)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source_channel TEXT NOT NULL,
    current_sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    storage_ref TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    linked_statement_id TEXT,
    received_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_versions (
    evidence_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    note TEXT,
    received_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (evidence_id, version_no)
);

CREATE TABLE IF NOT EXISTS preservation_records (
    record_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    evidence_id TEXT REFERENCES evidence(evidence_id),
    action TEXT NOT NULL,
    custodian TEXT NOT NULL,
    detail TEXT,
    evidence_sha256 TEXT,
    prev_hash TEXT NOT NULL DEFAULT '',
    record_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_links (
    link_id TEXT PRIMARY KEY,
    case_id_a TEXT NOT NULL REFERENCES cases(case_id),
    case_id_b TEXT NOT NULL REFERENCES cases(case_id),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id_a, case_id_b)
);

CREATE TABLE IF NOT EXISTS state_events (
    event_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(case_id),
    action TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    case_id TEXT,
    purpose TEXT,
    result TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence(case_id);
CREATE INDEX IF NOT EXISTS idx_statements_case ON statements(case_id);
CREATE INDEX IF NOT EXISTS idx_state_events_case ON state_events(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_links_a ON case_links(case_id_a);
CREATE INDEX IF NOT EXISTS idx_links_b ON case_links(case_id_b);
"""

IMMUTABLE_TABLES = {"evidence", "evidence_versions", "statements", "state_events",
                    "preservation_records", "audit_log", "case_links"}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Storage:
    """封装 SQLite 连接与只追加写入约定。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # HTTP 服务在工作线程中复用同一连接：关闭同线程校验并以锁串行化写入
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- 基础工具 -------------------------------------------------------

    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, tuple(params))
        return [self.row_to_dict(r) for r in cur.fetchall()]  # type: ignore[misc]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        cur = self.conn.execute(sql, tuple(params))
        return self.row_to_dict(cur.fetchone())

    # -- 案件与当事人 ----------------------------------------------------

    def insert_case(self, case: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO cases (case_id, title, complainant_party_id, state, "
            "advertised_service, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (case["case_id"], case["title"], case.get("complainant_party_id"),
             case["state"], case.get("advertised_service"),
             case["created_by"], case["created_at"]),
        )

    def update_case_state(self, case_id: str, state: str) -> None:
        self.conn.execute("UPDATE cases SET state=? WHERE case_id=?", (state, case_id))

    def set_complainant(self, case_id: str, party_id: str) -> None:
        self.conn.execute("UPDATE cases SET complainant_party_id=? WHERE case_id=?",
                          (party_id, case_id))

    def increment_reopened(self, case_id: str) -> None:
        self.conn.execute(
            "UPDATE cases SET reopened_count = reopened_count + 1 WHERE case_id=?",
            (case_id,),
        )

    def insert_party(self, party: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO parties (party_id, case_id, side, name, phone, id_card, "
            "address, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (party["party_id"], party["case_id"], party["side"], party.get("name"),
             party.get("phone"), party.get("id_card"), party.get("address"),
             party["created_at"]),
        )

    def list_parties(self, case_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM parties WHERE case_id=? ORDER BY created_at, party_id",
            (case_id,),
        )

    def insert_service(self, svc: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO services (service_id, case_id, name, advertised, actual, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (svc["service_id"], svc["case_id"], svc["name"], svc.get("advertised"),
             svc.get("actual"), svc["created_at"]),
        )

    def list_services(self, case_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM services WHERE case_id=? ORDER BY created_at, service_id",
            (case_id,),
        )

    # -- 陈述（只追加版本） ----------------------------------------------

    def next_statement_version(self, case_id: str) -> int:
        row = self.query_one(
            "SELECT COALESCE(MAX(version_no),0) AS m FROM statements WHERE case_id=?",
            (case_id,),
        )
        return int(row["m"]) + 1  # type: ignore[index]

    def insert_statement(self, st: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO statements (statement_id, case_id, version_no, content, "
            "source_channel, submitted_by, supersedes_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (st["statement_id"], st["case_id"], st["version_no"], st["content"],
             st["source_channel"], st["submitted_by"], st.get("supersedes_id"),
             st["created_at"]),
        )

    def list_statements(self, case_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM statements WHERE case_id=? ORDER BY version_no", (case_id,),
        )

    # -- 证据（只追加，后补材料生成新版本） --------------------------------

    def insert_evidence(self, ev: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO evidence (evidence_id, case_id, kind, title, source_channel, "
            "current_sha256, byte_size, storage_ref, metadata_json, linked_statement_id, "
            "received_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (ev["evidence_id"], ev["case_id"], ev["kind"], ev["title"],
             ev["source_channel"], ev["current_sha256"], ev["byte_size"],
             ev.get("storage_ref"), json.dumps(ev.get("metadata", {}), ensure_ascii=False),
             ev.get("linked_statement_id"), ev["received_by"], ev["created_at"]),
        )
        self.conn.execute(
            "INSERT INTO evidence_versions (evidence_id, version_no, sha256, byte_size, "
            "note, received_by, created_at) VALUES (?,1,?,?,?,?,?)",
            (ev["evidence_id"], ev["current_sha256"], ev["byte_size"], "初次接收",
             ev["received_by"], ev["created_at"]),
        )

    def next_evidence_version(self, evidence_id: str) -> int:
        row = self.query_one(
            "SELECT COALESCE(MAX(version_no),0) AS m FROM evidence_versions WHERE evidence_id=?",
            (evidence_id,),
        )
        return int(row["m"]) + 1  # type: ignore[index]

    def append_evidence_version(self, evidence_id: str, version_no: int,
                                sha256: str, byte_size: int, note: str,
                                received_by: str, created_at: str) -> None:
        """登记新版本：旧版本保留在 evidence_versions，主表指针更新到最新版。"""
        self.conn.execute(
            "INSERT INTO evidence_versions (evidence_id, version_no, sha256, byte_size, "
            "note, received_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (evidence_id, version_no, sha256, byte_size, note, received_by, created_at),
        )
        self.conn.execute(
            "UPDATE evidence SET current_sha256=?, byte_size=? WHERE evidence_id=?",
            (sha256, byte_size, evidence_id),
        )

    def list_evidence(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM evidence WHERE case_id=? ORDER BY created_at, evidence_id",
            (case_id,),
        )
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json") or "{}")
        return rows

    def list_evidence_versions(self, evidence_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM evidence_versions WHERE evidence_id=? ORDER BY version_no",
            (evidence_id,),
        )

    # -- 证据保全（哈希链） -----------------------------------------------

    def latest_preservation_hash(self, case_id: str) -> str:
        row = self.query_one(
            "SELECT record_hash FROM preservation_records WHERE case_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (case_id,),
        )
        return row["record_hash"] if row else ""

    def insert_preservation(self, rec: dict[str, Any]) -> None:
        payload = {
            "record_id": rec["record_id"],
            "case_id": rec["case_id"],
            "evidence_id": rec.get("evidence_id"),
            "action": rec["action"],
            "custodian": rec["custodian"],
            "detail": rec.get("detail"),
            "evidence_sha256": rec.get("evidence_sha256"),
            "prev_hash": rec["prev_hash"],
            "created_at": rec["created_at"],
        }
        record_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        self.conn.execute(
            "INSERT INTO preservation_records (record_id, case_id, evidence_id, action, "
            "custodian, detail, evidence_sha256, prev_hash, record_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (payload["record_id"], payload["case_id"], payload["evidence_id"],
             payload["action"], payload["custodian"], payload["detail"],
             payload["evidence_sha256"], payload["prev_hash"], record_hash,
             payload["created_at"]),
        )
        rec["record_hash"] = record_hash

    def verify_preservation_chain(self, case_id: str) -> dict[str, Any]:
        """重算全链，返回链长与是否完整。"""
        rows = self.query(
            "SELECT * FROM preservation_records WHERE case_id=? "
            "ORDER BY created_at, rowid",
            (case_id,),
        )
        prev = ""
        for i, row in enumerate(rows):
            if row["prev_hash"] != prev:
                return {"intact": False, "length": len(rows),
                        "broken_at": row["record_id"], "index": i}
            payload = {
                "record_id": row["record_id"],
                "case_id": row["case_id"],
                "evidence_id": row["evidence_id"],
                "action": row["action"],
                "custodian": row["custodian"],
                "detail": row["detail"],
                "evidence_sha256": row["evidence_sha256"],
                "prev_hash": row["prev_hash"],
                "created_at": row["created_at"],
            }
            digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
            if digest != row["record_hash"]:
                return {"intact": False, "length": len(rows),
                        "broken_at": row["record_id"], "index": i}
            prev = row["record_hash"]
        return {"intact": True, "length": len(rows), "broken_at": None, "index": None}

    def list_preservation(self, case_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM preservation_records WHERE case_id=? ORDER BY created_at, rowid",
            (case_id,),
        )

    # -- 重复投诉关联 -----------------------------------------------------

    def insert_link(self, link: dict[str, Any]) -> None:
        a, b = sorted((link["case_id_a"], link["case_id_b"]))
        self.conn.execute(
            "INSERT INTO case_links (link_id, case_id_a, case_id_b, reason, created_by, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (link["link_id"], a, b, link["reason"], link["created_by"], link["created_at"]),
        )

    def list_links(self, case_id: str) -> list[dict[str, Any]]:
        return self.query(
            "SELECT * FROM case_links WHERE case_id_a=? OR case_id_b=? "
            "ORDER BY created_at, link_id",
            (case_id, case_id),
        )

    # -- 状态事件（只追加） -----------------------------------------------

    def insert_state_event(self, ev: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO state_events (event_id, case_id, action, from_state, to_state, "
            "actor_id, actor_role, detail_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ev["event_id"], ev["case_id"], ev["action"], ev.get("from_state"),
             ev["to_state"], ev["actor_id"], ev["actor_role"],
             json.dumps(ev.get("detail", {}), ensure_ascii=False), ev["created_at"]),
        )

    def list_state_events(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM state_events WHERE case_id=? ORDER BY created_at, rowid",
            (case_id,),
        )
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return rows

    # -- 审计日志 ---------------------------------------------------------

    def insert_audit(self, entry: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (audit_id, actor_id, actor_role, action, resource_type, "
            "resource_id, case_id, purpose, result, detail_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (entry["audit_id"], entry["actor_id"], entry["actor_role"], entry["action"],
             entry["resource_type"], entry.get("resource_id"), entry.get("case_id"),
             entry.get("purpose"), entry["result"],
             json.dumps(entry.get("detail", {}), ensure_ascii=False), entry["created_at"]),
        )

    def list_audit(self, case_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if case_id:
            rows = self.query(
                "SELECT * FROM audit_log WHERE case_id=? ORDER BY created_at DESC, rowid DESC "
                "LIMIT ?",
                (case_id, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM audit_log ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        for row in rows:
            row["detail"] = json.loads(row.pop("detail_json") or "{}")
        return rows

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
