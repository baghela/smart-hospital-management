"""Authentication, RBAC and patient-scope tests."""
from __future__ import annotations

from datetime import date

import pytest

from backend.auth.rbac import CurrentUser
from backend.auth.security import create_token, decode_token, hash_password, verify_password
from backend.core.errors import AuthenticationError, PermissionDeniedError
from backend.models import Role
from backend.services import auth_service, billing_service, patient_service


def test_password_hash_roundtrip():
    stored = hash_password("Hospital@123")
    assert stored != "Hospital@123"
    assert verify_password("Hospital@123", stored)
    assert not verify_password("wrong-password", stored)


def test_token_roundtrip_and_tampering():
    token = create_token("admin", "admin")
    payload = decode_token(token)
    assert payload["sub"] == "admin" and payload["role"] == "admin"

    body, signature = token.split(".")
    with pytest.raises(AuthenticationError):
        decode_token(f"{body}x.{signature}")


def test_authenticate_rejects_bad_credentials(session):
    auth_service.create_user(
        session, username="tester", full_name="Tester", role=Role.RECEPTIONIST, password="Hospital@123"
    )
    session.commit()
    with pytest.raises(AuthenticationError):
        auth_service.authenticate(session, "tester", "wrong-password")
    user, token = auth_service.authenticate(session, "tester", "Hospital@123")
    assert user.username == "tester" and token


def test_lab_user_cannot_write_patients(session, hospital):
    lab_user = CurrentUser(username="lab1", role=Role.LAB)
    with pytest.raises(PermissionDeniedError, match="patient:write"):
        patient_service.register_patient(
            session, lab_user, full_name="Blocked Person", date_of_birth=date(1990, 1, 1),
            gender="male", phone="9800000041", consent_treatment=True,
        )


def test_receptionist_cannot_write_billing(session, hospital, receptionist):
    with pytest.raises(PermissionDeniedError, match="billing:write"):
        billing_service.create_invoice(session, receptionist, patient_id=hospital["patient"].id)


def test_patient_cannot_read_another_patient(session, admin, hospital):
    other = patient_service.register_patient(
        session, admin, full_name="Other Patient", date_of_birth=date(1995, 3, 3),
        gender="female", phone="9800000042", consent_treatment=True,
    )
    patient_user = CurrentUser(username="pat1", role=Role.PATIENT, patient_id=hospital["patient"].id)

    own = patient_service.get_patient(session, patient_user, hospital["patient"].id)
    assert own.id == hospital["patient"].id

    with pytest.raises(PermissionDeniedError, match="own records"):
        patient_service.get_patient(session, patient_user, other.id)


def test_permission_matrix_is_role_scoped():
    admin = CurrentUser(username="a", role=Role.ADMIN)
    pharmacist = CurrentUser(username="p", role=Role.PHARMACIST)
    assert admin.has("audit:read")
    assert not pharmacist.has("audit:read")
    assert pharmacist.has("pharmacy:write")


def test_approver_roles_are_enforced():
    pharmacist = CurrentUser(username="p", role=Role.PHARMACIST)
    assert pharmacist.can_approve("pharmacy_dispense")
    assert not pharmacist.can_approve("discharge")
    with pytest.raises(PermissionDeniedError):
        pharmacist.require_approver("discharge")

