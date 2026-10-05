"""领域模型：角色、状态、动作与状态机。

与 domain/contract.json 保持一致。状态机在代码中固化为表，
service 层的每次流转都必须通过 ``TransitionRule.allowed`` 校验，
任何绕过状态机的写操作都会被数据库触发器拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet

# 办案角色（非当事人）
ROLE_COMPLIANCE = "机构合规员"
ROLE_PRACTITIONER = "执业人员"
ROLE_REGULATOR = "监管人员"
ROLE_REVIEWER = "复核专家"
ROLE_PARTY = "当事人"

CASE_ROLES: FrozenSet[str] = frozenset(
    {ROLE_COMPLIANCE, ROLE_PRACTITIONER, ROLE_REGULATOR, ROLE_REVIEWER}
)
ALL_ROLES: FrozenSet[str] = CASE_ROLES | {ROLE_PARTY}
ROLES = tuple(sorted(ALL_ROLES))

# 接口层可用的稳定角色编码（HTTP 头不能直接放中文，也可百分号编码中文）
ROLE_CODES = {
    "compliance": ROLE_COMPLIANCE,
    "practitioner": ROLE_PRACTITIONER,
    "regulator": ROLE_REGULATOR,
    "reviewer": ROLE_REVIEWER,
    "party": ROLE_PARTY,
}


def resolve_role(value: str) -> str:
    """把角色编码/中文角色名解析为中文角色名。"""
    if value in ROLE_CODES:
        return ROLE_CODES[value]
    if value in ALL_ROLES:
        return value
    raise ValueError(f"未知角色：{value}")

# 案件状态
ST_CREATED = "登记"
ST_PENDING = "待核验"
ST_HANDLING = "处置中"
ST_MEDIATING = "调解中"
ST_MEDIATED = "已调解"
ST_DECIDED = "已决定"
ST_ENFORCEMENT = "已转执法"
ST_WITHDRAWN = "已撤回"
ST_ARCHIVED = "已归档"

STATES = (
    ST_CREATED,
    ST_PENDING,
    ST_HANDLING,
    ST_MEDIATING,
    ST_MEDIATED,
    ST_DECIDED,
    ST_ENFORCEMENT,
    ST_WITHDRAWN,
    ST_ARCHIVED,
)

# 动作
ACT_SUBMIT = "提交核验"
ACT_ACCEPT = "核验通过"
ACT_RETURN = "退回补正"
ACT_MEDIATE_START = "启动调解"
ACT_MEDIATE_SUCCESS = "调解成立"
ACT_MEDIATE_FAIL = "调解不成立"
ACT_ENFORCE = "转执法"
ACT_DECIDE = "作出决定"
ACT_WITHDRAW = "撤回"
ACT_REOPEN = "复开"
ACT_ARCHIVE = "归档"

ACTIONS = (
    ACT_SUBMIT,
    ACT_ACCEPT,
    ACT_RETURN,
    ACT_MEDIATE_START,
    ACT_MEDIATE_SUCCESS,
    ACT_MEDIATE_FAIL,
    ACT_ENFORCE,
    ACT_DECIDE,
    ACT_WITHDRAW,
    ACT_REOPEN,
    ACT_ARCHIVE,
)

# 当事人身份（用于数据裁剪）
PARTY_COMPLAINANT = "complainant"
PARTY_RESPONDENT = "respondent"


@dataclass(frozen=True)
class TransitionRule:
    """一条状态流转约束：动作 + 源状态 + 允许执行的办案角色。"""

    action: str
    dest: str
    sources: FrozenSet[str]
    roles: FrozenSet[str]

    def allowed(self, current: str, role: str) -> bool:
        return current in self.sources and role in self.roles


# 状态约束矩阵。撤回后只能复开；已归档不可直接修改，需先复开；
# 调解必须先进入“调解中”，调解不成立回到处置中；
# 转执法后进入终态通道；复开只能由监管人员/复核专家执行。
TRANSITIONS: tuple[TransitionRule, ...] = (
    TransitionRule(ACT_SUBMIT, ST_PENDING, frozenset({ST_CREATED}), frozenset({ROLE_COMPLIANCE, ROLE_PARTY})),
    TransitionRule(ACT_ACCEPT, ST_HANDLING, frozenset({ST_PENDING}), frozenset({ROLE_COMPLIANCE, ROLE_REGULATOR})),
    TransitionRule(ACT_RETURN, ST_CREATED, frozenset({ST_PENDING}), frozenset({ROLE_COMPLIANCE, ROLE_REVIEWER})),
    TransitionRule(ACT_MEDIATE_START, ST_MEDIATING, frozenset({ST_HANDLING}), frozenset({ROLE_COMPLIANCE, ROLE_PRACTITIONER})),
    TransitionRule(ACT_MEDIATE_SUCCESS, ST_MEDIATED, frozenset({ST_MEDIATING}), frozenset({ROLE_COMPLIANCE, ROLE_PRACTITIONER})),
    TransitionRule(ACT_MEDIATE_FAIL, ST_HANDLING, frozenset({ST_MEDIATING}), frozenset({ROLE_COMPLIANCE, ROLE_PRACTITIONER})),
    TransitionRule(ACT_ENFORCE, ST_ENFORCEMENT, frozenset({ST_HANDLING, ST_MEDIATING}), frozenset({ROLE_REGULATOR})),
    TransitionRule(ACT_DECIDE, ST_DECIDED, frozenset({ST_HANDLING, ST_ENFORCEMENT}), frozenset({ROLE_REGULATOR, ROLE_REVIEWER})),
    # 投诉人或机构合规员可在实质处置终结前撤回；调解成立/已决定后不可撤回
    TransitionRule(
        ACT_WITHDRAW,
        ST_WITHDRAWN,
        frozenset({ST_CREATED, ST_PENDING, ST_HANDLING, ST_MEDIATING}),
        frozenset({ROLE_COMPLIANCE, ROLE_PARTY}),
    ),
    TransitionRule(ACT_REOPEN, ST_HANDLING, frozenset({ST_WITHDRAWN, ST_ARCHIVED, ST_DECIDED, ST_MEDIATED}), frozenset({ROLE_REGULATOR, ROLE_REVIEWER})),
    TransitionRule(
        ACT_ARCHIVE,
        ST_ARCHIVED,
        frozenset({ST_MEDIATED, ST_DECIDED, ST_ENFORCEMENT, ST_WITHDRAWN}),
        frozenset({ROLE_COMPLIANCE, ROLE_REVIEWER}),
    ),
)

TRANSITION_BY_ACTION = {rule.action: rule for rule in TRANSITIONS}

# 证据材料来源渠道
EVIDENCE_CHANNELS = ("聊天截图", "知情同意", "收费记录", "合同协议", "其他")

# 敏感字段标记：案件与当事人对象中这些字段按角色/当事人身份裁剪
SENSITIVE_CASE_FIELDS: FrozenSet[str] = frozenset(
    {"respondent_name", "respondent_phone", "respondent_id_no", "respondent_contact", "internal_note"}
)
SENSITIVE_PARTY_FIELDS: FrozenSet[str] = frozenset({"phone", "id_no", "address", "contact"})


def find_rule(action: str) -> TransitionRule:
    rule = TRANSITION_BY_ACTION.get(action)
    if rule is None:
        raise ValueError(f"未知动作：{action}")
    return rule
