"""Human-in-the-loop approval workflow (narrow scope).

Only three high-impact actions are gated: discharge, claim decision support and
pharmacy dispensing. Low-risk mutations (booking, reschedule, cancel, transfers)
execute immediately and are audited.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.errors import NotFoundError, ValidationError
from backend.guardrails.policy import HIGH_IMPACT_ACTIONS, approval_reason, requires_human_approval
from backend.models import ApprovalRequest, ApprovalStatus
from backend.services.common import utcnow


def request_approval(
    session: Session,
    user: CurrentUser,
    *,
    action: str,
    resource_id: str | int,
    summary: str,
    payload: Dict[str, Any] | None = None,
    commit: bool = True,
) -> ApprovalRequest:
    if not requires_human_approval(action):
        raise ValidationError(
            f"'{action}' is not a gated action. Gated actions: {sorted(HIGH_IMPACT_ACTIONS)}"
        )
    existing = session.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.action == action,
            ApprovalRequest.resource_id == str(resource_id),
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
        )
    )
    if existing:
        return existing

    approval = ApprovalRequest(
        action=action,
        resource_id=str(resource_id),
        requested_by=user.username,
        summary=summary[:500],
        payload_json=json.dumps(payload or {}, default=str),
    )
    session.add(approval)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="approval_request",
        resource=action,
        resource_id=resource_id,
        detail=approval_reason(action),
    )
    if commit:
        session.commit()
    return approval


def list_approvals(
    session: Session, user: CurrentUser, *, status: str | None = ApprovalStatus.PENDING.value
) -> List[ApprovalRequest]:
    user.require("approval:read")
    stmt = select(ApprovalRequest)
    if status:
        stmt = stmt.where(ApprovalRequest.status == status)
    return list(session.scalars(stmt.order_by(ApprovalRequest.id.desc())))


def get_approval(session: Session, approval_id: int) -> ApprovalRequest:
    approval = session.get(ApprovalRequest, approval_id)
    if not approval:
        raise NotFoundError(f"Approval request {approval_id} not found.")
    return approval


def decide(
    session: Session,
    user: CurrentUser,
    approval_id: int,
    *,
    approved: bool,
    notes: str = "",
) -> ApprovalRequest:
    """Record a human decision. Approver must hold the right role and cannot
    approve their own request (four-eyes principle)."""
    user.require("approval:decide")
    approval = get_approval(session, approval_id)
    user.require_approver(approval.action)

    if approval.status != ApprovalStatus.PENDING.value:
        raise ValidationError("This request has already been decided.")
    if approval.requested_by == user.username and approved:
        raise ValidationError("A request cannot be approved by the same person who raised it.")
    if not approved and not notes.strip():
        raise ValidationError("Rejection requires a reason.")

    approval.status = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
    approval.decided_by = user.username
    approval.decided_at = utcnow()
    approval.decision_notes = notes.strip()[:500] or None
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="approval_decision",
        resource=approval.action,
        resource_id=approval.resource_id,
        outcome="approved" if approved else "rejected",
    )
    session.commit()
    return approval


def approved_ticket(session: Session, action: str, resource_id: str | int) -> ApprovalRequest | None:
    """Return a usable approval ticket for an action, if one exists."""
    return session.scalar(
        select(ApprovalRequest).where(
            ApprovalRequest.action == action,
            ApprovalRequest.resource_id == str(resource_id),
            ApprovalRequest.status == ApprovalStatus.APPROVED.value,
        )
    )


def payload_of(approval: ApprovalRequest) -> Dict[str, Any]:
    try:
        return json.loads(approval.payload_json or "{}")
    except json.JSONDecodeError:
        return {}

