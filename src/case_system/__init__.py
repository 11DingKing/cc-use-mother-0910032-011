"""消费者投诉证据链案件系统。

模块划分：
- models：角色、状态、动作与状态机约束（与 domain/contract.json 对齐）
- database：SQLite 只增表结构与防篡改触发器
- service：案件业务逻辑、证据保全哈希链、审计链、敏感字段裁剪
- api：零依赖 JSON HTTP 接口
"""
from .errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from .models import ROLES, STATES, ACTIONS, TRANSITIONS
from .service import CaseSystem
from .projection import mask_name, mask_phone, mask_id

__all__ = [
    "CaseSystem",
    "ROLES",
    "STATES",
    "ACTIONS",
    "TRANSITIONS",
    "ValidationError",
    "NotFoundError",
    "PermissionDeniedError",
    "ConflictError",
    "mask_name",
    "mask_phone",
    "mask_id",
]
