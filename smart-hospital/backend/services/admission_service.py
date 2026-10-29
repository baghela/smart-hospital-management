"""Ward/bed management, admission, transfer and (approval-gated) discharge."""
from __future__ import annotations

from typing import Dict, List

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import ApprovalRequiredError, ConflictError, NotFoundError, ValidationError
from backend.models import (
    Admission,
    AdmissionStatus,
    ApprovalAction,
    Bed,
    BedStatus,
    BedTransfer,
    Patient,
    Ward,
)
from backend.services import approval_service
from backend.services.common import next_reference, utcnow


# --------------------------------------------------------------------------
# Ward & bed setup
# --------------------------------------------------------------------------
def create_ward(
    session: Session,
    user: CurrentUser,
    *,
    code: str,
    name: str,
    department_id: int,
    daily_rate: float = 2000.0,
    bed_count: int = 0,
) -> Ward:
    user.require("bed:write")
    if session.scalar(select(Ward).where(Ward.code == code.upper())):
        raise ValidationError(f"Ward '{code}' already exists.")
    ward = Ward(code=code.upper(), name=name, department_id=department_id, daily_rate=daily_rate)
    session.add(ward)
    session.flush()
    for index in range(1, bed_count + 1):
        session.add(Bed(ward_id=ward.id, label=f"{ward.code}-{index:02d}"))
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="ward_create",
        resource="ward",
        resource_id=ward.id,
    )
    session.commit()
    return ward


def bed_board(session: Session, user: CurrentUser, *, ward_id: int | None = None) -> List[dict]:
    """Interactive bed board payload: bed, ward, status and occupant reference."""
    user.require("bed:read")
    stmt = select(Bed, Ward).join(Ward, Bed.ward_id == Ward.id)
    if ward_id:
        stmt = stmt.where(Bed.ward_id == ward_id)
    rows = session.execute(stmt.order_by(Ward.code, Bed.label)).all()

    occupants: Dict[int, Admission] = {
        adm.active_bed_id: adm
        for adm in session.scalars(
            select(Admission).where(Admission.status == AdmissionStatus.ACTIVE.value)
        )
        if adm.active_bed_id is not None
    }
    board = []
    for bed, ward in rows:
        admission = occupants.get(bed.id)
        board.append(
            {
                "bed_id": bed.id,
                "bed": bed.label,
                "ward": ward.name,
                "ward_id": ward.id,
                "daily_rate": ward.daily_rate,
                "status": bed.status,
                "admission_ref": admission.reference if admission else None,
                "patient_id": admission.patient_id if admission else None,
            }
        )
    return board


def available_beds(session: Session, user: CurrentUser, *, ward_id: int | None = None) -> List[Bed]:
    user.require("bed:read")
    stmt = select(Bed).where(Bed.status == BedStatus.AVAILABLE.value)
    if ward_id:
        stmt = stmt.where(Bed.ward_id == ward_id)
    return list(session.scalars(stmt.order_by(Bed.id)))


def bed_availability_summary(session: Session) -> List[dict]:
    """Read-only aggregate used by the copilot tool and dashboard KPIs."""
    rows = session.execute(
        select(Ward.name, Bed.status, func.count(Bed.id)).join(Bed, Bed.ward_id == Ward.id).group_by(
            Ward.name, Bed.status
        )
    ).all()
    summary: Dict[str, Dict[str, int]] = {}
    for ward_name, status, count in rows:
        summary.setdefault(ward_name, {"available": 0, "occupied": 0, "maintenance": 0})
        summary[ward_name][status] = count
    return [{"ward": ward, **counts} for ward, counts in sorted(summary.items())]


# --------------------------------------------------------------------------
# Admission lifecycle
# --------------------------------------------------------------------------
def admit_patient(
    session: Session, user: CurrentUser, *, patient_id: int, bed_id: int, reason: str
) -> Admission:
    user.require("admission:write")
    patient = session.get(Patient, patient_id)
    if not patient or not patient.is_active:
        raise NotFoundError(f"Patient {patient_id} not found.")
    bed = session.get(Bed, bed_id)
    if not bed:
        raise NotFoundError(f"Bed {bed_id} not found.")
    if bed.status != BedStatus.AVAILABLE.value:
        raise ConflictError(f"Bed {bed.label} is not available ({bed.status}).")
    if session.scalar(
        select(Admission).where(
            Admission.patient_id == patient_id, Admission.status == AdmissionStatus.ACTIVE.value
        )
    ):
        raise ConflictError("Patient already has an active admission.")

    admission = Admission(
        reference=next_reference(session, Admission, Admission.reference, "ADM"),
        patient_id=patient_id,
        bed_id=bed.id,
        active_bed_id=bed.id,
        reason=reason.strip() or None,
        admitted_at=utcnow(),
    )
    bed.status = BedStatus.OCCUPIED.value
    session.add(admission)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("That bed was just allocated to another patient.") from exc

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="admission_create",
        resource="admission",
        resource_id=admission.id,
    )
    session.commit()
    return admission


def transfer_bed(session: Session, user: CurrentUser, admission_id: int, new_bed_id: int) -> Admission:
    user.require("bed:write")
    admission = get_admission(session, user, admission_id)
    if admission.status != AdmissionStatus.ACTIVE.value:
        raise ValidationError("Only active admissions can be transferred.")
    new_bed = session.get(Bed, new_bed_id)
    if not new_bed:
        raise NotFoundError(f"Bed {new_bed_id} not found.")
    if new_bed.status != BedStatus.AVAILABLE.value:
        raise ConflictError(f"Bed {new_bed.label} is not available.")

    old_bed = session.get(Bed, admission.bed_id)
    session.add(
        BedTransfer(
            admission_id=admission.id,
            from_bed_id=old_bed.id,
            to_bed_id=new_bed.id,
            moved_by=user.username,
            moved_at=utcnow(),
        )
    )
    old_bed.status = BedStatus.AVAILABLE.value
    new_bed.status = BedStatus.OCCUPIED.value
    admission.bed_id = new_bed.id
    admission.active_bed_id = new_bed.id
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("Target bed was just taken.") from exc

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="bed_transfer",
        resource="admission",
        resource_id=admission.id,
    )
    session.commit()
    return admission


def request_discharge(
    session: Session, user: CurrentUser, admission_id: int, *, summary: str
) -> int:
    """Step 1 of the gated discharge: raise a human approval request."""
    user.require("discharge:request")
    admission = get_admission(session, user, admission_id)
    if admission.status != AdmissionStatus.ACTIVE.value:
        raise ValidationError("Admission is not active.")
    if len(summary.strip()) < 20:
        raise ValidationError("Discharge summary must be at least 20 characters.")

    approval = approval_service.request_approval(
        session,
        user,
        action=ApprovalAction.DISCHARGE.value,
        resource_id=admission.id,
        summary=f"Discharge {admission.reference}",
        payload={"discharge_summary": summary.strip()},
    )
    return approval.id


def complete_discharge(session: Session, user: CurrentUser, admission_id: int) -> Admission:
    """Step 2: executes only when an approved ticket exists."""
    user.require("discharge:request")
    admission = get_admission(session, user, admission_id)
    if admission.status != AdmissionStatus.ACTIVE.value:
        raise ValidationError("Admission is not active.")

    ticket = approval_service.approved_ticket(session, ApprovalAction.DISCHARGE.value, admission.id)
    if not ticket:
        raise ApprovalRequiredError(
            "Discharge requires an approved clinician sign-off before it can run."
        )

    payload = approval_service.payload_of(ticket)
    bed = session.get(Bed, admission.bed_id)
    admission.status = AdmissionStatus.DISCHARGED.value
    admission.discharged_at = utcnow()
    admission.active_bed_id = None
    admission.discharge_summary = payload.get("discharge_summary")
    admission.discharge_approved_by = ticket.decided_by
    if bed:
        bed.status = BedStatus.AVAILABLE.value

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="admission_discharge",
        resource="admission",
        resource_id=admission.id,
        detail=f"approval={ticket.id}",
    )
    session.commit()
    return admission


def get_admission(session: Session, user: CurrentUser, admission_id: int) -> Admission:
    user.require("bed:read")
    admission = session.get(Admission, admission_id)
    if not admission:
        raise NotFoundError(f"Admission {admission_id} not found.")
    enforce_patient_scope(user, admission.patient_id)
    return admission


def list_admissions(session: Session, user: CurrentUser, *, status: str | None = None) -> List[Admission]:
    user.require("bed:read")
    stmt = select(Admission)
    if status:
        stmt = stmt.where(Admission.status == status)
    if user.role.value == "patient":
        stmt = stmt.where(Admission.patient_id == user.patient_id)
    return list(session.scalars(stmt.order_by(Admission.id.desc())))


def length_of_stay_days(admission: Admission) -> int:
    end = admission.discharged_at or utcnow()
    return max(1, (end - admission.admitted_at).days or 1)

