"""Shared helpers for services (human readable reference ids)."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.base import Base


def next_reference(session: Session, model: type[Base], column, prefix: str, width: int = 5) -> str:
    """Generate a sequential business reference such as ``APT-00007``."""
    count = session.scalar(select(func.count()).select_from(model)) or 0
    candidate = count + 1
    while True:
        reference = f"{prefix}-{candidate:0{width}d}"
        exists = session.scalar(select(model.id).where(column == reference))
        if not exists:
            return reference
        candidate += 1


def utcnow() -> datetime:
    return datetime.utcnow().replace(microsecond=0)

