"""Prescriptions and pharmacy inventory/dispensing.

Dispensing is a gated action: it only executes with an approved pharmacist
sign-off ticket, and stock is decremented transactionally.
"""
from __future__ import annotations

from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import ApprovalRequiredError, ConflictError, NotFoundError, ValidationError
from backend.models import (
    ApprovalAction,
    Doctor,
    Patient,
    PharmacyItem,
    Prescription,
    PrescriptionStatus,
)
from backend.services import approval_service
from backend.services.common import next_reference, utcnow


def upsert_item(
    session: Session,
    user: CurrentUser,
    *,
    sku: str,
    name: str,
    unit_price: float,
    stock_qty: int,
    form: str = "tablet",
    reorder_level: int = 20,
) -> PharmacyItem:
    user.require("pharmacy:write")
    if unit_price < 0 or stock_qty < 0:
        raise ValidationError("Price and stock quantity cannot be negative.")
    item = session.scalar(select(PharmacyItem).where(PharmacyItem.sku == sku.upper()))
    if item:
        item.name, item.unit_price, item.stock_qty = name, unit_price, stock_qty
        item.form, item.reorder_level = form, reorder_level
    else:
        item = PharmacyItem(
            sku=sku.upper(),
            name=name,
            unit_price=unit_price,
            stock_qty=stock_qty,
            form=form,
            reorder_level=reorder_level,
        )
        session.add(item)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="pharmacy_item_upsert",
        resource="pharmacy_item",
        resource_id=item.id,
    )
    session.commit()
    return item


def list_items(session: Session, user: CurrentUser, *, low_stock_only: bool = False) -> List[PharmacyItem]:
    user.require("pharmacy:read")
    items = list(session.scalars(select(PharmacyItem).order_by(PharmacyItem.name)))
    if low_stock_only:
        items = [i for i in items if i.stock_qty <= i.reorder_level]
    return items


def create_prescription(
    session: Session,
    user: CurrentUser,
    *,
    patient_id: int,
    doctor_id: int,
    item_id: int,
    quantity: int,
    dosage: str,
) -> Prescription:
    """Only a doctor (or admin) may prescribe; the AI never prescribes."""
    user.require("pharmacy:write")
    if user.role.value not in {"doctor", "admin"}:
        raise ValidationError("Only a doctor can create a prescription.")
    enforce_patient_scope(user, patient_id)
    if not session.get(Patient, patient_id):
        raise NotFoundError(f"Patient {patient_id} not found.")
    if not session.get(Doctor, doctor_id):
        raise NotFoundError(f"Doctor {doctor_id} not found.")
    item = session.get(PharmacyItem, item_id)
    if not item:
        raise NotFoundError(f"Pharmacy item {item_id} not found.")
    if quantity <= 0:
        raise ValidationError("Quantity must be positive.")

    prescription = Prescription(
        reference=next_reference(session, Prescription, Prescription.reference, "RX"),
        patient_id=patient_id,
        doctor_id=doctor_id,
        item_id=item_id,
        quantity=quantity,
        dosage=dosage.strip() or "1-0-1",
        status=PrescriptionStatus.ACTIVE.value,
    )
    session.add(prescription)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="prescription_create",
        resource="prescription",
        resource_id=prescription.id,
    )
    session.commit()
    return prescription


def request_dispense(session: Session, user: CurrentUser, prescription_id: int) -> int:
    """Step 1 of the gated dispense: raise a pharmacist approval request."""
    user.require("pharmacy:write")
    prescription = get_prescription(session, user, prescription_id)
    if prescription.status != PrescriptionStatus.ACTIVE.value:
        raise ValidationError("Only active prescriptions can be dispensed.")
    if prescription.item.stock_qty < prescription.quantity:
        raise ConflictError(
            f"Insufficient stock for {prescription.item.name} "
            f"({prescription.item.stock_qty} available, {prescription.quantity} needed)."
        )
    approval = approval_service.request_approval(
        session,
        user,
        action=ApprovalAction.PHARMACY_DISPENSE.value,
        resource_id=prescription.id,
        summary=f"Dispense {prescription.reference} ({prescription.item.name} x{prescription.quantity})",
        payload={"item_id": prescription.item_id, "quantity": prescription.quantity},
    )
    return approval.id


def complete_dispense(session: Session, user: CurrentUser, prescription_id: int) -> Prescription:
    """Step 2: executes only with an approved ticket; decrements stock."""
    user.require("pharmacy:write")
    prescription = get_prescription(session, user, prescription_id)
    ticket = approval_service.approved_ticket(
        session, ApprovalAction.PHARMACY_DISPENSE.value, prescription.id
    )
    if not ticket:
        raise ApprovalRequiredError("Dispensing requires pharmacist sign-off.")
    if prescription.status != PrescriptionStatus.ACTIVE.value:
        raise ValidationError("Prescription is no longer active.")

    item = prescription.item
    if item.stock_qty < prescription.quantity:
        raise ConflictError("Stock dropped below the prescribed quantity.")
    item.stock_qty -= prescription.quantity
    prescription.status = PrescriptionStatus.DISPENSED.value
    prescription.dispensed_at = utcnow()
    prescription.dispensed_by = ticket.decided_by
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="pharmacy_dispense",
        resource="prescription",
        resource_id=prescription.id,
        detail=f"approval={ticket.id}",
    )
    session.commit()
    return prescription


def get_prescription(session: Session, user: CurrentUser, prescription_id: int) -> Prescription:
    user.require("pharmacy:read")
    prescription = session.get(Prescription, prescription_id)
    if not prescription:
        raise NotFoundError(f"Prescription {prescription_id} not found.")
    enforce_patient_scope(user, prescription.patient_id)
    return prescription


def list_prescriptions(
    session: Session, user: CurrentUser, *, patient_id: int | None = None, status: str | None = None
) -> List[Prescription]:
    user.require("pharmacy:read")
    stmt = select(Prescription)
    if patient_id:
        enforce_patient_scope(user, patient_id)
        stmt = stmt.where(Prescription.patient_id == patient_id)
    if status:
        stmt = stmt.where(Prescription.status == status)
    return list(session.scalars(stmt.order_by(Prescription.id.desc())))

