"""Engine / session factory.

PostgreSQL is the target datastore. SQLite is accepted for offline checks and
tests, in which case foreign keys are enabled explicitly.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings
from backend.db.base import Base

_settings = get_settings()

_connect_args = {"check_same_thread": False} if _settings.is_sqlite else {}


def _build_engine() -> Engine:
    """Create the engine, turning a missing driver into actionable guidance."""
    try:
        return create_engine(
            _settings.database_url,
            echo=_settings.sql_echo,
            future=True,
            pool_pre_ping=not _settings.is_sqlite,
            connect_args=_connect_args,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - environment specific
        raise RuntimeError(
            f"Database driver '{exc.name}' is not installed, but DATABASE_URL points at "
            f"'{_settings.database_url.split('://')[0]}'.\n"
            "Fix with one of:\n"
            "  pip install -r requirements.txt           # installs psycopg for PostgreSQL\n"
            "  set DATABASE_URL=sqlite:///./dev_hospital.db   # run offline on SQLite"
        ) from exc


engine: Engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(Engine, "connect")
def _enable_sqlite_fks(dbapi_connection, _record):  # pragma: no cover - driver hook
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager with commit/rollback semantics."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    """Create schema directly (used by tests and the SQLite dev path)."""
    import backend.models  # noqa: F401  ensures models are registered

    Base.metadata.create_all(bind=engine)

