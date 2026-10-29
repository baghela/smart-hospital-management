"""Integration tests for the narrow human-in-the-loop approval workflow."""
from __future__ import annotations

from datetime import date

import pytest

from backend.auth.rbac import CurrentUser
from backend.core.errors import ApprovalRequiredError, PermissionDeniedError, ValidationError
from backend.models import ApprovalStatus, Role
from backend.services import (
    admission_service,
    approval_service,
    billing_service,
    insurance_service,
    pharmacy_service,
)

SUMMARY = "Patient stable, vitals normal, discharged with oral medication and follow-up in one week."


@pytest.fixture
def admitted(session, admin, hospital):
    bed = admission_service.available_beds(session, admin)[0]
    return admission_service.admit_patient(
        session, admin, patient_id=hospital["patient"].id, bed_id=bed.id, reason="observation"
    )


# --------------------------------------------------------------------------
# Discharge
# --------------------------------------------------------------------------
def test_discharge_without_approval_is_blocked(session, doctor, admitted):
    with pytest.raises(ApprovalRequiredError, match="sign-off"):
        admission_service.complete_discharge(session, doctor, admitted.id)
    assert admitted.status == "active"


def test_discharge_two_step_flow(session, nurse, doctor, admitted):
    approval_id = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    approval = approval_service.get_approval(session, approval_id)
    assert approval.status == ApprovalStatus.PENDING.value

    approval_service.decide(session, doctor, approval_id, approved=True, notes="Reviewed and agreed")
    discharged = admission_service.complete_discharge(session, nurse, admitted.id)

    assert discharged.status == "discharged"
    assert discharged.discharge_summary == SUMMARY
    assert discharged.discharge_approved_by == doctor.username
    board = {row["bed_id"]: row["status"] for row in admission_service.bed_board(session, nurse)}
    assert board[discharged.bed_id] == "available"


def test_rejected_discharge_does_not_execute(session, nurse, doctor, admitted):
    approval_id = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    approval_service.decide(session, doctor, approval_id, approved=False, notes="Vitals still unstable")
    with pytest.raises(ApprovalRequiredError):
        admission_service.complete_discharge(session, nurse, admitted.id)


def test_short_discharge_summary_rejected(session, nurse, admitted):
    with pytest.raises(ValidationError, match="20 characters"):
        admission_service.request_discharge(session, nurse, admitted.id, summary="too short")


def test_requester_cannot_approve_own_request(session, doctor, admitted):
    approval_id = admission_service.request_discharge(session, doctor, admitted.id, summary=SUMMARY)
    with pytest.raises(ValidationError, match="same person"):
        approval_service.decide(session, doctor, approval_id, approved=True, notes="self approve")


def test_wrong_role_cannot_approve_discharge(session, nurse, pharmacist, admitted):
    approval_id = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    with pytest.raises(PermissionDeniedError, match="cannot approve"):
        approval_service.decide(session, pharmacist, approval_id, approved=True, notes="ok")


def test_rejection_requires_a_reason(session, nurse, doctor, admitted):
    approval_id = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    with pytest.raises(ValidationError, match="reason"):
        approval_service.decide(session, doctor, approval_id, approved=False, notes="")


def test_decision_cannot_be_repeated(session, nurse, doctor, admitted):
    approval_id = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    approval_service.decide(session, doctor, approval_id, approved=True, notes="ok")
    with pytest.raises(ValidationError, match="already been decided"):
        approval_service.decide(session, doctor, approval_id, approved=True, notes="again")


def test_duplicate_requests_reuse_the_same_ticket(session, nurse, admitted):
    first = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    second = admission_service.request_discharge(session, nurse, admitted.id, summary=SUMMARY)
    assert first == second


# --------------------------------------------------------------------------
# Pharmacy dispensing
# --------------------------------------------------------------------------
def test_dispense_requires_pharmacist_signoff(session, admin, doctor, pharmacist, hospital):
    item = pharmacy_service.upsert_item(
        session, admin, sku="MED001", name="Paracetamol", unit_price=2.5, stock_qty=100
    )
    rx = pharmacy_service.create_prescription(
        session, doctor, patient_id=hospital["patient"].id, doctor_id=hospital["doctor"].id,
        item_id=item.id, quantity=10, dosage="1-0-1",
    )

    with pytest.raises(ApprovalRequiredError, match="pharmacist sign-off"):
        pharmacy_service.complete_dispense(session, pharmacist, rx.id)

    approval_id = pharmacy_service.request_dispense(session, doctor, rx.id)
    approval_service.decide(session, pharmacist, approval_id, approved=True, notes="stock verified")
    dispensed = pharmacy_service.complete_dispense(session, pharmacist, rx.id)

    assert dispensed.status == "dispensed"
    assert item.stock_qty == 90


def test_dispense_blocked_when_stock_insufficient(session, admin, doctor, hospital):
    item = pharmacy_service.upsert_item(
        session, admin, sku="MED002", name="Rare Drug", unit_price=50.0, stock_qty=2
    )
    rx = pharmacy_service.create_prescription(
        session, doctor, patient_id=hospital["patient"].id, doctor_id=hospital["doctor"].id,
        item_id=item.id, quantity=10, dosage="1-0-1",
    )
    with pytest.raises(Exception, match="Insufficient stock"):
        pharmacy_service.request_dispense(session, doctor, rx.id)


def test_only_doctor_can_prescribe(session, admin, nurse, hospital):
    item = pharmacy_service.upsert_item(
        session, admin, sku="MED003", name="ORS", unit_price=15.0, stock_qty=50
    )
    with pytest.raises(Exception):
        pharmacy_service.create_prescription(
            session, nurse, patient_id=hospital["patient"].id, doctor_id=hospital["doctor"].id,
            item_id=item.id, quantity=1, dosage="1-0-1",
        )


# --------------------------------------------------------------------------
# Claim decisions
# --------------------------------------------------------------------------
def test_claim_decision_requires_billing_signoff(session, admin, billing_user, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Procedure", unit_price=5000.0)
    billing_service.issue_invoice(session, admin, invoice.id)
    policy = insurance_service.create_policy(
        session, admin, patient_id=hospital["patient"].id, payer_name="SafeCare",
        policy_no="POL-TEST-1", coverage_amount=100000.0,
        valid_from=date(2020, 1, 1), valid_to=date(2099, 1, 1),
    )
    claim = insurance_service.create_claim(session, admin, policy_id=policy.id, invoice_id=invoice.id)

    with pytest.raises(ApprovalRequiredError, match="billing sign-off"):
        insurance_service.apply_decision(session, admin, claim.id)

    approval_id = insurance_service.request_decision(
        session, admin, claim.id, proposed_amount=4000.0, rationale="Covered as per policy terms"
    )
    approval_service.decide(session, billing_user, approval_id, approved=True, notes="verified")
    decided = insurance_service.apply_decision(session, admin, claim.id)

    assert decided.status == "approved"
    assert decided.approved_amount == 4000.0
    assert decided.decided_by == billing_user.username


def test_claim_eligibility_blocks_expired_policy(session, admin, hospital):
    invoice = billing_service.create_invoice(session, admin, patient_id=hospital["patient"].id)
    billing_service.add_item(session, admin, invoice.id, description="Procedure", unit_price=1000.0)
    policy = insurance_service.create_policy(
        session, admin, patient_id=hospital["patient"].id, payer_name="OldCare",
        policy_no="POL-OLD-1", coverage_amount=50000.0,
        valid_from=date(2000, 1, 1), valid_to=date(2001, 1, 1),
    )
    with pytest.raises(ValidationError, match="not active"):
        insurance_service.create_claim(session, admin, policy_id=policy.id, invoice_id=invoice.id)


def test_non_gated_action_cannot_request_approval(session, admin):
    with pytest.raises(ValidationError, match="not a gated action"):
        approval_service.request_approval(
            session, admin, action="appointment_book", resource_id=1, summary="should fail"
        )


def test_patient_role_cannot_view_approvals(session):
    patient_user = CurrentUser(username="pat1", role=Role.PATIENT, patient_id=1)
    with pytest.raises(PermissionDeniedError):
        approval_service.list_approvals(session, patient_user)

