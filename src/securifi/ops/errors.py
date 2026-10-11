"""Errors raised by operations. Each maps to a stable code and HTTP status across all entry points."""

from __future__ import annotations

from typing import Any


class OpError(Exception):
    code = "error"
    status = 400
    outcome = "failed"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


class Unauthenticated(OpError):
    code, status, outcome = "unauthenticated", 401, "denied"


class PermissionDenied(OpError):
    code, status, outcome = "permission_denied", 403, "denied"


class NotFound(OpError):
    code, status = "not_found", 404


class ValidationFailed(OpError):
    code, status, outcome = "validation_failed", 422, "invalid"


class Conflict(OpError):
    code, status = "conflict", 409


class BusinessRuleViolation(OpError):
    """The request is well-formed but breaches a facility rule (e.g. borrowing base, four-eyes)."""

    code, status = "business_rule_violation", 422
