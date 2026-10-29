"""Concurrent appointment slot and bed allocation tests."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.core.errors import ConflictError
from backend.db.session import SessionLocal
from backend.services import admission_service, appointment_service, patient_service


def _new_patient(session, admin, name: str, phone: str):
    return patient_service.register_patient(
        session, admin, full_name=name, date_of_birth=date(1990, 1, 1),
        gender="male", phone=phone, consent_treatment=True,
    )


def test_two_sessions_cannot_take_the_same_slot(session, admin, hospital):
    """The unique constraint must stop a double booking across sessions."""
    day = date.today() + timedelta(days=1)
    slot = appointment_service.available_slots(session, admin, hospital["doctor"].id, day)[0]
    doctor_id = hospital["doctor"].id
    patient_a = hospital["patient"].id
    patient_b = _new_patient(session, admin, "Racer B", "9800000031").id

    appointment_service.book_appointment(
        session, admin, patient_id=patient_a, doctor_id=doctor_id, slot_start=slot
    )

    other = SessionLocal()
    try:
        with pytest.raises(ConflictError):
            appointment_service.book_appointment(
                other, admin, patient_id=patient_b, doctor_id=doctor_id, slot_start=slot
            )
    finally:
        other.close()


def test_slot_disappears_from_availability_after_booking(session, admin, hospital):
    day = date.today() + timedelta(days=1)
    before = appointment_service.available_slots(session, admin, hospital["doctor"].id, day)
    appointment_service.book_appointment(
        session, admin, patient_id=hospital["patient"].id,
        doctor_id=hospital["doctor"].id, slot_start=before[0],
    )
    after = appointment_service.available_slots(session, admin, hospital["doctor"].id, day)
    assert before[0] not in after
    assert len(after) == len(before) - 1


def test_two_sessions_cannot_take_the_same_bed(session, admin, hospital):
    bed = admission_service.available_beds(session, admin)[0]
    patient_b = _new_patient(session, admin, "Bed Racer", "9800000032").id

    admission_service.admit_patient(
        session, admin, patient_id=hospital["patient"].id, bed_id=bed.id, reason="first"
    )

    other = SessionLocal()
    try:
        with pytest.raises(ConflictError):
            admission_service.admit_patient(
                other, admin, patient_id=patient_b, bed_id=bed.id, reason="second"
            )
    finally:
        other.close()


def test_patient_cannot_have_two_active_admissions(session, admin, hospital):
    beds = admission_service.available_beds(session, admin)
    admission_service.admit_patient(
        session, admin, patient_id=hospital["patient"].id, bed_id=beds[0].id, reason="first"
    )
    with pytest.raises(ConflictError, match="active admission"):
        admission_service.admit_patient(
            session, admin, patient_id=hospital["patient"].id, bed_id=beds[1].id, reason="second"
        )


def test_transfer_releases_the_old_bed(session, admin, hospital):
    beds = admission_service.available_beds(session, admin)
    admission = admission_service.admit_patient(
        session, admin, patient_id=hospital["patient"].id, bed_id=beds[0].id, reason="obs"
    )
    admission_service.transfer_bed(session, admin, admission.id, beds[1].id)

    board = {row["bed_id"]: row["status"] for row in admission_service.bed_board(session, admin)}
    assert board[beds[0].id] == "available"
    assert board[beds[1].id] == "occupied"


def test_bed_availability_summary_counts_match_board(session, admin, hospital):
    beds = admission_service.available_beds(session, admin)
    admission_service.admit_patient(
        session, admin, patient_id=hospital["patient"].id, bed_id=beds[0].id, reason="obs"
    )
    summary = admission_service.bed_availability_summary(session)
    occupied = sum(row.get("occupied", 0) for row in summary)
    assert occupied == 1

