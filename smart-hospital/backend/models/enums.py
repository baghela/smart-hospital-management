"""Domain enumerations used across models, services and guardrails."""
from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    ADMIN = "admin"
    DOCTOR = "doctor"
    NURSE = "nurse"
    RECEPTIONIST = "receptionist"
    LAB = "lab"
    PHARMACIST = "pharmacist"
    BILLING = "billing"
    PATIENT = "patient"


class AppointmentStatus(str, Enum):
    BOOKED = "booked"
    CHECKED_IN = "checked_in"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class BedStatus(str, Enum):
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    MAINTENANCE = "maintenance"


class AdmissionStatus(str, Enum):
    ACTIVE = "active"
    DISCHARGED = "discharged"


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    REFUNDED = "refunded"


class ClaimStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    INFO_REQUESTED = "info_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


class LabOrderStatus(str, Enum):
    ORDERED = "ordered"
    COLLECTED = "collected"
    IN_PROGRESS = "in_progress"
    RESULTED = "resulted"
    CANCELLED = "cancelled"


class PrescriptionStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISPENSED = "dispensed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalAction(str, Enum):
    """Narrow human-in-the-loop scope: only these actions need sign-off."""

    DISCHARGE = "discharge"
    CLAIM_DECISION_SUPPORT = "claim_decision_support"
    PHARMACY_DISPENSE = "pharmacy_dispense"


class DocumentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    ARCHIVED = "archived"

