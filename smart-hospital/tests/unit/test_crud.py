"""CRUD, validation and relational integrity tests."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.core.errors import ConflictError, NotFoundError, ValidationError
from backend.services import appointment_service, billing_service, patient_service


def test_patient_registration_assigns_unique_uid(session, admin):
    first = patient_service.register_patient(
        session, admin, full_name="Alpha Patient", date_of_birth=date(1990, 5, 5),
        gender="female", phone="9800000011", consent_treatment=True,
    )
    second = patient_service.register_patient(
        session, admin, full_name="Beta Patient", date_of_birth=date(1991, 6, 6),
        gender="male", phone="9800000012", consent_treatment=True,
    )
    assert first.patient_uid != second.patient_uid
    assert first.patient_uid.startswith("PAT-")


def test_registration_requires_treatment_consent(session, admin):
    with pytest.raises(ValidationError, match="consent"):
        patient_service.register_patient(
            session, admin, full_name="No Consent", date_of_birth=date(1990, 1, 1),
            gender="male", phone="9800000013", consent_treatment=False,
        )


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("full_name", "ab", "at least 3"),
        ("phone", "123", "8 digits"),
        ("gender", "unknown", "Gender must be one of"),
    ],
)
def test_registration_validation(session, admin, field, value, message):
    payload = dict(
        full_name="Valid Name", date_of_birth=date(1990, 1, 1),
        gender="male", phone="9800000014", consent_treatment=True,
    )
    payload[field] = value
    with pytest.raises(ValidationError, match=message):
        patient_service.register_patient(session, admin, **payload)


def test_future_date_of_birth_rejected(session, admin):
    with pytest.raises(ValidationError, match="future"):
        patient_service.register_patient(
            session, admin, full_name="Future Person",
            date_of_birth=date.today() + timedelta(days=1),
            gender="male", phone="9800000015", consent_treatment=True,
        )


def test_patient_search_and_pagination(session, admin):
    for index in range(5):
        patient_service.register_patient(
            session, admin, full_name=f"Search Target {index}", date_of_birth=date(1990, 1, 1),
            gender="male", phone=f"980000002{index}", consent_treatment=True,
        )
    rows, total = patient_service.search_patients(session, admin, query="Search Target", page=1, page_size=2)
    assert total == 5
    assert len(rows) == 2


def test_invoice_totals_are_deterministic(session, admin, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Consultation", unit_price=600.0)
    billing_service.add_item(session, admin, invoice.id, description="CBC", unit_price=350.0, quantity=2)
    assert invoice.subtotal == 1300.0
    assert invoice.tax == 65.0
    assert invoice.total == 1365.0


def test_payment_cannot_exceed_outstanding(session, admin, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Consultation", unit_price=100.0)
    billing_service.issue_invoice(session, admin, invoice.id)
    with pytest.raises(ValidationError, match="exceeds"):
        billing_service.record_payment(session, admin, invoice.id, amount=1000.0)


def test_payment_marks_invoice_paid(session, admin, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Consultation", unit_price=100.0)
    billing_service.issue_invoice(session, admin, invoice.id)
    billing_service.record_payment(session, admin, invoice.id, amount=invoice.total)
    assert invoice.status == "paid"


def test_refund_cannot_exceed_paid_amount(session, admin, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Consultation", unit_price=100.0)
    billing_service.issue_invoice(session, admin, invoice.id)
    billing_service.record_payment(session, admin, invoice.id, amount=50.0)
    with pytest.raises(ValidationError, match="exceeds"):
        billing_service.refund(session, admin, invoice.id, amount=80.0, reason="overpaid")


def test_missing_entities_raise_not_found(session, admin):
    with pytest.raises(NotFoundError):
        patient_service.get_patient(session, admin, 9999)
    with pytest.raises(NotFoundError):
        billing_service.get_invoice(session, admin, 9999)


def test_cancelled_slot_can_be_rebooked(session, admin, hospital):
    day = date.today() + timedelta(days=1)
    slots = appointment_service.available_slots(session, admin, hospital["doctor"].id, day)
    first = appointment_service.book_appointment(
        session, admin, patient_id=hospital["patient"].id,
        doctor_id=hospital["doctor"].id, slot_start=slots[0],
    )
    appointment_service.cancel_appointment(session, admin, first.id, "patient request")

    other = patient_service.register_patient(
        session, admin, full_name="Second Patient", date_of_birth=date(1992, 2, 2),
        gender="female", phone="9800000099", consent_treatment=True,
    )
    rebooked = appointment_service.book_appointment(
        session, admin, patient_id=other.id, doctor_id=hospital["doctor"].id, slot_start=slots[0]
    )
    assert rebooked.slot_start == first.slot_start
    assert rebooked.id != first.id


def test_patient_cannot_double_book_same_time(session, admin, hospital):
    from backend.services import directory_service

    day = date.today() + timedelta(days=1)
    slots = appointment_service.available_slots(session, admin, hospital["doctor"].id, day)
    appointment_service.book_appointment(
        session, admin, patient_id=hospital["patient"].id,
        doctor_id=hospital["doctor"].id, slot_start=slots[0],
    )
    other_doctor = directory_service.create_doctor(
        session, admin, code="DOC02", full_name="Dr. Second", specialty="Cardiology",
        department_id=hospital["department"].id,
    )
    with pytest.raises(ConflictError, match="already has an appointment"):
        appointment_service.book_appointment(
            session, admin, patient_id=hospital["patient"].id,
            doctor_id=other_doctor.id, slot_start=slots[0],
        )


