"""案件系统的领域错误类型。"""
from __future__ import annotations


class CaseError(Exception):
    """业务错误基类，映射到 HTTP 400。"""

    status = 400


class ValidationError(CaseError):
    """请求内容不满足领域约束。"""

    status = 400


class NotFoundError(CaseError):
    """资源不存在。"""

    status = 404


class ConflictError(CaseError):
    """与当前案件状态或不可变记录冲突。"""

    status = 409


class PermissionError(CaseError):  # noqa: A001 - 领域内有意遮蔽内置名
    """当前角色无权执行该操作。"""

    status = 403
