"""Appointment and OPD encounter routes."""
from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import (
    AppointmentCancel,
    AppointmentCreate,
    AppointmentOut,
    AppointmentReschedule,
    EncounterCreate,
    SlotsOut,
)
from backend.auth.rbac import CurrentUser
from backend.services import appointment_service

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.get("/availability", response_model=SlotsOut)
def availability(
    doctor_id: int,
    day: date = Query(default_factory=date.today),
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    slots = appointment_service.available_slots(session, user, doctor_id, day)
    return SlotsOut(doctor_id=doctor_id, date=day, slots=[s.strftime("%H:%M") for s in slots])


@router.post("", response_model=AppointmentOut, status_code=201)
def book(
    payload: AppointmentCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return appointment_service.book_appointment(
        session,
        user,
        patient_id=payload.patient_id,
        doctor_id=payload.doctor_id,
        slot_start=payload.slot_start,
        reason=payload.reason,
    )


@router.get("", response_model=List[AppointmentOut])
def list_appointments(
    patient_id: int | None = None,
    doctor_id: int | None = None,
    day: date | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows, _total = appointment_service.list_appointments(
        session,
        user,
        patient_id=patient_id,
        doctor_id=doctor_id,
        day=day,
        status=status,
        page=page,
        page_size=page_size,
    )
    return rows


@router.post("/{appointment_id}/reschedule", response_model=AppointmentOut)
def reschedule(
    appointment_id: int,
    payload: AppointmentReschedule,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return appointment_service.reschedule_appointment(session, user, appointment_id, payload.slot_start)


@router.post("/{appointment_id}/cancel", response_model=AppointmentOut)
def cancel(
    appointment_id: int,
    payload: AppointmentCancel,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return appointment_service.cancel_appointment(session, user, appointment_id, payload.reason)


@router.post("/{appointment_id}/check-in", response_model=AppointmentOut)
def check_in(
    appointment_id: int,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return appointment_service.check_in(session, user, appointment_id)


@router.post("/{appointment_id}/complete", status_code=201)
def complete(
    appointment_id: int,
    payload: EncounterCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    encounter = appointment_service.complete_appointment(
        session,
        user,
        appointment_id,
        chief_complaint=payload.chief_complaint,
        observations=payload.observations,
        plan=payload.plan,
    )
    return {"encounter_id": encounter.id, "signed_by": encounter.signed_by}


@router.get("/queue/{doctor_id}", response_model=List[AppointmentOut])
def queue(
    doctor_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return appointment_service.today_queue(session, user, doctor_id)

