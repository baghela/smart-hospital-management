"""Audit trail writer. Stores who did what, without unnecessary PHI."""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.logging import get_logger
from backend.models import AuditEvent

logger = get_logger("audit")

_PHI_PATTERNS = [
    (re.compile(r"\b\d{10,16}\b"), "[redacted-number]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[redacted-email]"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "[redacted-date]"),
]


def scrub(text: Optional[str]) -> Optional[str]:
    """Remove obvious identifiers before persisting an audit detail string."""
    if not text:
        return text
    cleaned = text
    for pattern, replacement in _PHI_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned[:1000]


def record(
    session: Session,
    *,
    actor: str,
    actor_role: str,
    action: str,
    resource: str,
    resource_id: str | int | None = None,
    outcome: str = "success",
    detail: str | None = None,
    trace_id: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        actor_role=actor_role,
        action=action,
        resource=resource,
        resource_id=str(resource_id) if resource_id is not None else None,
        outcome=outcome,
        detail=scrub(detail),
        trace_id=trace_id,
    )
    session.add(event)
    session.flush()
    logger.info(
        "audit",
        extra={"event": action, "actor": actor, "resource": resource, "trace_id": trace_id},
    )
    return event


def recent(session: Session, limit: int = 100) -> list[AuditEvent]:
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    return list(session.scalars(stmt))

