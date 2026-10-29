"""Shared pytest fixtures. Tests run on an isolated SQLite database."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_DB = Path(tempfile.gettempdir()) / "hospital_test.db"

# Tests drop and recreate every table, so they must NEVER inherit a developer's
# DATABASE_URL. Force an isolated database unless one is explicitly supplied
# through HOSPITAL_TEST_DATABASE_URL.
os.environ["DATABASE_URL"] = os.environ.get(
    "HOSPITAL_TEST_DATABASE_URL", f"sqlite:///{TEST_DB.as_posix()}"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "10000")

from backend.auth.rbac import CurrentUser  # noqa: E402
from backend.db.base import Base  # noqa: E402
from backend.db.session import SessionLocal, engine  # noqa: E402
from backend.guardrails.policy import RATE_LIMITER, TOKEN_BUDGET  # noqa: E402
from backend.models import Role  # noqa: E402
import backend.models  # noqa: E402,F401


@pytest.fixture(scope="function")
def session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    RATE_LIMITER.reset()
    TOKEN_BUDGET.reset()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def admin() -> CurrentUser:
    return CurrentUser(username="admin", role=Role.ADMIN)


@pytest.fixture
def doctor() -> CurrentUser:
    return CurrentUser(username="drmehta", role=Role.DOCTOR)


@pytest.fixture
def nurse() -> CurrentUser:
    return CurrentUser(username="nurse1", role=Role.NURSE)


@pytest.fixture
def receptionist() -> CurrentUser:
    return CurrentUser(username="reception1", role=Role.RECEPTIONIST)


@pytest.fixture
def pharmacist() -> CurrentUser:
    return CurrentUser(username="pharma1", role=Role.PHARMACIST)


@pytest.fixture
def billing_user() -> CurrentUser:
    return CurrentUser(username="billing1", role=Role.BILLING)


@pytest.fixture
def hospital(session, admin):
    """A minimal but complete hospital: department, doctor, ward, beds, patient."""
    from backend.services import admission_service, directory_service, patient_service
    from datetime import date

    department = directory_service.create_department(session, admin, code="GEN", name="General Medicine")
    doctor_row = directory_service.create_doctor(
        session, admin, code="DOC01", full_name="Dr. Test", specialty="General",
        department_id=department.id, consultation_fee=500.0,
    )
    admission_service.create_ward(
        session, admin, code="GW1", name="General Ward", department_id=department.id, bed_count=3
    )
    patient = patient_service.register_patient(
        session, admin, full_name="Test Patient", date_of_birth=date(1990, 1, 1),
        gender="male", phone="9800000001", consent_treatment=True,
    )
    return {"department": department, "doctor": doctor_row, "patient": patient}


@pytest.fixture
def faq_corpus(session, admin):
    """Index the synthetic FAQ corpus for retrieval tests."""
    from rag import ingest

    docs = {
        "Hospital Visiting Hours": (
            "GENERAL VISITING HOURS\n"
            "General ward visiting hours are 11:00 to 13:00 and 17:00 to 19:00 every day.\n"
            "A maximum of two visitors are allowed per patient at any one time.\n"
            "ICU VISITING\n"
            "ICU visiting is limited to 17:00 to 18:00 daily and to one attendant per patient.\n"
        ),
        "Appointment Booking Guide": (
            "BOOKING AN APPOINTMENT\n"
            "Appointments can be booked at the reception counter, by phone, or through the portal.\n"
            "Each outpatient consultation slot is 30 minutes long by default.\n"
            "RESCHEDULING\n"
            "An appointment can be rescheduled free of charge up to 2 hours before the scheduled time.\n"
        ),
    }
    for title, text in docs.items():
        ingest.ingest_text(session, admin, title=title, text=text)
    return docs

