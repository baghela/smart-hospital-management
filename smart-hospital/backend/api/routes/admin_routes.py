"""Admin routes: dashboards, reports, provider config and audit views."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.ai.providers import provider_status
from backend.api.deps import current_user, db_session
from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.config import get_settings
from backend.services import report_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/kpis")
def kpis(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    return report_service.dashboard_kpis(session, user)


@router.get("/reports/appointments-trend")
def appointments_trend(
    days: int = 7, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    user.require("report:read")
    return report_service.appointments_trend(session, days)


@router.get("/reports/revenue")
def revenue(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    user.require("report:read")
    return report_service.revenue_by_status(session)


@router.get("/reports/department-load")
def department_load(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    user.require("report:read")
    return report_service.department_load(session)


@router.get("/reports/faq-feedback")
def faq_feedback(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    user.require("report:read")
    return report_service.faq_feedback_stats(session)


@router.get("/providers")
def providers(user: CurrentUser = Depends(current_user)):
    user.require("admin:config")
    settings = get_settings()
    return {
        "provider_order": settings.provider_order,
        "providers": provider_status(settings),
        "token_budget": settings.session_token_budget,
        "max_output_tokens": settings.max_output_tokens,
        "retrieval_top_k": settings.retrieval_top_k,
        "relevance_threshold": settings.relevance_threshold,
        "hybrid_alpha": settings.hybrid_alpha,
        "workflow_id": settings.workflow_id,
    }


@router.get("/audit")
def audit_events(
    limit: int = 100, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    user.require("audit:read")
    return [
        {
            "id": e.id,
            "at": e.created_at,
            "actor": e.actor,
            "role": e.actor_role,
            "action": e.action,
            "resource": e.resource,
            "resource_id": e.resource_id,
            "outcome": e.outcome,
            "trace_id": e.trace_id,
            "detail": e.detail,
        }
        for e in audit.recent(session, limit)
    ]

