"""Discharge summaries and follow-up appointment scheduling."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from backend.auth.rbac import CurrentUser
from backend.core.errors import ValidationError
from backend.models import Admission, AdmissionStatus, Doctor, Patient
from backend.services import admission_service, appointment_service, billing_service


def build_discharge_summary(session: Session, user: CurrentUser, admission_id: int) -> str:
    """Deterministic, data-driven summary. No LLM generated clinical content."""
    admission = admission_service.get_admission(session, user, admission_id)
    patient = session.get(Patient, admission.patient_id)
    stay_days = admission_service.length_of_stay_days(admission)
    invoices = billing_service.list_invoices(session, user, patient_id=patient.id)
    billed = sum(inv.total for inv in invoices)
    lines = [
        "DISCHARGE SUMMARY",
        f"Admission   : {admission.reference}",
        f"Patient     : {patient.full_name} ({patient.patient_uid})",
        f"Admitted    : {admission.admitted_at:%Y-%m-%d %H:%M}",
        f"Status      : {admission.status}",
        f"Length of stay (days): {stay_days}",
        f"Reason      : {admission.reason or 'n/a'}",
        f"Total billed: {billed:.2f}",
        "",
        "Clinical notes (clinician entered):",
        admission.discharge_summary or "<pending clinician sign-off>",
        "",
        f"Approved by : {admission.discharge_approved_by or '<pending>'}",
    ]
    return "\n".join(lines)


def schedule_follow_up(
    session: Session,
    user: CurrentUser,
    *,
    admission_id: int,
    doctor_id: int,
    days_ahead: int = 7,
    reason: str = "Post-discharge follow-up",
) -> Optional[object]:
    """Book the first free slot for the doctor ``days_ahead`` days from now."""
    admission = admission_service.get_admission(session, user, admission_id)
    if admission.status != AdmissionStatus.DISCHARGED.value:
        raise ValidationError("Follow-up can only be scheduled after discharge.")
    if days_ahead < 1 or days_ahead > 90:
        raise ValidationError("Follow-up must be between 1 and 90 days ahead.")

    doctor = session.get(Doctor, doctor_id)
    if not doctor:
        raise ValidationError("Doctor not found for follow-up.")

    for offset in range(days_ahead, days_ahead + 14):
        day = (datetime.utcnow() + timedelta(days=offset)).date()
        slots = appointment_service.available_slots(session, user, doctor_id, day)
        if slots:
            return appointment_service.book_appointment(
                session,
                user,
                patient_id=admission.patient_id,
                doctor_id=doctor_id,
                slot_start=slots[0],
                reason=reason,
            )
    raise ValidationError("No free follow-up slot found in the next two weeks.")

