"""Ward, bed, admission and discharge routes (discharge is approval-gated)."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import (
    AdmissionCreate,
    AdmissionOut,
    DischargeRequest,
    TransferRequest,
    WardCreate,
)
from backend.auth.rbac import CurrentUser
from backend.services import admission_service, discharge_service

router = APIRouter(tags=["admissions"])


@router.post("/wards", status_code=201)
def create_ward(
    payload: WardCreate, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    ward = admission_service.create_ward(session, user, **payload.model_dump())
    return {"id": ward.id, "code": ward.code, "name": ward.name}


@router.get("/beds/board")
def bed_board(
    ward_id: int | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return admission_service.bed_board(session, user, ward_id=ward_id)


@router.get("/beds/availability")
def bed_availability(session: Session = Depends(db_session), _user: CurrentUser = Depends(current_user)):
    return admission_service.bed_availability_summary(session)


@router.post("/admissions", response_model=AdmissionOut, status_code=201)
def admit(
    payload: AdmissionCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return admission_service.admit_patient(
        session, user, patient_id=payload.patient_id, bed_id=payload.bed_id, reason=payload.reason
    )


@router.get("/admissions", response_model=List[AdmissionOut])
def list_admissions(
    status: str | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return admission_service.list_admissions(session, user, status=status)


@router.post("/admissions/{admission_id}/transfer", response_model=AdmissionOut)
def transfer(
    admission_id: int,
    payload: TransferRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return admission_service.transfer_bed(session, user, admission_id, payload.new_bed_id)


@router.post("/admissions/{admission_id}/discharge-request", status_code=202)
def discharge_request(
    admission_id: int,
    payload: DischargeRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    """Step 1: raise a human approval ticket. No state change to the admission."""
    approval_id = admission_service.request_discharge(session, user, admission_id, summary=payload.summary)
    return {
        "approval_id": approval_id,
        "status": "pending_human_approval",
        "message": "Discharge awaits clinician sign-off.",
    }


@router.post("/admissions/{admission_id}/discharge", response_model=AdmissionOut)
def discharge(
    admission_id: int,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    """Step 2: executes only when an approved ticket exists."""
    return admission_service.complete_discharge(session, user, admission_id)


@router.get("/admissions/{admission_id}/summary")
def discharge_summary(
    admission_id: int,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return {"summary": discharge_service.build_discharge_summary(session, user, admission_id)}


@router.post("/admissions/{admission_id}/follow-up", status_code=201)
def follow_up(
    admission_id: int,
    doctor_id: int,
    days_ahead: int = 7,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    appointment = discharge_service.schedule_follow_up(
        session, user, admission_id=admission_id, doctor_id=doctor_id, days_ahead=days_ahead
    )
    return {"reference": appointment.reference, "slot_start": appointment.slot_start}

