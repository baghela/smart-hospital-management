"""Patient and directory routes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import (
    DepartmentCreate,
    DepartmentOut,
    DoctorCreate,
    DoctorOut,
    PaginatedPatients,
    PatientCreate,
    PatientOut,
    PatientUpdate,
)
from backend.auth.rbac import CurrentUser
from backend.services import directory_service, patient_service

router = APIRouter(tags=["directory"])


# --- patients -------------------------------------------------------------
@router.post("/patients", response_model=PatientOut, status_code=201)
def create_patient(
    payload: PatientCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return patient_service.register_patient(
        session,
        user,
        full_name=payload.full_name,
        date_of_birth=payload.date_of_birth,
        gender=payload.gender,
        phone=payload.phone,
        email=str(payload.email) if payload.email else None,
        address=payload.address,
        blood_group=payload.blood_group,
        consent_treatment=payload.consent_treatment,
        consent_data_sharing=payload.consent_data_sharing,
    )


@router.get("/patients", response_model=PaginatedPatients)
def search_patients(
    q: str = Query(default="", max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows, total = patient_service.search_patients(session, user, query=q, page=page, page_size=page_size)
    return PaginatedPatients(
        items=[PatientOut.model_validate(r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/patients/{patient_id}", response_model=PatientOut)
def get_patient(
    patient_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return patient_service.get_patient(session, user, patient_id)


@router.patch("/patients/{patient_id}", response_model=PatientOut)
def update_patient(
    patient_id: int,
    payload: PatientUpdate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    data = payload.model_dump(exclude_none=True)
    if "email" in data:
        data["email"] = str(data["email"])
    return patient_service.update_patient(session, user, patient_id, **data)


# --- departments & doctors ------------------------------------------------
@router.post("/departments", response_model=DepartmentOut, status_code=201)
def create_department(
    payload: DepartmentCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return directory_service.create_department(
        session, user, code=payload.code, name=payload.name, floor=payload.floor
    )


@router.get("/departments", response_model=List[DepartmentOut])
def list_departments(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    return directory_service.list_departments(session, user)


@router.post("/doctors", response_model=DoctorOut, status_code=201)
def create_doctor(
    payload: DoctorCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return directory_service.create_doctor(session, user, **payload.model_dump())


@router.get("/doctors", response_model=List[DoctorOut])
def list_doctors(
    department_id: int | None = None,
    q: str = "",
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return directory_service.list_doctors(session, user, department_id=department_id, query=q)

