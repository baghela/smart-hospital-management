"""Laboratory and pharmacy routes (dispensing is approval-gated)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import (
    LabOrderCreate,
    LabResultCreate,
    PharmacyItemCreate,
    PrescriptionCreate,
)
from backend.auth.rbac import CurrentUser
from backend.services import lab_service, pharmacy_service

router = APIRouter(tags=["clinical-ops"])


# --- lab ------------------------------------------------------------------
@router.get("/lab/catalog")
def lab_catalog(_user: CurrentUser = Depends(current_user)):
    return [{"code": c, "name": n, "price": p} for c, (n, p) in lab_service.TEST_CATALOG.items()]


@router.post("/lab/orders", status_code=201)
def order_test(
    payload: LabOrderCreate, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    order = lab_service.order_test(session, user, patient_id=payload.patient_id, test_code=payload.test_code)
    return {"id": order.id, "reference": order.reference, "status": order.status}


@router.get("/lab/orders")
def list_orders(
    patient_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows = lab_service.list_orders(session, user, patient_id=patient_id, status=status)
    return [
        {
            "id": o.id,
            "reference": o.reference,
            "patient_id": o.patient_id,
            "test": o.test_name,
            "status": o.status,
            "specimen_id": o.specimen_id,
            "result": o.result_value,
        }
        for o in rows
    ]


@router.post("/lab/orders/{order_id}/collect")
def collect(order_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    order = lab_service.collect_specimen(session, user, order_id)
    return {"id": order.id, "specimen_id": order.specimen_id, "status": order.status}


@router.post("/lab/orders/{order_id}/start")
def start(order_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    order = lab_service.start_processing(session, user, order_id)
    return {"id": order.id, "status": order.status}


@router.post("/lab/orders/{order_id}/result")
def result(
    order_id: int,
    payload: LabResultCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    order = lab_service.record_result(session, user, order_id, value=payload.value, notes=payload.notes)
    return {"id": order.id, "status": order.status, "result": order.result_value}


# --- pharmacy -------------------------------------------------------------
@router.post("/pharmacy/items", status_code=201)
def upsert_item(
    payload: PharmacyItemCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    item = pharmacy_service.upsert_item(session, user, **payload.model_dump())
    return {"id": item.id, "sku": item.sku, "stock_qty": item.stock_qty}


@router.get("/pharmacy/items")
def list_items(
    low_stock_only: bool = False,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows = pharmacy_service.list_items(session, user, low_stock_only=low_stock_only)
    return [
        {
            "id": i.id,
            "sku": i.sku,
            "name": i.name,
            "form": i.form,
            "unit_price": i.unit_price,
            "stock_qty": i.stock_qty,
            "reorder_level": i.reorder_level,
        }
        for i in rows
    ]


@router.post("/pharmacy/prescriptions", status_code=201)
def create_prescription(
    payload: PrescriptionCreate,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rx = pharmacy_service.create_prescription(session, user, **payload.model_dump())
    return {"id": rx.id, "reference": rx.reference, "status": rx.status}


@router.get("/pharmacy/prescriptions")
def list_prescriptions(
    patient_id: int | None = None,
    status: str | None = None,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rows = pharmacy_service.list_prescriptions(session, user, patient_id=patient_id, status=status)
    return [
        {
            "id": r.id,
            "reference": r.reference,
            "patient_id": r.patient_id,
            "item": r.item.name,
            "quantity": r.quantity,
            "dosage": r.dosage,
            "status": r.status,
        }
        for r in rows
    ]


@router.post("/pharmacy/prescriptions/{prescription_id}/dispense-request", status_code=202)
def dispense_request(
    prescription_id: int,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    approval_id = pharmacy_service.request_dispense(session, user, prescription_id)
    return {"approval_id": approval_id, "status": "pending_human_approval"}


@router.post("/pharmacy/prescriptions/{prescription_id}/dispense")
def dispense(
    prescription_id: int,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    rx = pharmacy_service.complete_dispense(session, user, prescription_id)
    return {"id": rx.id, "status": rx.status, "dispensed_by": rx.dispensed_by}

