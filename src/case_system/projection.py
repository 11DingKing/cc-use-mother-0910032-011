"""敏感字段裁剪。

案件查看/导出时根据“办案角色 + 当事人身份（投诉人/被投诉人）”投影：
- 当事人只能看自己的完整信息，对方信息脱敏，且看不到内部备注；
- 执业人员不展示当事人完整证件号；
- 机构合规员/监管人员/复核专家按职责可见完整信息（访问留审计）。
"""
from __future__ import annotations

from typing import Any, Optional

from .models import (
    PARTY_COMPLAINANT,
    ROLE_PARTY,
    ROLE_PRACTITIONER,
    SENSITIVE_CASE_FIELDS,
    SENSITIVE_PARTY_FIELDS,
)


def mask_name(value: Optional[str]) -> Optional[str]:
    """张某某 / 李某 / ABC 企业 -> 张* / 李* / A*。"""
    if value is None:
        return None
    value = str(value)
    if not value:
        return value
    if len(value) == 1:
        return value + "*"
    return value[0] + "*" * (len(value) - 1)


def mask_phone(value: Optional[str]) -> Optional[str]:
    """13812345678 -> 138****5678。"""
    if value is None:
        return None
    digits = str(value)
    if len(digits) < 7:
        return "***"
    keep_tail = 4
    keep_head = min(3, len(digits) - keep_tail)
    return digits[:keep_head] + "*" * (len(digits) - keep_head - keep_tail) + digits[-keep_tail:]


def mask_id(value: Optional[str]) -> Optional[str]:
    """身份证/统一社会信用代码仅保留前 3 后 2。"""
    if value is None:
        return None
    value = str(value)
    if len(value) <= 5:
        return "***"
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


_MASKERS = {
    "name": mask_name,
    "respondent_name": mask_name,
    "phone": mask_phone,
    "respondent_phone": mask_phone,
    "contact": mask_name,
    "respondent_contact": mask_name,
    "id_no": mask_id,
    "respondent_id_no": mask_id,
    "address": mask_name,
}


def viewer_key(role: str, party_side: Optional[str]) -> str:
    """统一的查看者标识。"""
    if role == ROLE_PARTY:
        return f"{ROLE_PARTY}:{party_side or 'unknown'}"
    return role


def project_case(
    case: dict[str, Any],
    role: str,
    party_side: Optional[str] = None,
) -> dict[str, Any]:
    """按查看者身份裁剪案件字段。

    ``party_side`` 仅在 role == 当事人 时有意义，取值
    ``complainant`` / ``respondent``。
    """
    result = dict(case)

    if role == ROLE_PARTY:
        # 当事人永远看不到内部备注
        result["internal_note"] = None
        if party_side == PARTY_COMPLAINANT:
            # 投诉人看被投诉人信息需脱敏
            for field in ("respondent_name", "respondent_phone", "respondent_id_no", "respondent_contact"):
                if result.get(field) is not None:
                    result[field] = _MASKERS[field](result[field])
        elif party_side == "respondent":
            # 被投诉人看投诉人个人信息需脱敏
            if result.get("complainant_name") is not None:
                result["complainant_name"] = mask_name(result["complainant_name"])
            for src, dst in (("complainant_phone", "phone"), ("complainant_id_no", "id_no"), ("complainant_address", "address")):
                if result.get(src) is not None:
                    result[src] = _MASKERS[dst](result[src])
        result["_redacted_reason"] = f"当事人视图（{party_side or '未知'}）"
    elif role == ROLE_PRACTITIONER:
        # 执业人员参与调解，但不展示完整证件号
        for field in ("respondent_id_no", "complainant_id_no"):
            if result.get(field) is not None:
                result[field] = mask_id(result[field])
        result["internal_note"] = None
        result["_redacted_reason"] = "执业人员视图：证件号与内部备注已裁剪"
    else:
        # 机构合规员、监管人员、复核专家可见完整字段，由审计记录访问
        result["_redacted_reason"] = None
    return result


def project_party(party: dict[str, Any], role: str, party_side: Optional[str], viewer_side: Optional[str]) -> dict[str, Any]:
    """裁剪独立的当事人记录。当事人不能查看对方的完整敏感信息。"""
    result = dict(party)
    if role == ROLE_PARTY:
        if viewer_side != party_side:
            for field in SENSITIVE_PARTY_FIELDS:
                if result.get(field) is not None and field in _MASKERS:
                    result[field] = _MASKERS[field](result[field])
    elif role == ROLE_PRACTITIONER:
        if result.get("id_no") is not None:
            result["id_no"] = mask_id(result["id_no"])
    return result
