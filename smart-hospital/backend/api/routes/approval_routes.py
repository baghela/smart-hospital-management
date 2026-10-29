"""Human-in-the-loop approval routes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import ApprovalDecision, ApprovalOut
from backend.auth.rbac import CurrentUser
from backend.guardrails.policy import HIGH_IMPACT_ACTIONS
from backend.services import approval_service

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("/policy")
def policy(_user: CurrentUser = Depends(current_user)):
    """Expose the narrow approval scope so the UI never has to guess."""
    return {
        "gated_actions": HIGH_IMPACT_ACTIONS,
        "note": "Low-risk mutations (booking, reschedule, cancel, transfer) execute immediately and are audited.",
    }


@router.get("", response_model=List[ApprovalOut])
def list_approvals(
    status: str | None = "pending",
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return approval_service.list_approvals(session, user, status=status)


@router.get("/{approval_id}", response_model=ApprovalOut)
def get_approval(
    approval_id: int, session: Session = Depends(db_session), _user: CurrentUser = Depends(current_user)
):
    return approval_service.get_approval(session, approval_id)


@router.post("/{approval_id}/decision", response_model=ApprovalOut)
def decide(
    approval_id: int,
    payload: ApprovalDecision,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return approval_service.decide(
        session, user, approval_id, approved=payload.approved, notes=payload.notes
    )

