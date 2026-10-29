"""Operational dashboards and reports - all values computed from live data."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.auth.rbac import CurrentUser
from backend.models import (
    Admission,
    AdmissionStatus,
    Appointment,
    AppointmentStatus,
    ApprovalRequest,
    ApprovalStatus,
    Bed,
    BedStatus,
    ChatFeedback,
    Claim,
    FaqDocument,
    Invoice,
    InvoiceStatus,
    LabOrder,
    Patient,
)


def dashboard_kpis(session: Session, user: CurrentUser) -> Dict[str, float | int]:
    """Live KPI values (no hard-coded demo numbers)."""
    today = date.today()
    day_start, day_end = datetime.combine(today, time.min), datetime.combine(today, time.max)

    total_beds = session.scalar(select(func.count()).select_from(Bed)) or 0
    occupied = (
        session.scalar(
            select(func.count()).select_from(Bed).where(Bed.status == BedStatus.OCCUPIED.value)
        )
        or 0
    )
    revenue_today = (
        session.scalar(
            select(func.coalesce(func.sum(Invoice.amount_paid), 0.0)).where(
                Invoice.issued_at.between(day_start, day_end)
            )
        )
        or 0.0
    )
    outstanding = (
        session.scalar(
            select(func.coalesce(func.sum(Invoice.total - Invoice.amount_paid), 0.0)).where(
                Invoice.status.in_([InvoiceStatus.ISSUED.value, InvoiceStatus.PARTIALLY_PAID.value])
            )
        )
        or 0.0
    )
    return {
        "patients_total": session.scalar(
            select(func.count()).select_from(Patient).where(Patient.is_active.is_(True))
        )
        or 0,
        "appointments_today": session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.slot_start.between(day_start, day_end))
        )
        or 0,
        "checked_in_now": session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.status == AppointmentStatus.CHECKED_IN.value)
        )
        or 0,
        "active_admissions": session.scalar(
            select(func.count())
            .select_from(Admission)
            .where(Admission.status == AdmissionStatus.ACTIVE.value)
        )
        or 0,
        "beds_total": total_beds,
        "beds_occupied": occupied,
        "bed_occupancy_pct": round(100 * occupied / total_beds, 1) if total_beds else 0.0,
        "pending_approvals": session.scalar(
            select(func.count())
            .select_from(ApprovalRequest)
            .where(ApprovalRequest.status == ApprovalStatus.PENDING.value)
        )
        or 0,
        "pending_lab_orders": session.scalar(
            select(func.count()).select_from(LabOrder).where(LabOrder.status != "resulted")
        )
        or 0,
        "open_claims": session.scalar(
            select(func.count()).select_from(Claim).where(Claim.status.in_(["submitted", "in_review"]))
        )
        or 0,
        "faq_documents": session.scalar(select(func.count()).select_from(FaqDocument)) or 0,
        "revenue_today": round(float(revenue_today), 2),
        "outstanding_amount": round(float(outstanding), 2),
    }


def appointments_trend(session: Session, days: int = 7) -> List[dict]:
    today = date.today()
    out = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        count = (
            session.scalar(
                select(func.count())
                .select_from(Appointment)
                .where(
                    Appointment.slot_start.between(
                        datetime.combine(day, time.min), datetime.combine(day, time.max)
                    )
                )
            )
            or 0
        )
        out.append({"date": day.isoformat(), "appointments": count})
    return out


def revenue_by_status(session: Session) -> List[dict]:
    rows = session.execute(
        select(Invoice.status, func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0.0)).group_by(
            Invoice.status
        )
    ).all()
    return [
        {"status": status, "invoices": count, "amount": round(float(total), 2)}
        for status, count, total in rows
    ]


def department_load(session: Session) -> List[dict]:
    from backend.models import Department, Doctor  # local import avoids cycles

    rows = session.execute(
        select(Department.name, func.count(Appointment.id))
        .join(Doctor, Doctor.department_id == Department.id)
        .join(Appointment, Appointment.doctor_id == Doctor.id, isouter=True)
        .group_by(Department.name)
    ).all()
    return [{"department": name, "appointments": count} for name, count in rows]


def faq_feedback_stats(session: Session) -> Dict[str, float | int]:
    total = session.scalar(select(func.count()).select_from(ChatFeedback)) or 0
    helpful = (
        session.scalar(select(func.count()).select_from(ChatFeedback).where(ChatFeedback.helpful.is_(True)))
        or 0
    )
    return {
        "feedback_total": total,
        "helpful": helpful,
        "helpful_rate": round(100 * helpful / total, 1) if total else 0.0,
    }

