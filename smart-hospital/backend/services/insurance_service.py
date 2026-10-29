"""Insurance policies, pre-authorisation and claim tracking.

Claim *decisions* are never automated: eligibility is computed deterministically
and the final approve/reject step requires a human approval ticket.
"""
from __future__ import annotations

from datetime import date
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import ApprovalRequiredError, NotFoundError, ValidationError
from backend.models import ApprovalAction, Claim, ClaimStatus, InsurancePolicy, Invoice, Patient
from backend.services import approval_service, billing_service
from backend.services.common import next_reference, utcnow


def create_policy(
    session: Session,
    user: CurrentUser,
    *,
    patient_id: int,
    payer_name: str,
    policy_no: str,
    coverage_amount: float,
    valid_from: date,
    valid_to: date,
) -> InsurancePolicy:
    user.require("insurance:write")
    if not session.get(Patient, patient_id):
        raise NotFoundError(f"Patient {patient_id} not found.")
    if valid_to <= valid_from:
        raise ValidationError("Policy end date must be after the start date.")
    if coverage_amount <= 0:
        raise ValidationError("Coverage amount must be positive.")
    if session.scalar(select(InsurancePolicy).where(InsurancePolicy.policy_no == policy_no)):
        raise ValidationError(f"Policy '{policy_no}' already exists.")

    policy = InsurancePolicy(
        policy_no=policy_no.strip().upper(),
        patient_id=patient_id,
        payer_name=payer_name.strip(),
        coverage_amount=coverage_amount,
        valid_from=valid_from,
        valid_to=valid_to,
    )
    session.add(policy)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="policy_create",
        resource="insurance_policy",
        resource_id=policy.id,
    )
    session.commit()
    return policy


def check_eligibility(session: Session, policy: InsurancePolicy, invoice: Invoice) -> dict:
    """Deterministic eligibility rules (no LLM involvement)."""
    today = date.today()
    reasons: List[str] = []
    if not (policy.valid_from <= today <= policy.valid_to):
        reasons.append("Policy is not active for today's date.")
    if policy.patient_id != invoice.patient_id:
        reasons.append("Policy holder does not match the invoice patient.")
    used = (
        session.scalar(
            select(Claim).where(Claim.policy_id == policy.id, Claim.status == ClaimStatus.APPROVED.value)
        )
        is not None
    )
    claimable = min(invoice.total, policy.coverage_amount)
    if invoice.total <= 0:
        reasons.append("Invoice has no billable amount.")
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "claimable_amount": round(claimable, 2),
        "prior_approved_claim": used,
    }


def create_claim(
    session: Session, user: CurrentUser, *, policy_id: int, invoice_id: int, notes: str = ""
) -> Claim:
    user.require("insurance:write")
    policy = session.get(InsurancePolicy, policy_id)
    if not policy:
        raise NotFoundError(f"Policy {policy_id} not found.")
    invoice = billing_service.get_invoice(session, user, invoice_id)
    eligibility = check_eligibility(session, policy, invoice)
    if not eligibility["eligible"]:
        raise ValidationError("; ".join(eligibility["reasons"]))

    claim = Claim(
        claim_no=next_reference(session, Claim, Claim.claim_no, "CLM"),
        policy_id=policy.id,
        invoice_id=invoice.id,
        status=ClaimStatus.SUBMITTED.value,
        requested_amount=eligibility["claimable_amount"],
        notes=notes.strip() or None,
    )
    session.add(claim)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="claim_submit",
        resource="claim",
        resource_id=claim.id,
    )
    session.commit()
    return claim


def request_decision(
    session: Session, user: CurrentUser, claim_id: int, *, proposed_amount: float, rationale: str
) -> int:
    """Raise the human approval ticket for a claim decision."""
    user.require("insurance:write")
    claim = get_claim(session, user, claim_id)
    if claim.status not in (ClaimStatus.SUBMITTED.value, ClaimStatus.IN_REVIEW.value):
        raise ValidationError("Only submitted or in-review claims can be decided.")
    if proposed_amount < 0 or proposed_amount > claim.requested_amount:
        raise ValidationError("Proposed amount must be between 0 and the requested amount.")
    if len(rationale.strip()) < 10:
        raise ValidationError("Provide a rationale of at least 10 characters.")

    claim.status = ClaimStatus.IN_REVIEW.value
    approval = approval_service.request_approval(
        session,
        user,
        action=ApprovalAction.CLAIM_DECISION_SUPPORT.value,
        resource_id=claim.id,
        summary=f"Claim {claim.claim_no} decision",
        payload={"proposed_amount": proposed_amount, "rationale": rationale.strip()},
    )
    return approval.id


def apply_decision(session: Session, user: CurrentUser, claim_id: int) -> Claim:
    """Apply an approved claim decision; blocked without human sign-off."""
    user.require("insurance:write")
    claim = get_claim(session, user, claim_id)
    ticket = approval_service.approved_ticket(
        session, ApprovalAction.CLAIM_DECISION_SUPPORT.value, claim.id
    )
    if not ticket:
        raise ApprovalRequiredError("Claim decisions require billing sign-off.")

    payload = approval_service.payload_of(ticket)
    amount = float(payload.get("proposed_amount", 0.0))
    claim.approved_amount = round(amount, 2)
    claim.status = ClaimStatus.APPROVED.value if amount > 0 else ClaimStatus.REJECTED.value
    claim.decided_by = ticket.decided_by
    claim.decided_at = utcnow()
    claim.notes = (claim.notes or "") + f" | {payload.get('rationale', '')}"
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="claim_decision_apply",
        resource="claim",
        resource_id=claim.id,
        detail=f"approval={ticket.id}",
    )
    session.commit()
    return claim


def get_claim(session: Session, user: CurrentUser, claim_id: int) -> Claim:
    user.require("insurance:read")
    claim = session.get(Claim, claim_id)
    if not claim:
        raise NotFoundError(f"Claim {claim_id} not found.")
    enforce_patient_scope(user, claim.policy.patient_id)
    return claim


def get_claim_by_no(session: Session, user: CurrentUser, claim_no: str) -> Claim:
    user.require("insurance:read")
    claim = session.scalar(select(Claim).where(Claim.claim_no == claim_no.strip().upper()))
    if not claim:
        raise NotFoundError(f"Claim '{claim_no}' not found.")
    enforce_patient_scope(user, claim.policy.patient_id)
    return claim


def list_policies(session: Session, user: CurrentUser, *, patient_id: int | None = None) -> List[InsurancePolicy]:
    user.require("insurance:read")
    stmt = select(InsurancePolicy)
    if user.role.value == "patient":
        patient_id = user.patient_id
    if patient_id:
        enforce_patient_scope(user, patient_id)
        stmt = stmt.where(InsurancePolicy.patient_id == patient_id)
    return list(session.scalars(stmt.order_by(InsurancePolicy.id.desc())))


def list_claims(session: Session, user: CurrentUser, *, status: str | None = None) -> List[Claim]:
    user.require("insurance:read")
    stmt = select(Claim)
    if status:
        stmt = stmt.where(Claim.status == status)
    claims = list(session.scalars(stmt.order_by(Claim.id.desc())))
    if user.role.value == "patient":
        claims = [c for c in claims if c.policy.patient_id == user.patient_id]
    return claims

