"""领域错误类型。"""
from __future__ import annotations


class CaseSystemError(Exception):
    """案件系统基础错误。"""


class ValidationError(CaseSystemError):
    """输入不满足领域约束（400）。"""


class NotFoundError(CaseSystemError):
    """对象不存在（404）。"""


class PermissionDeniedError(CaseSystemError):
    """角色或当事人身份无权执行（403）。"""


class ConflictError(CaseSystemError):
    """当前案件状态不允许该动作（409）。"""
