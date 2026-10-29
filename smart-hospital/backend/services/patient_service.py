"""Patient registration, search, profile and consent management."""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import NotFoundError, ValidationError
from backend.models import Patient
from backend.services.common import next_reference

_VALID_GENDERS = {"male", "female", "other", "undisclosed"}


def _validate(full_name: str, dob: date, gender: str, phone: str, email: Optional[str]) -> None:
    if not full_name or len(full_name.strip()) < 3:
        raise ValidationError("Patient name must be at least 3 characters.")
    if dob > date.today():
        raise ValidationError("Date of birth cannot be in the future.")
    if (date.today().year - dob.year) > 130:
        raise ValidationError("Date of birth is not plausible.")
    if gender.lower() not in _VALID_GENDERS:
        raise ValidationError(f"Gender must be one of {sorted(_VALID_GENDERS)}.")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 8:
        raise ValidationError("Phone number must contain at least 8 digits.")
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise ValidationError("Email address is not valid.")


def register_patient(
    session: Session,
    user: CurrentUser,
    *,
    full_name: str,
    date_of_birth: date,
    gender: str,
    phone: str,
    email: str | None = None,
    address: str | None = None,
    blood_group: str | None = None,
    consent_treatment: bool = False,
    consent_data_sharing: bool = False,
) -> Patient:
    user.require("patient:write")
    _validate(full_name, date_of_birth, gender, phone, email)
    if not consent_treatment:
        raise ValidationError("Treatment consent is mandatory for registration.")

    patient = Patient(
        patient_uid=next_reference(session, Patient, Patient.patient_uid, "PAT", 6),
        full_name=full_name.strip(),
        date_of_birth=date_of_birth,
        gender=gender.lower(),
        phone=phone.strip(),
        email=(email or "").strip() or None,
        address=(address or "").strip() or None,
        blood_group=(blood_group or "").strip() or None,
        consent_treatment=consent_treatment,
        consent_data_sharing=consent_data_sharing,
        consent_recorded_at=datetime.utcnow(),
    )
    session.add(patient)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="patient_register",
        resource="patient",
        resource_id=patient.id,
    )
    session.commit()
    return patient


def update_patient(session: Session, user: CurrentUser, patient_id: int, **fields) -> Patient:
    user.require("patient:write")
    patient = get_patient(session, user, patient_id)
    allowed = {"full_name", "phone", "email", "address", "blood_group", "consent_data_sharing"}
    for key, value in fields.items():
        if key in allowed and value is not None:
            setattr(patient, key, value)
    _validate(patient.full_name, patient.date_of_birth, patient.gender, patient.phone, patient.email)
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="patient_update",
        resource="patient",
        resource_id=patient.id,
        detail=",".join(sorted(set(fields) & allowed)),
    )
    session.commit()
    return patient


def get_patient(session: Session, user: CurrentUser, patient_id: int) -> Patient:
    user.require("patient:read")
    enforce_patient_scope(user, patient_id)
    patient = session.get(Patient, patient_id)
    if not patient:
        raise NotFoundError(f"Patient {patient_id} not found.")
    return patient


def get_by_uid(session: Session, user: CurrentUser, patient_uid: str) -> Patient:
    user.require("patient:read")
    patient = session.scalar(select(Patient).where(Patient.patient_uid == patient_uid))
    if not patient:
        raise NotFoundError(f"Patient '{patient_uid}' not found.")
    enforce_patient_scope(user, patient.id)
    return patient


def search_patients(
    session: Session,
    user: CurrentUser,
    *,
    query: str = "",
    page: int = 1,
    page_size: int = 20,
) -> tuple[List[Patient], int]:
    user.require("patient:read")
    stmt = select(Patient).where(Patient.is_active.is_(True))
    if user.patient_id and user.role.value == "patient":
        stmt = stmt.where(Patient.id == user.patient_id)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(
            or_(Patient.full_name.ilike(like), Patient.patient_uid.ilike(like), Patient.phone.ilike(like))
        )
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows: Sequence[Patient] = session.scalars(
        stmt.order_by(Patient.id.desc()).offset(max(0, (page - 1)) * page_size).limit(page_size)
    ).all()
    return list(rows), total


def deactivate_patient(session: Session, user: CurrentUser, patient_id: int, reason: str) -> Patient:
    """Soft delete supporting the data-retention/erasure workflow."""
    user.require("patient:write")
    patient = get_patient(session, user, patient_id)
    patient.is_active = False
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="patient_deactivate",
        resource="patient",
        resource_id=patient.id,
        detail=reason,
    )
    session.commit()
    return patient

