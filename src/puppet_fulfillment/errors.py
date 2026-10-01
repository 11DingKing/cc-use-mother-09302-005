"""领域错误：携带稳定错误码与 HTTP 状态，供服务层与接口层共用。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务错误的基类。"""

    code = "domain_error"
    status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        body = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class ValidationError(DomainError):
    code = "validation_error"
    status = 400


class NotFound(DomainError):
    code = "not_found"
    status = 404


class PermissionDenied(DomainError):
    code = "permission_denied"
    status = 403


class Conflict(DomainError):
    code = "conflict"
    status = 409


class InsufficientStock(Conflict):
    """场次确认时无法一次性锁定足量库存。"""

    code = "insufficient_stock"


class IllegalTransition(Conflict):
    """物资当前状态不允许目标动作。"""

    code = "illegal_transition"


class DuplicateReceipt(Conflict):
    """同一出库单重复签收。"""

    code = "duplicate_receipt"
