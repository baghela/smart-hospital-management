"""Seed synthetic hospital data and the sample FAQ corpus.

Run with::

    python -m data.seed.seed_data

All data is synthetic. No real patient information is used anywhere.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select

from backend.auth.rbac import CurrentUser
from backend.core.errors import HospitalError
from backend.db.session import create_all, session_scope
from backend.models import Department, Doctor, Patient, Role, User, Ward
from backend.services import (
    admission_service,
    auth_service,
    billing_service,
    directory_service,
    insurance_service,
    lab_service,
    patient_service,
    pharmacy_service,
)
from rag import ingest

FAQ_DIR = Path(__file__).parent / "faqs"
DEFAULT_PASSWORD = "Hospital@123"

SEED_USERS = [
    ("admin", "Asha Admin", Role.ADMIN),
    ("drmehta", "Dr. Rohit Mehta", Role.DOCTOR),
    ("drnair", "Dr. Leena Nair", Role.DOCTOR),
    ("nurse1", "Nisha Nurse", Role.NURSE),
    ("reception1", "Rita Receptionist", Role.RECEPTIONIST),
    ("lab1", "Lalit Lab", Role.LAB),
    ("pharma1", "Priya Pharmacist", Role.PHARMACIST),
    ("billing1", "Bharat Billing", Role.BILLING),
]

DEPARTMENTS = [
    ("GEN", "General Medicine", "1"),
    ("CARD", "Cardiology", "2"),
    ("ORTH", "Orthopaedics", "2"),
    ("PAED", "Paediatrics", "3"),
]

DOCTORS = [
    ("DOC01", "Dr. Rohit Mehta", "General Physician", "GEN", 600.0),
    ("DOC02", "Dr. Leena Nair", "Cardiologist", "CARD", 1200.0),
    ("DOC03", "Dr. Imran Qureshi", "Orthopaedic Surgeon", "ORTH", 1000.0),
    ("DOC04", "Dr. Sneha Rao", "Paediatrician", "PAED", 800.0),
]

PATIENTS = [
    ("Aarav Sharma", date(1988, 4, 12), "male", "9800011001", "O+"),
    ("Diya Patel", date(1995, 9, 2), "female", "9800011002", "A+"),
    ("Kabir Singh", date(1972, 1, 25), "male", "9800011003", "B+"),
    ("Meera Iyer", date(2001, 6, 18), "female", "9800011004", "AB+"),
    ("Rahul Verma", date(1965, 11, 7), "male", "9800011005", "O-"),
    ("Sara Khan", date(1990, 3, 30), "female", "9800011006", "A-"),
]

PHARMACY = [
    ("MED001", "Paracetamol 500mg", 2.5, 500, "tablet"),
    ("MED002", "Amoxicillin 250mg", 8.0, 200, "capsule"),
    ("MED003", "ORS Sachet", 15.0, 150, "sachet"),
    ("MED004", "Metformin 500mg", 3.2, 300, "tablet"),
    ("MED005", "Cough Syrup 100ml", 85.0, 40, "syrup"),
]


def _admin() -> CurrentUser:
    return CurrentUser(username="admin", role=Role.ADMIN)


def seed() -> None:
    create_all()
    admin = _admin()
    random.seed(7)

    with session_scope() as session:
        # --- users ----------------------------------------------------
        for username, full_name, role in SEED_USERS:
            if not session.scalar(select(User).where(User.username == username)):
                auth_service.create_user(
                    session,
                    username=username,
                    full_name=full_name,
                    role=role,
                    password=DEFAULT_PASSWORD,
                )
        session.commit()

        # --- departments & doctors -----------------------------------
        for code, name, floor in DEPARTMENTS:
            if not session.scalar(select(Department).where(Department.code == code)):
                directory_service.create_department(session, admin, code=code, name=name, floor=floor)

        dept_by_code = {d.code: d for d in session.scalars(select(Department))}
        for code, name, specialty, dept_code, fee in DOCTORS:
            if not session.scalar(select(Doctor).where(Doctor.code == code)):
                directory_service.create_doctor(
                    session,
                    admin,
                    code=code,
                    full_name=name,
                    specialty=specialty,
                    department_id=dept_by_code[dept_code].id,
                    consultation_fee=fee,
                )

        # --- wards & beds --------------------------------------------
        if not session.scalar(select(Ward)):
            admission_service.create_ward(
                session, admin, code="GW1", name="General Ward 1",
                department_id=dept_by_code["GEN"].id, daily_rate=1800.0, bed_count=8,
            )
            admission_service.create_ward(
                session, admin, code="ICU", name="Intensive Care Unit",
                department_id=dept_by_code["CARD"].id, daily_rate=6500.0, bed_count=4,
            )
            admission_service.create_ward(
                session, admin, code="PW1", name="Paediatric Ward",
                department_id=dept_by_code["PAED"].id, daily_rate=2200.0, bed_count=6,
            )

        # --- patients -------------------------------------------------
        if not session.scalar(select(Patient)):
            for name, dob, gender, phone, blood in PATIENTS:
                patient_service.register_patient(
                    session,
                    admin,
                    full_name=name,
                    date_of_birth=dob,
                    gender=gender,
                    phone=phone,
                    email=f"{name.split()[0].lower()}@example.com",
                    blood_group=blood,
                    consent_treatment=True,
                    consent_data_sharing=True,
                )

        patients = list(session.scalars(select(Patient)))
        doctors = list(session.scalars(select(Doctor)))

        # --- patient portal logins ------------------------------------
        for patient in patients[:2]:
            username = patient.patient_uid.lower()
            if not session.scalar(select(User).where(User.username == username)):
                auth_service.create_user(
                    session,
                    username=username,
                    full_name=patient.full_name,
                    role=Role.PATIENT,
                    password=DEFAULT_PASSWORD,
                    patient_id=patient.id,
                )
        session.commit()

        # --- appointments ---------------------------------------------
        from backend.services import appointment_service

        tomorrow = date.today() + timedelta(days=1)
        for index, patient in enumerate(patients[:4]):
            doctor = doctors[index % len(doctors)]
            slots = appointment_service.available_slots(session, admin, doctor.id, tomorrow)
            if slots:
                try:
                    appointment_service.book_appointment(
                        session,
                        admin,
                        patient_id=patient.id,
                        doctor_id=doctor.id,
                        slot_start=slots[index % len(slots)],
                        reason="Routine consultation",
                    )
                except HospitalError:
                    pass

        # --- admission -------------------------------------------------
        free_beds = admission_service.available_beds(session, admin)
        if free_beds and not admission_service.list_admissions(session, admin, status="active"):
            admission_service.admit_patient(
                session,
                admin,
                patient_id=patients[2].id,
                bed_id=free_beds[0].id,
                reason="Observation for chest discomfort",
            )

        # --- pharmacy --------------------------------------------------
        for sku, name, price, qty, form in PHARMACY:
            pharmacy_service.upsert_item(
                session, admin, sku=sku, name=name, unit_price=price, stock_qty=qty, form=form
            )

        # --- billing ---------------------------------------------------
        if not billing_service.list_invoices(session, admin):
            invoice = billing_service.create_invoice(session, admin, patient_id=patients[0].id)
            billing_service.add_item(
                session, admin, invoice.id, description="Consultation - General Medicine",
                unit_price=600.0, category="consultation",
            )
            billing_service.add_item(
                session, admin, invoice.id, description="Complete Blood Count",
                unit_price=350.0, category="laboratory",
            )
            billing_service.issue_invoice(session, admin, invoice.id)
            billing_service.record_payment(session, admin, invoice.id, amount=500.0, method="card")

            # --- insurance ---------------------------------------------
            policy = insurance_service.create_policy(
                session,
                admin,
                patient_id=patients[0].id,
                payer_name="SafeCare Health Insurance",
                policy_no="POL-SYN-0001",
                coverage_amount=200000.0,
                valid_from=date.today() - timedelta(days=120),
                valid_to=date.today() + timedelta(days=240),
            )
            insurance_service.create_claim(
                session, admin, policy_id=policy.id, invoice_id=invoice.id, notes="Synthetic seed claim"
            )

        # --- lab -------------------------------------------------------
        if not lab_service.list_orders(session, admin):
            order = lab_service.order_test(session, admin, patient_id=patients[1].id, test_code="CBC")
            lab_service.collect_specimen(session, admin, order.id)

        # --- FAQ corpus ------------------------------------------------
        existing = {d.title for d in ingest.list_documents(session)}
        corpus_map = {
            "visiting_hours.txt": ("Hospital Visiting Hours", "visiting", "operational_faq"),
            "appointments.txt": ("Appointment Booking Guide", "appointments", "operational_faq"),
            "admission_billing.txt": ("Admission, Billing and Discharge", "billing", "operational_faq"),
            "insurance_policy.txt": ("Insurance and Cashless Claims", "insurance", "insurance_policy"),
            "lab_preparation.txt": ("Laboratory Preparation Instructions", "laboratory", "clinical_information"),
        }
        for filename, (title, category, corpus) in corpus_map.items():
            path = FAQ_DIR / filename
            if title in existing or not path.exists():
                continue
            ingest.ingest_document(
                session,
                admin,
                filename=filename,
                data=path.read_bytes(),
                title=title,
                category=category,
                corpus=corpus,
                mime_type="text/plain",
            )

    print("Seed complete.")
    print(f"Sign in with any of: {', '.join(u for u, _n, _r in SEED_USERS)}")
    print(f"Password for all seeded accounts: {DEFAULT_PASSWORD}")


if __name__ == "__main__":
    seed()

