"""Deterministic guardrails: approval policy, prompt-injection screening,
clinical boundaries, upload validation, token budgets and rate limiting.

These are code-enforced checks, not system-prompt suggestions.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable, List, Tuple

from backend.core.config import get_settings
from backend.core.errors import GuardrailError, ValidationError
from backend.models.enums import ApprovalAction

# --------------------------------------------------------------------------
# Narrow human-in-the-loop policy
# --------------------------------------------------------------------------
HIGH_IMPACT_ACTIONS: Dict[str, str] = {
    ApprovalAction.DISCHARGE.value: "Patient discharge requires clinician sign-off.",
    ApprovalAction.CLAIM_DECISION_SUPPORT.value: "Insurance claim decisions require billing sign-off.",
    ApprovalAction.PHARMACY_DISPENSE.value: "Medication dispensing requires pharmacist sign-off.",
}

# Low-risk mutations deliberately excluded from approval (narrow scope).
AUTO_ALLOWED_ACTIONS = {
    "appointment_book",
    "appointment_reschedule",
    "appointment_cancel",
    "appointment_checkin",
    "patient_register",
    "bed_transfer",
    "invoice_issue",
    "payment_record",
}


def requires_human_approval(action: str) -> bool:
    return action in HIGH_IMPACT_ACTIONS


def approval_reason(action: str) -> str:
    return HIGH_IMPACT_ACTIONS.get(action, "")


# --------------------------------------------------------------------------
# Prompt injection / unsafe content screening
# --------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"disregard (the )?(system|previous) (prompt|instructions)",
    r"reveal (your )?(system prompt|instructions|api key|secret)",
    r"you are now (a|an) ",
    r"act as (an? )?(admin|administrator|root|developer)",
    r"print (the )?(env|environment|api[_ ]key|password)",
    r"<\s*/?\s*(script|iframe|object)\b",
    r"\bDROP\s+TABLE\b|\bDELETE\s+FROM\b|\bUNION\s+SELECT\b|;--",
    r"exfiltrat|base64 decode and run|curl\s+http",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

_CLINICAL_PATTERNS = [
    r"\b(diagnos\w*)\b.*\b(me|my|patient)\b",
    r"what('s| is) wrong with (me|my|the patient)",
    r"\b(prescribe|prescription for|dosage for me|increase (my )?dose|stop taking)\b",
    r"\b(should i take|which medicine should)\b",
    r"\bapprove (my|this) claim\b",
]
_CLINICAL_RE = [re.compile(p, re.IGNORECASE) for p in _CLINICAL_PATTERNS]

_EMERGENCY_PATTERNS = [
    r"\b(chest pain|heart attack|stroke|unconscious|not breathing|severe bleeding|suicide|overdose)\b",
]
_EMERGENCY_RE = [re.compile(p, re.IGNORECASE) for p in _EMERGENCY_PATTERNS]

_PII_RE = [
    (re.compile(r"\b\d{12}\b"), "government id"),
    (re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"), "card number"),
]

EMERGENCY_MESSAGE = (
    "This may be a medical emergency. Call the hospital emergency line at "
    "**1066** or go to the Emergency Department immediately. I cannot provide "
    "clinical advice."
)

CLINICAL_BOUNDARY_MESSAGE = (
    "I can't provide diagnosis, medication changes or claim approvals. "
    "A qualified clinician or the relevant hospital desk must handle this. "
    "I can still answer operational questions (timings, process, documents)."
)


@dataclass
class ScreenResult:
    allowed: bool
    reason: str = ""
    category: str = "ok"
    message: str = ""
    flags: List[str] = field(default_factory=list)


def screen_user_message(text: str) -> ScreenResult:
    """Screen an inbound user message before it reaches retrieval or an LLM."""
    if not text or not text.strip():
        return ScreenResult(False, "empty", "invalid", "Please enter a question.")
    if len(text) > 2000:
        return ScreenResult(False, "too_long", "invalid", "Question is too long (max 2000 chars).")

    flags: List[str] = []
    for pattern in _EMERGENCY_RE:
        if pattern.search(text):
            return ScreenResult(False, "emergency", "emergency", EMERGENCY_MESSAGE, ["emergency"])

    for pattern in _INJECTION_RE:
        if pattern.search(text):
            return ScreenResult(
                False,
                "prompt_injection",
                "blocked",
                "That request looks like an attempt to override system rules, so it was blocked.",
                ["prompt_injection"],
            )

    for pattern in _CLINICAL_RE:
        if pattern.search(text):
            return ScreenResult(
                False, "clinical_boundary", "clinical", CLINICAL_BOUNDARY_MESSAGE, ["clinical"]
            )

    for pattern, label in _PII_RE:
        if pattern.search(text):
            flags.append(f"pii:{label}")

    return ScreenResult(True, flags=flags)


def sanitize_retrieved_text(text: str) -> str:
    """Retrieved documents are untrusted data, never instructions."""
    cleaned = text
    for pattern in _INJECTION_RE:
        cleaned = pattern.sub("[removed-instruction]", cleaned)
    return cleaned.replace("```", "'''")


def redact_pii(text: str) -> str:
    out = text
    for pattern, label in _PII_RE:
        out = pattern.sub(f"[redacted-{label.replace(' ', '-')}]", out)
    return out


# --------------------------------------------------------------------------
# Upload validation
# --------------------------------------------------------------------------
_MIME_BY_EXT = {
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    },
    ".csv": {"text/csv", "application/vnd.ms-excel", "text/plain"},
    ".txt": {"text/plain"},
    ".md": {"text/plain", "text/markdown"},
}


def validate_upload(filename: str, size_bytes: int, mime_type: str | None = None) -> str:
    """Validate extension, size and MIME type. Returns the normalised extension."""
    settings = get_settings()
    name = (filename or "").strip()
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValidationError("Invalid file name.")
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if ext not in settings.allowed_upload_ext:
        raise ValidationError(
            f"Unsupported file type '{ext or 'unknown'}'. Allowed: {', '.join(settings.allowed_upload_ext)}"
        )
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if size_bytes <= 0:
        raise ValidationError("Uploaded file is empty.")
    if size_bytes > max_bytes:
        raise ValidationError(f"File exceeds the {settings.max_upload_mb} MB limit.")
    if mime_type and ext in _MIME_BY_EXT and mime_type not in _MIME_BY_EXT[ext]:
        raise ValidationError(f"MIME type '{mime_type}' does not match extension '{ext}'.")
    return ext


def screen_document_text(text: str) -> Tuple[str, List[str]]:
    """Sanitize extracted document text and report what was neutralised."""
    flags: List[str] = []
    for pattern in _INJECTION_RE:
        if pattern.search(text):
            flags.append("embedded_instruction_removed")
            break
    return sanitize_retrieved_text(text), flags


# --------------------------------------------------------------------------
# Token budget
# --------------------------------------------------------------------------
def estimate_tokens(text: str) -> int:
    """Cheap deterministic estimate (~4 chars/token)."""
    return max(1, len(text or "") // 4)


class TokenBudget:
    """Per-session token budget enforced before any provider call."""

    def __init__(self, limit: int | None = None) -> None:
        self.limit = limit or get_settings().session_token_budget
        self._used: Dict[str, int] = defaultdict(int)

    def used(self, session_id: str) -> int:
        return self._used[session_id]

    def remaining(self, session_id: str) -> int:
        return max(0, self.limit - self._used[session_id])

    def check(self, session_id: str, projected: int) -> None:
        if self._used[session_id] + projected > self.limit:
            raise GuardrailError(
                "Session token budget exhausted. Clear the chat or raise the budget in Admin.",
                code="token_budget_exceeded",
            )

    def consume(self, session_id: str, tokens: int) -> int:
        self._used[session_id] += max(0, tokens)
        return self._used[session_id]

    def reset(self, session_id: str | None = None) -> None:
        if session_id is None:
            self._used.clear()
        else:
            self._used.pop(session_id, None)


TOKEN_BUDGET = TokenBudget()


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int | None = None) -> None:
        self.per_minute = per_minute or get_settings().rate_limit_per_minute
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.time()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= self.per_minute:
            raise GuardrailError("Rate limit exceeded, retry shortly.", code="rate_limited")
        bucket.append(now)

    def reset(self) -> None:
        self._hits.clear()


RATE_LIMITER = RateLimiter()


def bound_history(history: Iterable[dict], max_turns: int | None = None) -> List[dict]:
    """Keep chat history bounded to control token cost."""
    limit = (max_turns or get_settings().max_history_turns) * 2
    items = list(history)
    return items[-limit:] if limit > 0 else []

