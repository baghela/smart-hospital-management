"""Appointment booking, rescheduling, cancellation, check-in and queueing.

Double booking is prevented by the ``uq_appointment_doctor_active_slot``
database constraint; an ``IntegrityError`` is translated to ``ConflictError``
so concurrent writers get a deterministic outcome.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import List, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.models import Appointment, AppointmentStatus, Doctor, Encounter, Patient
from backend.services.common import next_reference, utcnow

ACTIVE_STATUSES = (AppointmentStatus.BOOKED.value, AppointmentStatus.CHECKED_IN.value)


def generate_slots(doctor: Doctor, day: date) -> List[datetime]:
    """Deterministic slot generation (business rule stays in Python)."""
    slots: List[datetime] = []
    cursor = datetime.combine(day, time(hour=doctor.day_start_hour))
    end = datetime.combine(day, time(hour=doctor.day_end_hour))
    step = timedelta(minutes=doctor.slot_minutes)
    while cursor + step <= end:
        slots.append(cursor)
        cursor += step
    return slots


def available_slots(session: Session, user: CurrentUser, doctor_id: int, day: date) -> List[datetime]:
    user.require("appointment:read")
    doctor = session.get(Doctor, doctor_id)
    if not doctor or not doctor.is_active:
        raise NotFoundError(f"Doctor {doctor_id} not found or inactive.")
    taken = set(
        session.scalars(
            select(Appointment.slot_start).where(
                Appointment.doctor_id == doctor_id,
                Appointment.status.in_(ACTIVE_STATUSES),
                Appointment.slot_start >= datetime.combine(day, time.min),
                Appointment.slot_start <= datetime.combine(day, time.max),
            )
        )
    )
    now = datetime.utcnow()
    return [s for s in generate_slots(doctor, day) if s not in taken and s > now]


def book_appointment(
    session: Session,
    user: CurrentUser,
    *,
    patient_id: int,
    doctor_id: int,
    slot_start: datetime,
    reason: str | None = None,
) -> Appointment:
    user.require("appointment:write")
    enforce_patient_scope(user, patient_id)

    patient = session.get(Patient, patient_id)
    if not patient or not patient.is_active:
        raise NotFoundError(f"Patient {patient_id} not found.")
    if not patient.consent_treatment:
        raise ValidationError("Patient has not provided treatment consent.")
    doctor = session.get(Doctor, doctor_id)
    if not doctor or not doctor.is_active:
        raise NotFoundError(f"Doctor {doctor_id} not found or inactive.")

    slot_start = slot_start.replace(second=0, microsecond=0)
    if slot_start <= datetime.utcnow():
        raise ValidationError("Appointment slot must be in the future.")
    if slot_start not in generate_slots(doctor, slot_start.date()):
        raise ValidationError("Requested time is outside the doctor's schedule grid.")

    # Same patient cannot hold two active appointments at the same time.
    clash = session.scalar(
        select(Appointment).where(
            Appointment.patient_id == patient_id,
            Appointment.slot_start == slot_start,
            Appointment.status.in_(ACTIVE_STATUSES),
        )
    )
    if clash:
        raise ConflictError("Patient already has an appointment at this time.")

    appointment = Appointment(
        reference=next_reference(session, Appointment, Appointment.reference, "APT"),
        patient_id=patient_id,
        doctor_id=doctor_id,
        slot_start=slot_start,
        slot_end=slot_start + timedelta(minutes=doctor.slot_minutes),
        active_slot=slot_start,
        status=AppointmentStatus.BOOKED.value,
        reason=(reason or "").strip() or None,
    )
    session.add(appointment)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("That slot has just been booked by someone else.") from exc

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="appointment_book",
        resource="appointment",
        resource_id=appointment.id,
    )
    session.commit()
    return appointment


def reschedule_appointment(
    session: Session, user: CurrentUser, appointment_id: int, new_slot: datetime
) -> Appointment:
    user.require("appointment:write")
    appointment = get_appointment(session, user, appointment_id)
    if appointment.status not in ACTIVE_STATUSES:
        raise ValidationError("Only active appointments can be rescheduled.")
    doctor = session.get(Doctor, appointment.doctor_id)
    new_slot = new_slot.replace(second=0, microsecond=0)
    if new_slot <= datetime.utcnow():
        raise ValidationError("New slot must be in the future.")
    if new_slot not in generate_slots(doctor, new_slot.date()):
        raise ValidationError("Requested time is outside the doctor's schedule grid.")

    appointment.slot_start = new_slot
    appointment.slot_end = new_slot + timedelta(minutes=doctor.slot_minutes)
    appointment.active_slot = new_slot
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("That slot is already taken.") from exc

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="appointment_reschedule",
        resource="appointment",
        resource_id=appointment.id,
    )
    session.commit()
    return appointment


def cancel_appointment(session: Session, user: CurrentUser, appointment_id: int, reason: str) -> Appointment:
    user.require("appointment:write")
    appointment = get_appointment(session, user, appointment_id)
    if appointment.status == AppointmentStatus.COMPLETED.value:
        raise ValidationError("Completed appointments cannot be cancelled.")
    appointment.status = AppointmentStatus.CANCELLED.value
    appointment.active_slot = None  # frees the unique slot for rebooking
    appointment.reason = (appointment.reason or "") + f" | cancelled: {reason.strip()}"
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="appointment_cancel",
        resource="appointment",
        resource_id=appointment.id,
    )
    session.commit()
    return appointment


def check_in(session: Session, user: CurrentUser, appointment_id: int) -> Appointment:
    user.require("appointment:write")
    appointment = get_appointment(session, user, appointment_id)
    if appointment.status != AppointmentStatus.BOOKED.value:
        raise ValidationError("Only booked appointments can be checked in.")
    today_start = datetime.combine(appointment.slot_start.date(), time.min)
    today_end = datetime.combine(appointment.slot_start.date(), time.max)
    queue_number = (
        session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.doctor_id == appointment.doctor_id,
                Appointment.checked_in_at.is_not(None),
                Appointment.slot_start.between(today_start, today_end),
            )
        )
        or 0
    ) + 1
    appointment.status = AppointmentStatus.CHECKED_IN.value
    appointment.checked_in_at = utcnow()
    appointment.queue_number = queue_number
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="appointment_checkin",
        resource="appointment",
        resource_id=appointment.id,
    )
    session.commit()
    return appointment


def complete_appointment(
    session: Session,
    user: CurrentUser,
    appointment_id: int,
    *,
    chief_complaint: str,
    observations: str | None = None,
    plan: str | None = None,
) -> Encounter:
    """Close an OPD visit with clinician-signed documentation."""
    user.require("encounter:write")
    appointment = get_appointment(session, user, appointment_id)
    if appointment.status != AppointmentStatus.CHECKED_IN.value:
        raise ValidationError("Patient must be checked in before the visit can be completed.")
    if not chief_complaint.strip():
        raise ValidationError("Chief complaint is required.")

    encounter = Encounter(
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        chief_complaint=chief_complaint.strip(),
        observations=(observations or "").strip() or None,
        plan=(plan or "").strip() or None,
        signed_by=user.username,  # human sign-off, never AI generated alone
        signed_at=utcnow(),
    )
    appointment.status = AppointmentStatus.COMPLETED.value
    appointment.active_slot = None
    session.add(encounter)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="encounter_sign",
        resource="encounter",
        resource_id=encounter.id,
    )
    session.commit()
    return encounter


def get_appointment(session: Session, user: CurrentUser, appointment_id: int) -> Appointment:
    user.require("appointment:read")
    appointment = session.get(Appointment, appointment_id)
    if not appointment:
        raise NotFoundError(f"Appointment {appointment_id} not found.")
    enforce_patient_scope(user, appointment.patient_id)
    return appointment


def list_appointments(
    session: Session,
    user: CurrentUser,
    *,
    patient_id: int | None = None,
    doctor_id: int | None = None,
    day: date | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[List[Appointment], int]:
    user.require("appointment:read")
    stmt = select(Appointment)
    if user.role.value == "patient":
        patient_id = user.patient_id
    if patient_id:
        enforce_patient_scope(user, patient_id)
        stmt = stmt.where(Appointment.patient_id == patient_id)
    if doctor_id:
        stmt = stmt.where(Appointment.doctor_id == doctor_id)
    if day:
        stmt = stmt.where(
            Appointment.slot_start.between(datetime.combine(day, time.min), datetime.combine(day, time.max))
        )
    if status:
        stmt = stmt.where(Appointment.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows: Sequence[Appointment] = session.scalars(
        stmt.order_by(Appointment.slot_start).offset(max(0, page - 1) * page_size).limit(page_size)
    ).all()
    return list(rows), total


def today_queue(session: Session, user: CurrentUser, doctor_id: int) -> List[Appointment]:
    user.require("appointment:read")
    today = date.today()
    stmt = (
        select(Appointment)
        .where(
            Appointment.doctor_id == doctor_id,
            Appointment.status == AppointmentStatus.CHECKED_IN.value,
            Appointment.slot_start.between(
                datetime.combine(today, time.min), datetime.combine(today, time.max)
            ),
        )
        .order_by(Appointment.queue_number)
    )
    return list(session.scalars(stmt))

