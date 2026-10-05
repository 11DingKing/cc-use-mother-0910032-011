"""敏感字段裁剪。

裁剪依据两个维度：
1. 办案角色（监管人员、执业人员、机构合规员、复核专家）可见的敏感等级；
2. 当事人归属：当事人只能看到自己的完整敏感信息，对方当事人按最低等级呈现。

任何被裁剪的字段不会直接删除，而是置空并登记到 ``_redacted`` 列表，
保证接口调用方知道“这里本来有字段、但按权限被裁掉了”。
"""
from __future__ import annotations

from typing import Any

# 敏感字段 -> 敏感等级
LEVEL_NAME = "name"
LEVEL_CONTACT = "contact"
LEVEL_ID_NUMBER = "id_number"

SENSITIVE_FIELDS: dict[str, str] = {
    "name": LEVEL_NAME,
    "phone": LEVEL_CONTACT,
    "id_card": LEVEL_ID_NUMBER,
    "address": LEVEL_CONTACT,
}

# 办案角色可见的敏感等级（当事人角色走归属规则，不在此表）
ROLE_LEVELS: dict[str, set[str]] = {
    "监管人员": {LEVEL_NAME, LEVEL_CONTACT, LEVEL_ID_NUMBER},
    "执业人员": {LEVEL_NAME, LEVEL_CONTACT},
    "机构合规员": {LEVEL_NAME},
    "复核专家": {LEVEL_NAME},
}

# 非本方当事人时，当事人角色能看到对方的等级
COUNTERPARTY_LEVELS = {LEVEL_NAME}


def _mask(level: str, value: str) -> str:
    if value is None:
        return value
    text = str(value)
    if level == LEVEL_NAME:
        return text[0] + "**" if text else "**"
    if level == LEVEL_CONTACT:
        if len(text) <= 6:
            return "***"
        return f"{text[:3]}****{text[-2:]}"
    if level == LEVEL_ID_NUMBER:
        if len(text) <= 4:
            return "****"
        return f"{text[0]}{'*' * (len(text) - 2)}{text[-1]}"
    return "***"


class Redactor:
    """按请求人身份构造的字段裁剪器。"""

    def __init__(self, role: str, viewer_party_id: str | None = None) -> None:
        self.role = role
        self.viewer_party_id = viewer_party_id

    def _level_allowed(self, level: str, owner_party_id: str | None) -> bool:
        if self.role == "当事人":
            if not self.viewer_party_id:
                return False
            if owner_party_id == self.viewer_party_id:
                return True
            return level in COUNTERPARTY_LEVELS
        return level in ROLE_LEVELS.get(self.role, set())

    def redact_party(self, party: dict[str, Any]) -> dict[str, Any]:
        """裁剪一条当事人记录，返回副本并附 ``_redacted`` 清单。"""
        out = dict(party)
        redacted: list[str] = []
        owner = party.get("party_id")
        for field, level in SENSITIVE_FIELDS.items():
            if field not in out or out[field] is None:
                continue
            if self._level_allowed(level, owner):
                continue
            out[field] = _mask(level, out[field]) if level == LEVEL_NAME else None
            redacted.append(field)
        if redacted:
            out["_redacted"] = redacted
        return out

    def redact_parties(self, parties: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.redact_party(p) for p in parties]
