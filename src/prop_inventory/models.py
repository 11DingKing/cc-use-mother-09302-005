"""领域常量与业务错误。

状态与角色对齐 ``domain/contract.json`` 的 ``states`` / ``actors``，
并在契约五个履约状态之外扩展隔离、丢失、报废三个仓储状态。
"""
from __future__ import annotations

# ---- 单件物资状态（对齐契约 states，后三个为仓储扩展） ----
STOCK = "备货"          # 在库可预约
RESERVED = "占用"       # 已被场次锁定
SHIPPED = "出库"        # 已出库在途
IN_USE = "使用"         # 学校已签收，现场使用
RETURNED = "归还"       # 已归还入库（可再次预约）
QUARANTINED = "隔离"    # 损坏隔离，不可用
LOST = "丢失"           # 归还差异确认为缺失
SCRAPPED = "报废"       # 隔离后判定不可修复

ITEM_STATUSES = (STOCK, RESERVED, SHIPPED, IN_USE, RETURNED, QUARANTINED, LOST, SCRAPPED)

#: 可参与预约锁定的状态
AVAILABLE_STATUSES = (STOCK, RETURNED)
#: 仍绑定场次、尚未清算的状态
OPEN_BINDING_STATUSES = (RESERVED, SHIPPED, IN_USE)
#: 不可再投入使用的状态
TERMINAL_STATUSES = (QUARANTINED, LOST, SCRAPPED)

#: 状态对应的物理位置，回答“东西究竟在哪”
ITEM_LOCATIONS = {
    STOCK: "仓库",
    RETURNED: "仓库",
    RESERVED: "仓库(已锁定)",
    SHIPPED: "在途",
    IN_USE: "到校",
    QUARANTINED: "隔离区",
    LOST: "去向不明",
    SCRAPPED: "已报废",
}

# ---- 角色（对齐契约 actors） ----
ROLE_ADMIN = "器材管理员"
ROLE_TEACHER = "活动老师"
ROLE_SIGNER = "学校签收人"
ACTORS = (ROLE_ADMIN, ROLE_TEACHER, ROLE_SIGNER)

# ---- 物资状况 ----
CONDITION_GOOD = "完好"
CONDITION_DAMAGED = "损坏"
CONDITIONS = (CONDITION_GOOD, CONDITION_DAMAGED)


class DomainError(Exception):
    """业务规则错误，携带 HTTP 状态码与机器可读码。"""

    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def bad_request(code: str, message: str, details: dict | None = None) -> DomainError:
    return DomainError(code, message, 400, details)


def not_found(what: str, ident: object) -> DomainError:
    return DomainError("not_found", f"{what}不存在：{ident}", 404, {"entity": what, "id": ident})


def conflict(code: str, message: str, details: dict | None = None) -> DomainError:
    return DomainError(code, message, 409, details)


def unprocessable(code: str, message: str, details: dict | None = None) -> DomainError:
    return DomainError(code, message, 422, details)
