"""SQLite 持久化层。

设计原则：
1. 证据、陈述版本、保全记录、案件事件、审计日志均为只增表，
   触发器在数据库层面拒绝 UPDATE/DELETE，防止后补材料覆盖原证据；
2. 案件主表仅允许状态等少量字段更新；
3. 保全记录按案件构成哈希链，审计日志构成全局哈希链，
   任一记录被底层篡改都会在校验时断链。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS parties (
    party_id      TEXT PRIMARY KEY,
    side          TEXT NOT NULL CHECK (side IN ('complainant', 'respondent')),
    name          TEXT NOT NULL,
    phone         TEXT,
    id_no         TEXT,
    address       TEXT,
    contact       TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id            TEXT PRIMARY KEY,
    case_no            TEXT NOT NULL UNIQUE,
    complainant_id     TEXT NOT NULL REFERENCES parties(party_id),
    respondent_id      TEXT REFERENCES parties(party_id),
    service_name       TEXT NOT NULL,
    claim              TEXT NOT NULL,
    internal_note      TEXT,
    state              TEXT NOT NULL,
    created_by_role    TEXT NOT NULL,
    created_by_party   TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_complainant ON cases(complainant_id);
CREATE INDEX IF NOT EXISTS idx_cases_respondent ON cases(respondent_id);
CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);

-- 重复投诉关联：每个关联存两个方向的行，服务层保证对称
CREATE TABLE IF NOT EXISTS case_links (
    case_id        TEXT NOT NULL REFERENCES cases(case_id),
    linked_case_id TEXT NOT NULL REFERENCES cases(case_id),
    reason         TEXT NOT NULL,
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (case_id, linked_case_id),
    CHECK (case_id <> linked_case_id)
);

-- 证据材料（聊天截图、知情同意、收费记录……）只增不改
CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_id       TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL REFERENCES cases(case_id),
    channel           TEXT NOT NULL,
    title             TEXT NOT NULL,
    file_name         TEXT,
    file_sha256       TEXT NOT NULL,
    file_size         INTEGER NOT NULL DEFAULT 0,
    content_text      TEXT,
    summary           TEXT,
    submitted_by_role TEXT NOT NULL,
    submitted_by_party TEXT,
    supersedes_id     TEXT REFERENCES evidence_items(evidence_id),
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_case ON evidence_items(case_id, created_at);

-- 陈述版本：同一案件版本号单调递增，永不修改
CREATE TABLE IF NOT EXISTS statements (
    statement_id       TEXT PRIMARY KEY,
    case_id            TEXT NOT NULL REFERENCES cases(case_id),
    version_no         INTEGER NOT NULL,
    content            TEXT NOT NULL,
    change_note        TEXT,
    submitted_by_role  TEXT NOT NULL,
    submitted_by_party TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE (case_id, version_no)
);

-- 证据保全记录：每次固化生成全量快照并串联哈希
CREATE TABLE IF NOT EXISTS preservations (
    preservation_id TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(case_id),
    seq             INTEGER NOT NULL,
    state_locked    TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    record_hash     TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (case_id, seq)
);

-- 案件时间线事件：只增
CREATE TABLE IF NOT EXISTS case_events (
    event_id     TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases(case_id),
    seq          INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    actor_role   TEXT NOT NULL,
    actor_party  TEXT,
    detail_json  TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (case_id, seq)
);

-- 导出登记：每次导出固化载荷指纹与裁剪方案
CREATE TABLE IF NOT EXISTS export_records (
    export_id        TEXT PRIMARY KEY,
    case_id          TEXT NOT NULL REFERENCES cases(case_id),
    actor_role       TEXT NOT NULL,
    actor_party      TEXT,
    reason           TEXT,
    redaction_view   TEXT NOT NULL,
    payload_sha256   TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- 全局审计链：查看、导出、越权拒绝全部留痕，只增
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id    TEXT PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    actor_role  TEXT NOT NULL,
    actor_party TEXT,
    action      TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id   TEXT,
    result      TEXT NOT NULL CHECK (result IN ('allowed', 'denied')),
    detail_json TEXT,
    client_ip   TEXT,
    prev_hash   TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log(object_type, object_id, seq);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at, seq);
"""

APPEND_ONLY_TABLES = (
    "evidence_items",
    "statements",
    "preservations",
    "case_events",
    "export_records",
    "audit_log",
    "case_links",
)

_TRIGGER_TEMPLATE = """
CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为只增表，禁止更新（证据不可覆盖）');
END;
CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
BEGIN
    SELECT RAISE(ABORT, '{table} 为只增表，禁止删除（证据不可覆盖）');
END;
"""

GENESIS_HASH = "0" * 64


class Database:
    """持有单一 SQLite 连接并串行化写操作。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            for table in APPEND_ONLY_TABLES:
                self._conn.executescript(_TRIGGER_TEMPLATE.format(table=table))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
