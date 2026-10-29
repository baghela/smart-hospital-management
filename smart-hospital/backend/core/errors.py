"""Shared error types returning consistent API responses."""
from __future__ import annotations


class HospitalError(Exception):
    """Base domain error."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFoundError(HospitalError):
    status_code = 404
    code = "not_found"


class ValidationError(HospitalError):
    status_code = 422
    code = "validation_error"


class ConflictError(HospitalError):
    """Raised on double booking of an appointment slot or a bed."""

    status_code = 409
    code = "conflict"


class PermissionDeniedError(HospitalError):
    status_code = 403
    code = "permission_denied"


class AuthenticationError(HospitalError):
    status_code = 401
    code = "unauthenticated"


class GuardrailError(HospitalError):
    status_code = 400
    code = "guardrail_blocked"


class ApprovalRequiredError(HospitalError):
    """High-impact action requires authorised human review."""

    status_code = 202
    code = "approval_required"

    def __init__(self, message: str, approval_id: int | None = None) -> None:
        super().__init__(message)
        self.approval_id = approval_id


class ProviderError(HospitalError):
    status_code = 503
    code = "provider_unavailable"

