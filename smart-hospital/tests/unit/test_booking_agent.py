"""Booking agent: doctor lookup -> slots -> confirmation -> appointment row."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from backend.copilot.booking_agent import (
    STAGE_CONFIRM,
    STAGE_DONE,
    STAGE_SLOT,
    BookingAgent,
    BookingState,
    parse_day,
    parse_time,
)
from backend.models import Appointment
from backend.services import appointment_service


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


def test_parsers():
    today = date(2026, 10, 29)  # Thursday
    assert parse_day("tomorrow", today) == date(2026, 10, 30)
    assert parse_day("on 2026-11-05", today) == date(2026, 11, 5)
    assert parse_day("next monday", today) == date(2026, 11, 2)
    assert parse_day("10.30", today) is None
    assert parse_time("at 10:30") == "10:30"
    assert parse_time("2 pm") == "14:00"
    assert parse_time("Dr Test") is None


def test_full_flow_books_and_blocks_slot(session, receptionist, hospital):
    agent = BookingAgent(session, receptionist)
    state = BookingState()
    patient_id = hospital["patient"].id

    reply = agent.handle("I want to see Dr. Test tomorrow", state, patient_id=patient_id)
    assert reply.state.stage == STAGE_SLOT
    assert reply.state.doctor_id == hospital["doctor"].id
    assert "10:00" in reply.slots

    reply = agent.select_slot("10:00", reply.state)
    assert reply.awaiting_confirmation and reply.state.stage == STAGE_CONFIRM
    assert session.scalars(select(Appointment)).first() is None  # nothing booked before confirmation

    reply = agent.handle("yes", reply.state, patient_id=patient_id, reason="Checkup")
    assert reply.booked and reply.state.stage == STAGE_DONE

    rows = session.scalars(select(Appointment)).all()
    assert len(rows) == 1
    assert rows[0].doctor_id == hospital["doctor"].id
    assert rows[0].slot_start.strftime("%H:%M") == "10:00"
    assert rows[0].status == "booked"

    free = appointment_service.available_slots(session, receptionist, hospital["doctor"].id, _tomorrow())
    assert "10:00" not in [s.strftime("%H:%M") for s in free]


def test_decline_does_not_book(session, receptionist, hospital):
    agent = BookingAgent(session, receptionist)
    reply = agent.handle("dr test tomorrow 11:00", BookingState(), patient_id=hospital["patient"].id)
    assert reply.awaiting_confirmation
    reply = agent.handle("no", reply.state, patient_id=hospital["patient"].id)
    assert reply.state.stage == STAGE_SLOT
    assert session.scalars(select(Appointment)).first() is None


def test_unknown_doctor_lists_options(session, receptionist, hospital):
    reply = BookingAgent(session, receptionist).handle(
        "book with Dr. Nobody", BookingState(), patient_id=hospital["patient"].id
    )
    assert reply.state.stage == "ask_doctor"
    assert "Dr. Test" in reply.message


def test_taken_slot_is_rejected(session, receptionist, hospital):
    agent = BookingAgent(session, receptionist)
    first = agent.handle("Dr Test tomorrow 09:30", BookingState(), patient_id=hospital["patient"].id)
    agent.confirm(first.state, patient_id=hospital["patient"].id)

    reply = agent.handle("Dr Test tomorrow", BookingState(), patient_id=hospital["patient"].id)
    assert "09:30" not in reply.slots
    reply = agent.handle("09:30", reply.state, patient_id=hospital["patient"].id)
    assert not reply.awaiting_confirmation
    assert "not available" in reply.message
