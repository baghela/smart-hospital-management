"""Itemized billing, invoices, payments, refunds and receipts.

All money math is deterministic Python - never delegated to an LLM.
"""
from __future__ import annotations

from typing import Iterable, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser, enforce_patient_scope
from backend.core.errors import NotFoundError, ValidationError
from backend.models import Invoice, InvoiceItem, InvoiceStatus, Patient, Payment
from backend.services.common import next_reference, utcnow

TAX_RATE = 0.05  # 5% hospital service tax, configured centrally


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def recalculate(invoice: Invoice) -> Invoice:
    subtotal = _round(sum(item.line_total for item in invoice.items))
    invoice.subtotal = subtotal
    invoice.tax = _round(subtotal * TAX_RATE)
    invoice.total = _round(invoice.subtotal + invoice.tax)
    return invoice


def create_invoice(session: Session, user: CurrentUser, *, patient_id: int) -> Invoice:
    user.require("billing:write")
    if not session.get(Patient, patient_id):
        raise NotFoundError(f"Patient {patient_id} not found.")
    invoice = Invoice(
        number=next_reference(session, Invoice, Invoice.number, "INV"),
        patient_id=patient_id,
        status=InvoiceStatus.DRAFT.value,
    )
    session.add(invoice)
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="invoice_create",
        resource="invoice",
        resource_id=invoice.id,
    )
    session.commit()
    return invoice


def add_item(
    session: Session,
    user: CurrentUser,
    invoice_id: int,
    *,
    description: str,
    unit_price: float,
    quantity: int = 1,
    category: str = "service",
) -> Invoice:
    user.require("billing:write")
    invoice = get_invoice(session, user, invoice_id)
    if invoice.status not in (InvoiceStatus.DRAFT.value, InvoiceStatus.ISSUED.value):
        raise ValidationError("Items cannot be added to a settled invoice.")
    if quantity <= 0:
        raise ValidationError("Quantity must be positive.")
    if unit_price < 0:
        raise ValidationError("Unit price cannot be negative.")

    invoice.items.append(
        InvoiceItem(
            description=description.strip()[:160],
            category=category,
            quantity=quantity,
            unit_price=_round(unit_price),
            line_total=_round(unit_price * quantity),
        )
    )
    session.flush()
    recalculate(invoice)
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="invoice_add_item",
        resource="invoice",
        resource_id=invoice.id,
    )
    session.commit()
    return invoice


def add_items(session: Session, user: CurrentUser, invoice_id: int, items: Iterable[dict]) -> Invoice:
    invoice = None
    for item in items:
        invoice = add_item(session, user, invoice_id, **item)
    if invoice is None:
        raise ValidationError("No invoice items supplied.")
    return invoice


def issue_invoice(session: Session, user: CurrentUser, invoice_id: int) -> Invoice:
    user.require("billing:write")
    invoice = get_invoice(session, user, invoice_id)
    if not invoice.items:
        raise ValidationError("Cannot issue an invoice with no items.")
    if invoice.status != InvoiceStatus.DRAFT.value:
        raise ValidationError("Only draft invoices can be issued.")
    recalculate(invoice)
    invoice.status = InvoiceStatus.ISSUED.value
    invoice.issued_at = utcnow()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="invoice_issue",
        resource="invoice",
        resource_id=invoice.id,
    )
    session.commit()
    return invoice


def record_payment(
    session: Session,
    user: CurrentUser,
    invoice_id: int,
    *,
    amount: float,
    method: str = "cash",
) -> Payment:
    user.require("billing:write")
    invoice = get_invoice(session, user, invoice_id)
    if invoice.status == InvoiceStatus.DRAFT.value:
        raise ValidationError("Issue the invoice before accepting payment.")
    if amount <= 0:
        raise ValidationError("Payment amount must be positive.")
    outstanding = _round(invoice.total - invoice.amount_paid + invoice.amount_refunded)
    if amount > outstanding + 0.01:
        raise ValidationError(f"Payment exceeds the outstanding balance of {outstanding:.2f}.")

    payment = Payment(
        invoice_id=invoice.id,
        receipt_no=next_reference(session, Payment, Payment.receipt_no, "RCPT", 6),
        amount=_round(amount),
        method=method,
        recorded_by=user.username,
        recorded_at=utcnow(),
    )
    invoice.payments.append(payment)
    invoice.amount_paid = _round(invoice.amount_paid + amount)
    invoice.status = (
        InvoiceStatus.PAID.value
        if invoice.amount_paid + 0.01 >= invoice.total
        else InvoiceStatus.PARTIALLY_PAID.value
    )
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="payment_record",
        resource="invoice",
        resource_id=invoice.id,
    )
    session.commit()
    return payment


def refund(
    session: Session, user: CurrentUser, invoice_id: int, *, amount: float, reason: str
) -> Payment:
    user.require("billing:write")
    invoice = get_invoice(session, user, invoice_id)
    if amount <= 0:
        raise ValidationError("Refund amount must be positive.")
    refundable = _round(invoice.amount_paid - invoice.amount_refunded)
    if amount > refundable + 0.01:
        raise ValidationError(f"Refund exceeds the refundable amount of {refundable:.2f}.")
    if not reason.strip():
        raise ValidationError("A refund reason is required.")

    entry = Payment(
        invoice_id=invoice.id,
        receipt_no=next_reference(session, Payment, Payment.receipt_no, "RCPT", 6),
        amount=_round(amount),
        method="refund",
        is_refund=True,
        recorded_by=user.username,
        recorded_at=utcnow(),
    )
    invoice.payments.append(entry)
    invoice.amount_refunded = _round(invoice.amount_refunded + amount)
    if invoice.amount_refunded + 0.01 >= invoice.amount_paid:
        invoice.status = InvoiceStatus.REFUNDED.value
    session.flush()
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="payment_refund",
        resource="invoice",
        resource_id=invoice.id,
        detail=reason,
    )
    session.commit()
    return entry


def get_invoice(session: Session, user: CurrentUser, invoice_id: int) -> Invoice:
    user.require("billing:read")
    invoice = session.get(Invoice, invoice_id)
    if not invoice:
        raise NotFoundError(f"Invoice {invoice_id} not found.")
    enforce_patient_scope(user, invoice.patient_id)
    return invoice


def get_by_number(session: Session, user: CurrentUser, number: str) -> Invoice:
    user.require("billing:read")
    invoice = session.scalar(select(Invoice).where(Invoice.number == number.strip().upper()))
    if not invoice:
        raise NotFoundError(f"Invoice '{number}' not found.")
    enforce_patient_scope(user, invoice.patient_id)
    return invoice


def list_invoices(
    session: Session, user: CurrentUser, *, patient_id: int | None = None, status: str | None = None
) -> List[Invoice]:
    user.require("billing:read")
    stmt = select(Invoice)
    if user.role.value == "patient":
        patient_id = user.patient_id
    if patient_id:
        enforce_patient_scope(user, patient_id)
        stmt = stmt.where(Invoice.patient_id == patient_id)
    if status:
        stmt = stmt.where(Invoice.status == status)
    return list(session.scalars(stmt.order_by(Invoice.id.desc())))


def invoice_summary(invoice: Invoice) -> dict:
    """Compact, PHI-light summary used by the copilot tool."""
    return {
        "invoice_no": invoice.number,
        "status": invoice.status,
        "subtotal": invoice.subtotal,
        "tax": invoice.tax,
        "total": invoice.total,
        "paid": invoice.amount_paid,
        "refunded": invoice.amount_refunded,
        "outstanding": _round(invoice.total - invoice.amount_paid + invoice.amount_refunded),
        "line_items": [
            {"description": i.description, "qty": i.quantity, "amount": i.line_total}
            for i in invoice.items
        ],
    }


def receipt_text(invoice: Invoice, payment: Payment) -> str:
    lines = [
        "SMART HOSPITAL - PAYMENT RECEIPT",
        f"Receipt   : {payment.receipt_no}",
        f"Invoice   : {invoice.number}",
        f"Date      : {payment.recorded_at:%Y-%m-%d %H:%M}",
        f"Method    : {payment.method}",
        f"Amount    : {payment.amount:.2f}",
        f"Invoice total {invoice.total:.2f} | paid {invoice.amount_paid:.2f}",
    ]
    return "\n".join(lines)

