"""Narrowly scoped, read-only copilot tools.

Every tool enforces RBAC + patient scoping, returns compact JSON-ready dicts,
and never performs a mutation. Mutations go through authenticated backend APIs
with explicit approval where required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.errors import HospitalError, PermissionDeniedError
from backend.services import (
    admission_service,
    appointment_service,
    billing_service,
    insurance_service,
)
from rag import retriever
from rag.citations import validate_answer

TOOL_NAMES = [
    "search_hospital_faq",
    "get_appointment_availability",
    "get_patient_appointments",
    "get_bed_availability",
    "get_invoice_summary",
    "get_claim_status",
    "validate_retrieved_evidence",
]


@dataclass
class ToolSpec:
    name: str
    description: str
    permission: str
    handler: Callable[..., Dict[str, Any]]
    args: Dict[str, str]


class ToolRegistry:
    """Registry with explicit authorisation and audit on every invocation."""

    def __init__(self, session: Session, user: CurrentUser, trace_id: str = "") -> None:
        self.session = session
        self.user = user
        self.trace_id = trace_id
        self.calls: List[dict] = []
        self._specs: Dict[str, ToolSpec] = {spec.name: spec for spec in self._build()}

    # -- introspection -------------------------------------------------
    def available(self) -> List[ToolSpec]:
        return [spec for spec in self._specs.values() if self.user.has(spec.permission)]

    def describe(self) -> List[dict]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "args": spec.args,
                "permitted": self.user.has(spec.permission),
            }
            for spec in self._specs.values()
        ]

    # -- invocation ----------------------------------------------------
    def invoke(self, name: str, **kwargs) -> Dict[str, Any]:
        spec = self._specs.get(name)
        if not spec:
            raise PermissionDeniedError(f"Unknown tool '{name}'.")
        if not self.user.has(spec.permission):
            audit.record(
                self.session,
                actor=self.user.username,
                actor_role=self.user.role.value,
                action="tool_call_denied",
                resource=name,
                outcome="denied",
                trace_id=self.trace_id,
            )
            self.session.commit()
            raise PermissionDeniedError(f"Role '{self.user.role.value}' cannot use '{name}'.")

        try:
            result = spec.handler(**kwargs)
            outcome = "success"
        except HospitalError as exc:
            result = {"error": exc.message, "code": exc.code}
            outcome = "error"

        audit.record(
            self.session,
            actor=self.user.username,
            actor_role=self.user.role.value,
            action="tool_call",
            resource=name,
            outcome=outcome,
            trace_id=self.trace_id,
        )
        self.session.commit()
        self.calls.append({"tool": name, "args": kwargs, "outcome": outcome})
        return result

    # -- tool implementations -----------------------------------------
    def _build(self) -> List[ToolSpec]:
        return [
            ToolSpec(
                "search_hospital_faq",
                "Search approved hospital FAQ documents and return cited passages.",
                "faq:ask",
                self._search_hospital_faq,
                {"query": "str", "corpus": "str (operational_faq|insurance_policy|clinical_information)"},
            ),
            ToolSpec(
                "get_appointment_availability",
                "List free appointment slots for a doctor on a date (YYYY-MM-DD).",
                "appointment:read",
                self._get_appointment_availability,
                {"doctor_id": "int", "day": "str"},
            ),
            ToolSpec(
                "get_patient_appointments",
                "List appointments for a patient the caller is authorised to see.",
                "appointment:read",
                self._get_patient_appointments,
                {"patient_id": "int"},
            ),
            ToolSpec(
                "get_bed_availability",
                "Summarise bed availability per ward.",
                "bed:read",
                self._get_bed_availability,
                {},
            ),
            ToolSpec(
                "get_invoice_summary",
                "Return an itemised invoice summary by invoice number.",
                "billing:read",
                self._get_invoice_summary,
                {"invoice_no": "str"},
            ),
            ToolSpec(
                "get_claim_status",
                "Return the current status of an insurance claim.",
                "insurance:read",
                self._get_claim_status,
                {"claim_no": "str"},
            ),
            ToolSpec(
                "validate_retrieved_evidence",
                "Check whether a drafted answer is supported by retrieved evidence.",
                "faq:ask",
                self._validate_retrieved_evidence,
                {"answer": "str", "chunk_ids": "list[int]"},
            ),
        ]

    def _search_hospital_faq(self, query: str, corpus: str = "operational_faq", top_k: int = 5) -> dict:
        result = retriever.search(self.session, query, corpus=corpus, top_k=top_k)
        return {
            "query": query,
            "corpus": corpus,
            "sufficient": result.sufficient,
            "top_score": round(result.top_score, 4),
            "passages": [c.as_dict() for c in result.chunks],
        }

    def _get_appointment_availability(self, doctor_id: int, day: str) -> dict:
        target = date.fromisoformat(day) if isinstance(day, str) else day
        slots = appointment_service.available_slots(self.session, self.user, int(doctor_id), target)
        return {
            "doctor_id": int(doctor_id),
            "date": target.isoformat(),
            "available_slots": [s.strftime("%H:%M") for s in slots],
            "count": len(slots),
        }

    def _get_patient_appointments(self, patient_id: int, limit: int = 10) -> dict:
        rows, total = appointment_service.list_appointments(
            self.session, self.user, patient_id=int(patient_id), page_size=limit
        )
        return {
            "patient_id": int(patient_id),
            "total": total,
            "appointments": [
                {
                    "reference": a.reference,
                    "doctor_id": a.doctor_id,
                    "when": a.slot_start.isoformat(timespec="minutes"),
                    "status": a.status,
                    "queue_number": a.queue_number,
                }
                for a in rows
            ],
        }

    def _get_bed_availability(self, ward: Optional[str] = None) -> dict:
        summary = admission_service.bed_availability_summary(self.session)
        if ward:
            summary = [row for row in summary if ward.lower() in row["ward"].lower()]
        return {"wards": summary, "generated_at": datetime.utcnow().isoformat(timespec="seconds")}

    def _get_invoice_summary(self, invoice_no: str) -> dict:
        invoice = billing_service.get_by_number(self.session, self.user, invoice_no)
        return billing_service.invoice_summary(invoice)

    def _get_claim_status(self, claim_no: str) -> dict:
        claim = insurance_service.get_claim_by_no(self.session, self.user, claim_no)
        return {
            "claim_no": claim.claim_no,
            "status": claim.status,
            "requested_amount": claim.requested_amount,
            "approved_amount": claim.approved_amount,
            "decided_by": claim.decided_by,
            "note": "Claim decisions are made by authorised billing staff, not by AI.",
        }

    def _validate_retrieved_evidence(self, answer: str, chunk_ids: List[int] | None = None) -> dict:
        from backend.models import FaqChunk, FaqDocument

        chunks = []
        for chunk_id in chunk_ids or []:
            chunk = self.session.get(FaqChunk, int(chunk_id))
            if not chunk:
                continue
            document = self.session.get(FaqDocument, chunk.document_id)
            chunks.append(
                retriever.RetrievedChunk(
                    chunk_id=chunk.id,
                    document_id=chunk.document_id,
                    title=document.title if document else "unknown",
                    category=document.category if document else "unknown",
                    corpus=document.corpus if document else "unknown",
                    version=document.version if document else "1.0",
                    section=chunk.section,
                    page=chunk.page,
                    content=chunk.content,
                )
            )
        return validate_answer(answer, chunks).as_dict()

