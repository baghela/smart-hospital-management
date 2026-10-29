"""Department and doctor directory management."""
from __future__ import annotations

from typing import List

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.errors import NotFoundError, ValidationError
from backend.models import Department, Doctor


def create_department(
    session: Session, user: CurrentUser, *, code: str, name: str, floor: str | None = None
) -> Department:
    user.require("doctor:write")
    code = code.strip().upper()
    if not code or not name.strip():
        raise ValidationError("Department code and name are required.")
    if session.scalar(select(Department).where(Department.code == code)):
        raise ValidationError(f"Department '{code}' already exists.")
    dept = Department(code=code, name=name.strip(), floor=floor)
    session.add(dept)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="department_create",
        resource="department",
        resource_id=dept.id,
    )
    session.commit()
    return dept


def list_departments(session: Session, user: CurrentUser) -> List[Department]:
    user.require("doctor:read")
    return list(session.scalars(select(Department).where(Department.is_active.is_(True)).order_by(Department.name)))


def create_doctor(
    session: Session,
    user: CurrentUser,
    *,
    code: str,
    full_name: str,
    specialty: str,
    department_id: int,
    consultation_fee: float = 500.0,
    slot_minutes: int = 30,
    day_start_hour: int = 9,
    day_end_hour: int = 17,
) -> Doctor:
    user.require("doctor:write")
    if not session.get(Department, department_id):
        raise NotFoundError(f"Department {department_id} not found.")
    if slot_minutes not in (15, 20, 30, 45, 60):
        raise ValidationError("Slot length must be 15, 20, 30, 45 or 60 minutes.")
    if not 0 <= day_start_hour < day_end_hour <= 23:
        raise ValidationError("Working hours are invalid.")
    if consultation_fee < 0:
        raise ValidationError("Consultation fee cannot be negative.")
    code = code.strip().upper()
    if session.scalar(select(Doctor).where(Doctor.code == code)):
        raise ValidationError(f"Doctor code '{code}' already exists.")

    doctor = Doctor(
        code=code,
        full_name=full_name.strip(),
        specialty=specialty.strip(),
        department_id=department_id,
        consultation_fee=consultation_fee,
        slot_minutes=slot_minutes,
        day_start_hour=day_start_hour,
        day_end_hour=day_end_hour,
    )
    session.add(doctor)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="doctor_create",
        resource="doctor",
        resource_id=doctor.id,
    )
    session.commit()
    return doctor


def list_doctors(
    session: Session,
    user: CurrentUser,
    *,
    department_id: int | None = None,
    query: str = "",
) -> List[Doctor]:
    user.require("doctor:read")
    stmt = select(Doctor).where(Doctor.is_active.is_(True))
    if department_id:
        stmt = stmt.where(Doctor.department_id == department_id)
    if query:
        like = f"%{query.strip()}%"
        stmt = stmt.where(or_(Doctor.full_name.ilike(like), Doctor.specialty.ilike(like), Doctor.code.ilike(like)))
    return list(session.scalars(stmt.order_by(Doctor.full_name)))


def get_doctor(session: Session, user: CurrentUser, doctor_id: int) -> Doctor:
    user.require("doctor:read")
    doctor = session.get(Doctor, doctor_id)
    if not doctor:
        raise NotFoundError(f"Doctor {doctor_id} not found.")
    return doctor


def set_doctor_active(session: Session, user: CurrentUser, doctor_id: int, active: bool) -> Doctor:
    user.require("doctor:write")
    doctor = get_doctor(session, user, doctor_id)
    doctor.is_active = active
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="doctor_set_active",
        resource="doctor",
        resource_id=doctor.id,
        detail=str(active),
    )
    session.commit()
    return doctor

