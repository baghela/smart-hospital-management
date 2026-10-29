"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_email(value: Optional[str]) -> Optional[str]:
    """Lightweight email check (avoids the optional email-validator dependency)."""
    if value in (None, ""):
        return None
    if "@" not in value or "." not in value.split("@")[-1] or len(value) > 120:
        raise ValueError("email address is not valid")
    return value


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -----------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    permissions: List[str] = []


# --- patients -------------------------------------------------------------
class PatientCreate(BaseModel):
    full_name: str = Field(min_length=3, max_length=120)
    date_of_birth: date
    gender: str
    phone: str = Field(min_length=8, max_length=24)
    email: Optional[str] = None
    address: Optional[str] = Field(default=None, max_length=255)
    blood_group: Optional[str] = Field(default=None, max_length=8)
    consent_treatment: bool = False
    consent_data_sharing: bool = False

    _email = field_validator("email")(_validate_email)

    @field_validator("gender")
    @classmethod
    def _gender(cls, value: str) -> str:
        allowed = {"male", "female", "other", "undisclosed"}
        if value.lower() not in allowed:
            raise ValueError(f"gender must be one of {sorted(allowed)}")
        return value.lower()


class PatientUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    blood_group: Optional[str] = None
    consent_data_sharing: Optional[bool] = None

    _email = field_validator("email")(_validate_email)


class PatientOut(ORMModel):
    id: int
    patient_uid: str
    full_name: str
    date_of_birth: date
    gender: str
    phone: str
    email: Optional[str] = None
    blood_group: Optional[str] = None
    consent_treatment: bool
    is_active: bool


class PaginatedPatients(BaseModel):
    items: List[PatientOut]
    total: int
    page: int
    page_size: int


# --- directory ------------------------------------------------------------
class DepartmentCreate(BaseModel):
    code: str = Field(min_length=2, max_length=16)
    name: str = Field(min_length=3, max_length=120)
    floor: Optional[str] = None


class DepartmentOut(ORMModel):
    id: int
    code: str
    name: str
    floor: Optional[str] = None


class DoctorCreate(BaseModel):
    code: str = Field(min_length=2, max_length=16)
    full_name: str
    specialty: str
    department_id: int
    consultation_fee: float = 500.0
    slot_minutes: int = 30
    day_start_hour: int = 9
    day_end_hour: int = 17


class DoctorOut(ORMModel):
    id: int
    code: str
    full_name: str
    specialty: str
    department_id: int
    consultation_fee: float
    slot_minutes: int
    is_active: bool


# --- appointments ---------------------------------------------------------
class AppointmentCreate(BaseModel):
    patient_id: int
    doctor_id: int
    slot_start: datetime
    reason: Optional[str] = Field(default=None, max_length=255)


class AppointmentReschedule(BaseModel):
    slot_start: datetime


class AppointmentCancel(BaseModel):
    reason: str = Field(min_length=3, max_length=255)


class EncounterCreate(BaseModel):
    chief_complaint: str = Field(min_length=3, max_length=255)
    observations: Optional[str] = None
    plan: Optional[str] = None


class AppointmentOut(ORMModel):
    id: int
    reference: str
    patient_id: int
    doctor_id: int
    slot_start: datetime
    slot_end: datetime
    status: str
    queue_number: Optional[int] = None
    reason: Optional[str] = None


class SlotsOut(BaseModel):
    doctor_id: int
    date: date
    slots: List[str]


# --- beds / admissions ----------------------------------------------------
class WardCreate(BaseModel):
    code: str
    name: str
    department_id: int
    daily_rate: float = 2000.0
    bed_count: int = Field(default=0, ge=0, le=200)


class AdmissionCreate(BaseModel):
    patient_id: int
    bed_id: int
    reason: str = Field(min_length=3, max_length=255)


class TransferRequest(BaseModel):
    new_bed_id: int


class DischargeRequest(BaseModel):
    summary: str = Field(min_length=20, max_length=4000)


class AdmissionOut(ORMModel):
    id: int
    reference: str
    patient_id: int
    bed_id: int
    status: str
    admitted_at: datetime
    discharged_at: Optional[datetime] = None


# --- billing --------------------------------------------------------------
class InvoiceCreate(BaseModel):
    patient_id: int


class InvoiceItemCreate(BaseModel):
    description: str = Field(min_length=2, max_length=160)
    unit_price: float = Field(ge=0)
    quantity: int = Field(default=1, ge=1, le=999)
    category: str = "service"


class PaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    method: str = "cash"


class RefundCreate(BaseModel):
    amount: float = Field(gt=0)
    reason: str = Field(min_length=3, max_length=255)


class InvoiceOut(ORMModel):
    id: int
    number: str
    patient_id: int
    status: str
    subtotal: float
    tax: float
    total: float
    amount_paid: float
    amount_refunded: float


# --- insurance ------------------------------------------------------------
class PolicyCreate(BaseModel):
    patient_id: int
    payer_name: str
    policy_no: str
    coverage_amount: float = Field(gt=0)
    valid_from: date
    valid_to: date


class ClaimCreate(BaseModel):
    policy_id: int
    invoice_id: int
    notes: str = ""


class ClaimDecisionRequest(BaseModel):
    proposed_amount: float = Field(ge=0)
    rationale: str = Field(min_length=10, max_length=1000)


class ClaimOut(ORMModel):
    id: int
    claim_no: str
    policy_id: int
    invoice_id: int
    status: str
    requested_amount: float
    approved_amount: float


# --- lab / pharmacy -------------------------------------------------------
class LabOrderCreate(BaseModel):
    patient_id: int
    test_code: str


class LabResultCreate(BaseModel):
    value: str = Field(min_length=1, max_length=255)
    notes: str = ""


class PharmacyItemCreate(BaseModel):
    sku: str
    name: str
    unit_price: float = Field(ge=0)
    stock_qty: int = Field(ge=0)
    form: str = "tablet"
    reorder_level: int = 20


class PrescriptionCreate(BaseModel):
    patient_id: int
    doctor_id: int
    item_id: int
    quantity: int = Field(gt=0, le=500)
    dosage: str = "1-0-1"


# --- approvals ------------------------------------------------------------
class ApprovalDecision(BaseModel):
    approved: bool
    notes: str = ""


class ApprovalOut(ORMModel):
    id: int
    action: str
    resource_id: str
    requested_by: str
    requested_at: datetime
    status: str
    summary: str
    decided_by: Optional[str] = None
    decision_notes: Optional[str] = None


# --- copilot / RAG --------------------------------------------------------
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    session_id: str = "default"
    history: List[Dict[str, str]] = []
    provider_order: Optional[List[str]] = None


class SourceOut(BaseModel):
    marker: str
    title: str
    section: str
    page: int
    version: str
    score: float
    preview: str


class AskResponse(BaseModel):
    answer: str
    status: str
    sources: List[SourceOut] = []
    escalation: Optional[Dict[str, Any]] = None
    validation: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    corpus: str = "operational_faq"
    tokens_used: int = 0
    tokens_remaining: int = 0
    elapsed_ms: int = 0
    repair_attempts: int = 0
    tool_calls: List[Dict[str, Any]] = []
    node_trace: List[Dict[str, Any]] = []
    trace_id: str = ""
    engine: str = "langgraph"


class FeedbackRequest(BaseModel):
    question: str
    answer: str
    helpful: bool
    notes: str = ""


class DocumentOut(ORMModel):
    id: int
    title: str
    category: str
    corpus: str
    version: str
    status: str
    effective_date: date
    source_filename: str
    uploaded_by: str


class ErrorResponse(BaseModel):
    error: str
    code: str
    trace_id: Optional[str] = None

