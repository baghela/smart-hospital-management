"""Billing and insurance routes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import (
    ClaimCreate,
    ClaimDecisionRequest,
    ClaimOut,
    InvoiceCreate,
    InvoiceItemCreate,
    InvoiceOut,
    PaymentCreate,
    PolicyCreate,
    RefundCreate,
)
from backend.auth.rbac import CurrentUser
from backend.services import billing_service, insurance_service

router = APIRouter(tags=["finance"])


# --- billing --------------------------------------------------------------
@router.post("/invoices", response_model=InvoiceOut, status_code=201)
def create_invoice(
    payload: InvoiceCreate, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return billing_service.create_invoice(session, user, patient_id=payload.patient_id)


@router.get("/invoices", response_model=List[InvoiceOut])
def list_invoices(
    patient_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return billing_service.list_invoices(session, user, patient_id=patient_id, status=status)


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return billing_service.invoice_summary(billing_service.get_invoice(session, user, invoice_id))


@router.post("/invoices/{invoice_id}/items", response_model=InvoiceOut)
def add_item(
    invoice_id: int,
    payload: InvoiceItemCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return billing_service.add_item(session, user, invoice_id, **payload.model_dump())


@router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
def issue(
    invoice_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return billing_service.issue_invoice(session, user, invoice_id)


@router.post("/invoices/{invoice_id}/payments", status_code=201)
def pay(
    invoice_id: int,
    payload: PaymentCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    payment = billing_service.record_payment(
        session, user, invoice_id, amount=payload.amount, method=payload.method
    )
    invoice = billing_service.get_invoice(session, user, invoice_id)
    return {
        "receipt_no": payment.receipt_no,
        "amount": payment.amount,
        "receipt": billing_service.receipt_text(invoice, payment),
    }


@router.post("/invoices/{invoice_id}/refunds", status_code=201)
def refund(
    invoice_id: int,
    payload: RefundCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    entry = billing_service.refund(session, user, invoice_id, amount=payload.amount, reason=payload.reason)
    return {"receipt_no": entry.receipt_no, "amount": entry.amount}


# --- insurance ------------------------------------------------------------
@router.post("/policies", status_code=201)
def create_policy(
    payload: PolicyCreate, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    policy = insurance_service.create_policy(session, user, **payload.model_dump())
    return {"id": policy.id, "policy_no": policy.policy_no}


@router.get("/policies")
def list_policies(
    patient_id: int | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows = insurance_service.list_policies(session, user, patient_id=patient_id)
    return [
        {
            "id": p.id,
            "policy_no": p.policy_no,
            "payer_name": p.payer_name,
            "coverage_amount": p.coverage_amount,
            "valid_to": p.valid_to,
        }
        for p in rows
    ]


@router.post("/claims", response_model=ClaimOut, status_code=201)
def create_claim(
    payload: ClaimCreate, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return insurance_service.create_claim(
        session, user, policy_id=payload.policy_id, invoice_id=payload.invoice_id, notes=payload.notes
    )


@router.get("/claims", response_model=List[ClaimOut])
def list_claims(
    status: str | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return insurance_service.list_claims(session, user, status=status)


@router.post("/claims/{claim_id}/decision-request", status_code=202)
def claim_decision_request(
    claim_id: int,
    payload: ClaimDecisionRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    approval_id = insurance_service.request_decision(
        session, user, claim_id, proposed_amount=payload.proposed_amount, rationale=payload.rationale
    )
    return {"approval_id": approval_id, "status": "pending_human_approval"}


@router.post("/claims/{claim_id}/apply-decision", response_model=ClaimOut)
def claim_apply_decision(
    claim_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return insurance_service.apply_decision(session, user, claim_id)

