"""ORM models for the hospital domain.

Concurrency guarantees are enforced at the database level:

* ``uq_appointment_doctor_active_slot`` prevents double booking a doctor slot.
  ``active_slot`` mirrors ``slot_start`` while the appointment is active and is
  set to ``NULL`` on cancellation (NULLs are distinct in both PostgreSQL and
  SQLite, so cancelled rows never block a rebooking).
* ``uq_admission_active_bed`` prevents two active admissions on the same bed.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, TimestampMixin
from backend.models.enums import (
    AdmissionStatus,
    AppointmentStatus,
    ApprovalStatus,
    BedStatus,
    ClaimStatus,
    DocumentStatus,
    InvoiceStatus,
    LabOrderStatus,
    PrescriptionStatus,
    Role,
)


# --------------------------------------------------------------------------
# Identity & access
# --------------------------------------------------------------------------
class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32), index=True, default=Role.RECEPTIONIST.value)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Patients may only see their own record.
    patient_id: Mapped[Optional[int]] = mapped_column(ForeignKey("patients.id"), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    actor_role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource: Mapped[str] = mapped_column(String(96))
    resource_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), default="success")
    trace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Free-form, PHI-free detail string.
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------
class Department(Base, TimestampMixin):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    floor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    doctors: Mapped[List["Doctor"]] = relationship(back_populates="department")
    wards: Mapped[List["Ward"]] = relationship(back_populates="department")


class Doctor(Base, TimestampMixin):
    __tablename__ = "doctors"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), index=True)
    specialty: Mapped[str] = mapped_column(String(80))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    consultation_fee: Mapped[float] = mapped_column(Float, default=500.0)
    slot_minutes: Mapped[int] = mapped_column(Integer, default=30)
    day_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    day_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    department: Mapped[Department] = relationship(back_populates="doctors")
    appointments: Mapped[List["Appointment"]] = relationship(back_populates="doctor")


class Patient(Base, TimestampMixin):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(primary_key=True)
    patient_uid: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), index=True)
    date_of_birth: Mapped[date] = mapped_column(Date)
    gender: Mapped[str] = mapped_column(String(16))
    phone: Mapped[str] = mapped_column(String(24), index=True)
    email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    blood_group: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    consent_treatment: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_data_sharing: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_recorded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    appointments: Mapped[List["Appointment"]] = relationship(back_populates="patient")
    admissions: Mapped[List["Admission"]] = relationship(back_populates="patient")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="patient")


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------
class Appointment(Base, TimestampMixin):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("doctor_id", "active_slot", name="uq_appointment_doctor_active_slot"),
        Index("ix_appointment_patient_slot", "patient_id", "slot_start"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"), index=True)
    slot_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    slot_end: Mapped[datetime] = mapped_column(DateTime)
    active_slot: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=AppointmentStatus.BOOKED.value, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    queue_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    checked_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="appointments")
    doctor: Mapped[Doctor] = relationship(back_populates="appointments")
    encounter: Mapped[Optional["Encounter"]] = relationship(back_populates="appointment", uselist=False)


class Encounter(Base, TimestampMixin):
    """OPD clinical documentation; drafts always require human sign-off."""

    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"), unique=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    chief_complaint: Mapped[str] = mapped_column(String(255))
    observations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    plan: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    appointment: Mapped[Appointment] = relationship(back_populates="encounter")


# --------------------------------------------------------------------------
# Inpatient
# --------------------------------------------------------------------------
class Ward(Base, TimestampMixin):
    __tablename__ = "wards"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    daily_rate: Mapped[float] = mapped_column(Float, default=2000.0)

    department: Mapped[Department] = relationship(back_populates="wards")
    beds: Mapped[List["Bed"]] = relationship(back_populates="ward")


class Bed(Base, TimestampMixin):
    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "label", name="uq_bed_ward_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"), index=True)
    label: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default=BedStatus.AVAILABLE.value, index=True)

    ward: Mapped[Ward] = relationship(back_populates="beds")


class Admission(Base, TimestampMixin):
    __tablename__ = "admissions"
    __table_args__ = (UniqueConstraint("active_bed_id", name="uq_admission_active_bed"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    bed_id: Mapped[int] = mapped_column(ForeignKey("beds.id"), index=True)
    active_bed_id: Mapped[Optional[int]] = mapped_column(ForeignKey("beds.id"), nullable=True)
    admitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    discharged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default=AdmissionStatus.ACTIVE.value, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    discharge_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discharge_approved_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="admissions")
    transfers: Mapped[List["BedTransfer"]] = relationship(back_populates="admission")


class BedTransfer(Base):
    __tablename__ = "bed_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    from_bed_id: Mapped[int] = mapped_column(ForeignKey("beds.id"))
    to_bed_id: Mapped[int] = mapped_column(ForeignKey("beds.id"))
    moved_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    moved_by: Mapped[str] = mapped_column(String(64))

    admission: Mapped[Admission] = relationship(back_populates="transfers")


# --------------------------------------------------------------------------
# Lab & pharmacy
# --------------------------------------------------------------------------
class LabOrder(Base, TimestampMixin):
    __tablename__ = "lab_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    ordered_by: Mapped[str] = mapped_column(String(64))
    test_code: Mapped[str] = mapped_column(String(32), index=True)
    test_name: Mapped[str] = mapped_column(String(120))
    price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default=LabOrderStatus.ORDERED.value, index=True)
    specimen_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    result_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resulted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class PharmacyItem(Base, TimestampMixin):
    __tablename__ = "pharmacy_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    form: Mapped[str] = mapped_column(String(32), default="tablet")
    unit_price: Mapped[float] = mapped_column(Float, default=10.0)
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=20)


class Prescription(Base, TimestampMixin):
    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    doctor_id: Mapped[int] = mapped_column(ForeignKey("doctors.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("pharmacy_items.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    dosage: Mapped[str] = mapped_column(String(120), default="1-0-1")
    status: Mapped[str] = mapped_column(String(24), default=PrescriptionStatus.ACTIVE.value, index=True)
    dispensed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    dispensed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    item: Mapped[PharmacyItem] = relationship()


# --------------------------------------------------------------------------
# Finance
# --------------------------------------------------------------------------
class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default=InvoiceStatus.DRAFT.value, index=True)
    subtotal: Mapped[float] = mapped_column(Float, default=0.0)
    tax: Mapped[float] = mapped_column(Float, default=0.0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    amount_paid: Mapped[float] = mapped_column(Float, default=0.0)
    amount_refunded: Mapped[float] = mapped_column(Float, default=0.0)
    issued_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    patient: Mapped[Patient] = relationship(back_populates="invoices")
    items: Mapped[List["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )
    payments: Mapped[List["Payment"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    description: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(32), default="service")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    line_total: Mapped[float] = mapped_column(Float, default=0.0)

    invoice: Mapped[Invoice] = relationship(back_populates="items")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    receipt_no: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    amount: Mapped[float] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(24), default="cash")
    is_refund: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_by: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    invoice: Mapped[Invoice] = relationship(back_populates="payments")


class InsurancePolicy(Base, TimestampMixin):
    __tablename__ = "insurance_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    payer_name: Mapped[str] = mapped_column(String(120))
    coverage_amount: Mapped[float] = mapped_column(Float, default=100000.0)
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date] = mapped_column(Date)


class Claim(Base, TimestampMixin):
    __tablename__ = "claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("insurance_policies.id"), index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default=ClaimStatus.DRAFT.value, index=True)
    requested_amount: Mapped[float] = mapped_column(Float, default=0.0)
    approved_amount: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    policy: Mapped[InsurancePolicy] = relationship()


# --------------------------------------------------------------------------
# Human-in-the-loop (narrow scope)
# --------------------------------------------------------------------------
class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(48), index=True)
    resource_id: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(64), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(24), default=ApprovalStatus.PENDING.value, index=True)
    summary: Mapped[str] = mapped_column(String(500))
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decided_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    decision_notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)


# --------------------------------------------------------------------------
# RAG corpus
# --------------------------------------------------------------------------
class FaqDocument(Base, TimestampMixin):
    __tablename__ = "faq_documents"
    __table_args__ = (UniqueConstraint("title", "version", name="uq_faq_title_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str] = mapped_column(String(48), index=True, default="operational")
    corpus: Mapped[str] = mapped_column(String(32), index=True, default="operational_faq")
    version: Mapped[str] = mapped_column(String(16), default="1.0")
    status: Mapped[str] = mapped_column(String(24), default=DocumentStatus.APPROVED.value, index=True)
    effective_date: Mapped[date] = mapped_column(Date, default=date.today)
    source_filename: Mapped[str] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_by: Mapped[str] = mapped_column(String(64))

    chunks: Mapped[List["FaqChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class FaqChunk(Base):
    __tablename__ = "faq_chunks"
    __table_args__ = (Index("ix_faq_chunk_doc_idx", "document_id", "chunk_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("faq_documents.id"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    section: Mapped[str] = mapped_column(String(160), default="General")
    page: Mapped[int] = mapped_column(Integer, default=1)
    content: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0)
    # Embedding stored as JSON text so the app runs on stock PostgreSQL;
    # pgvector is listed as a production-hardening step in the README.
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    document: Mapped[FaqDocument] = relationship(back_populates="chunks")


class ChatFeedback(Base):
    __tablename__ = "chat_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    username: Mapped[str] = mapped_column(String(64), index=True)
    question: Mapped[str] = mapped_column(String(500))
    answer_preview: Mapped[str] = mapped_column(String(500))
    helpful: Mapped[bool] = mapped_column(Boolean)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

