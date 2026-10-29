"""Role-mapped escalation matrix (static YAML source of truth).

Used when FAQ evidence is insufficient, when a clinical/financial boundary is
hit, or when an emergency phrase is detected. Falls back to a tiny built-in
parser if PyYAML is unavailable so the module always loads.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

MATRIX_PATH = Path(__file__).with_name("escalation_matrix.yaml")

# Keyword -> topic key in the matrix. Deterministic, no LLM classification.
_TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "appointment": ["appointment", "booking", "slot", "opd", "reschedul", "consult", "visiting hour"],
    "billing": ["bill", "invoice", "payment", "refund", "receipt", "charge", "cost", "price"],
    "insurance": ["insurance", "claim", "policy", "tpa", "cashless", "pre-auth", "preauth", "reimburse"],
    "admission": ["admission", "admit", "ward", "bed", "inpatient", "ipd"],
    "discharge": ["discharge", "going home", "release"],
    "laboratory": ["lab", "test", "sample", "specimen", "fasting", "report", "blood", "x-ray", "scan"],
    "pharmacy": ["pharmacy", "medicine", "medication", "drug", "dispens", "prescription"],
    "clinical": ["doctor", "nurse", "symptom", "treatment", "diagnos", "pain", "fever"],
    "records": ["record", "document", "certificate", "copy of", "medical record"],
}


@dataclass(frozen=True)
class EscalationContact:
    topic: str
    desk: str
    contact: str
    email: str
    hours: str

    def message(self) -> str:
        return (
            f"Please contact the **{self.desk}** - phone {self.contact}, "
            f"email {self.email} (available {self.hours})."
        )

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "desk": self.desk,
            "contact": self.contact,
            "email": self.email,
            "hours": self.hours,
        }


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _mini_yaml(text: str) -> Dict[str, Any]:
    """Minimal indentation-based parser for this flat mapping file."""
    root: Dict[str, Any] = {}
    stack: List[tuple[int, Dict[str, Any]]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, value = raw.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value:
            parent[key.strip()] = value
        else:
            child: Dict[str, Any] = {}
            parent[key.strip()] = child
            stack.append((indent, child))
    return root


@lru_cache(maxsize=1)
def load_matrix() -> Dict[str, Any]:
    text = MATRIX_PATH.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _mini_yaml(text)


def _contact_from(topic: str, node: Dict[str, Any]) -> EscalationContact:
    return EscalationContact(
        topic=topic,
        desk=str(node.get("desk", "Hospital Help Desk")),
        contact=str(node.get("contact", "+91-80-4000-1000")),
        email=str(node.get("email", "helpdesk@smarthospital.example")),
        hours=str(node.get("hours", "08:00-20:00 IST")),
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------
def classify_topic(question: str) -> str:
    """Keyword topic classification (cheap, deterministic, zero tokens)."""
    text = (question or "").lower()
    best_topic, best_hits = "default", 0
    for topic, keywords in _TOPIC_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic if best_hits else "default"


def resolve(question: str = "", *, role: str | None = None, emergency: bool = False) -> EscalationContact:
    """Resolve the contact for a question/role, preferring topic over role."""
    matrix = load_matrix()
    if emergency:
        return _contact_from("emergency", matrix.get("emergency", {}))

    topics = matrix.get("topics", {}) or {}
    topic = classify_topic(question)
    if topic in topics:
        return _contact_from(topic, topics[topic])

    role_map = matrix.get("roles", {}) or {}
    role_topic = role_map.get((role or "").lower())
    if role_topic and role_topic in topics:
        return _contact_from(role_topic, topics[role_topic])

    return _contact_from("default", matrix.get("default", {}))


def all_contacts() -> List[dict]:
    matrix = load_matrix()
    rows = [_contact_from("default", matrix.get("default", {})).as_dict()]
    rows.append(_contact_from("emergency", matrix.get("emergency", {})).as_dict())
    for topic, node in (matrix.get("topics", {}) or {}).items():
        rows.append(_contact_from(topic, node).as_dict())
    return rows


def ticket_reference(topic: str, seed: str) -> str:
    """Deterministic handoff reference shown to the user.

    Uses a stable digest rather than ``hash()``, which is randomised per process.
    """
    normalised = re.sub(r"\s+", " ", seed.strip().lower())
    digest = hashlib.blake2b(f"{topic}|{normalised}".encode("utf-8"), digest_size=4).digest()
    return f"ESC-{topic[:3].upper()}-{int.from_bytes(digest, 'big') % 100000:05d}"

