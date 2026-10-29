"""Role based access control enforced in the backend (not only in the UI)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set

from backend.core.errors import PermissionDeniedError
from backend.models.enums import Role

# Permission -> roles allowed. Kept explicit and auditable.
PERMISSIONS: Dict[str, Set[Role]] = {
    "patient:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.RECEPTIONIST, Role.BILLING, Role.LAB, Role.PHARMACIST, Role.PATIENT},
    "patient:write": {Role.ADMIN, Role.RECEPTIONIST},
    "doctor:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.RECEPTIONIST, Role.PATIENT, Role.BILLING},
    "doctor:write": {Role.ADMIN},
    "appointment:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.RECEPTIONIST, Role.PATIENT},
    "appointment:write": {Role.ADMIN, Role.RECEPTIONIST, Role.DOCTOR},
    "encounter:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE},
    "encounter:write": {Role.ADMIN, Role.DOCTOR},
    "bed:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.RECEPTIONIST},
    "bed:write": {Role.ADMIN, Role.NURSE},
    "admission:write": {Role.ADMIN, Role.NURSE, Role.DOCTOR},
    "discharge:request": {Role.ADMIN, Role.DOCTOR, Role.NURSE},
    "lab:read": {Role.ADMIN, Role.DOCTOR, Role.NURSE, Role.LAB},
    "lab:write": {Role.ADMIN, Role.LAB, Role.DOCTOR},
    "pharmacy:read": {Role.ADMIN, Role.PHARMACIST, Role.DOCTOR, Role.NURSE},
    "pharmacy:write": {Role.ADMIN, Role.PHARMACIST, Role.DOCTOR},
    "billing:read": {Role.ADMIN, Role.BILLING, Role.RECEPTIONIST, Role.PATIENT},
    "billing:write": {Role.ADMIN, Role.BILLING},
    "insurance:read": {Role.ADMIN, Role.BILLING, Role.PATIENT},
    "insurance:write": {Role.ADMIN, Role.BILLING},
    "faq:ask": set(Role),
    "faq:ingest": {Role.ADMIN, Role.RECEPTIONIST},
    "faq:delete": {Role.ADMIN},
    "approval:read": {Role.ADMIN, Role.DOCTOR, Role.BILLING, Role.PHARMACIST, Role.NURSE},
    "approval:decide": {Role.ADMIN, Role.DOCTOR, Role.BILLING, Role.PHARMACIST},
    "audit:read": {Role.ADMIN},
    "report:read": {Role.ADMIN, Role.BILLING, Role.DOCTOR},
    "admin:config": {Role.ADMIN},
}

# Which roles may sign off on each high-impact action (narrow HITL scope).
APPROVER_ROLES: Dict[str, Set[Role]] = {
    "discharge": {Role.DOCTOR, Role.ADMIN},
    "claim_decision_support": {Role.BILLING, Role.ADMIN},
    "pharmacy_dispense": {Role.PHARMACIST, Role.ADMIN},
}


@dataclass(frozen=True)
class CurrentUser:
    username: str
    role: Role
    patient_id: Optional[int] = None

    def has(self, permission: str) -> bool:
        return self.role in PERMISSIONS.get(permission, set())

    def require(self, permission: str) -> None:
        if not self.has(permission):
            raise PermissionDeniedError(
                f"Role '{self.role.value}' is not permitted to perform '{permission}'."
            )

    def can_approve(self, action: str) -> bool:
        return self.role in APPROVER_ROLES.get(action, set())

    def require_approver(self, action: str) -> None:
        if not self.can_approve(action):
            raise PermissionDeniedError(
                f"Role '{self.role.value}' cannot approve '{action}'."
            )


def enforce_patient_scope(user: CurrentUser, patient_id: int) -> None:
    """Patients may only touch their own record (minimum necessary data)."""
    if user.role is Role.PATIENT and user.patient_id != patient_id:
        raise PermissionDeniedError("Patients may only access their own records.")


def allowed_permissions(user: CurrentUser) -> Iterable[str]:
    return sorted(name for name, roles in PERMISSIONS.items() if user.role in roles)

