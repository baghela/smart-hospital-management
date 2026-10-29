"""Application configuration loaded from environment variables.

PostgreSQL is the primary datastore (Option B: PostgreSQL + Alembic).
A SQLite URL may be supplied via ``DATABASE_URL`` for offline module checks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List

try:  # optional, keeps module importable without python-dotenv
    from dotenv import load_dotenv

    # Load the project-root .env explicitly so it is found regardless of the
    # working directory Streamlit/uvicorn were launched from.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # pragma: no cover - optional dependency
    pass


def _csv(name: str, default: str) -> List[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    app_name: str = os.getenv("APP_NAME", "Smart Hospital Management System")
    environment: str = os.getenv("APP_ENV", "development")

    # --- Database -------------------------------------------------------
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://hospital:hospital@localhost:5432/hospital",
    )
    sql_echo: bool = _bool("SQL_ECHO", False)

    # --- Security -------------------------------------------------------
    secret_key: str = os.getenv("SECRET_KEY", "dev-only-not-for-production")
    token_ttl_seconds: int = _int("TOKEN_TTL_SECONDS", 8 * 3600)
    api_base_url: str = os.getenv("API_BASE_URL", "http://localhost:8000")

    # --- AI providers (both selectable; chain used for fallback) ---------
    provider_order: List[str] = field(
        default_factory=lambda: _csv("AI_PROVIDER_ORDER", "groq,gemini")
    )

    gemini_api_key: str = (os.getenv("GEMINI_API_KEY", "") or os.getenv("GOOGLE_API_KEY", "")).strip().strip('"')
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    gemini_base_url: str = os.getenv(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"
    )

    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip().strip('"')
    groq_model: str = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    groq_base_url: str = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")

    # --- Token / cost budget -------------------------------------------
    max_context_chars: int = _int("MAX_CONTEXT_CHARS", 6000)
    max_output_tokens: int = _int("MAX_OUTPUT_TOKENS", 400)
    max_history_turns: int = _int("MAX_HISTORY_TURNS", 4)
    session_token_budget: int = _int("SESSION_TOKEN_BUDGET", 40000)
    request_timeout_seconds: int = _int("REQUEST_TIMEOUT_SECONDS", 30)
    max_retries: int = _int("MAX_RETRIES", 2)
    # TLS for outbound AI calls. By default the OS certificate store is used
    # (via `truststore`) so corporate TLS-inspection CAs are trusted.
    ai_ca_bundle: str = os.getenv("AI_CA_BUNDLE", "")
    ai_verify_ssl: bool = _bool("AI_VERIFY_SSL", True)

    # --- Retrieval ------------------------------------------------------
    retrieval_top_k: int = _int("RETRIEVAL_TOP_K", 5)
    retrieval_candidates: int = _int("RETRIEVAL_CANDIDATES", 20)
    hybrid_alpha: float = _float("HYBRID_ALPHA", 0.5)  # vector weight
    # Calibrated against evaluation/dataset.json. With stemmed coverage the two
    # populations separate cleanly: answerable cases score >= 0.45, unanswerable
    # cases <= 0.17, so 0.25 sits in the middle of a 0.28-wide margin.
    relevance_threshold: float = _float("RELEVANCE_THRESHOLD", 0.25)
    chunk_chars: int = _int("CHUNK_CHARS", 900)
    chunk_overlap: int = _int("CHUNK_OVERLAP", 120)
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "hashing-512")

    # --- Uploads --------------------------------------------------------
    max_upload_mb: int = _int("MAX_UPLOAD_MB", 10)
    allowed_upload_ext: List[str] = field(
        default_factory=lambda: _csv("ALLOWED_UPLOAD_EXT", ".pdf,.docx,.csv,.txt,.md")
    )

    # --- Rate limiting --------------------------------------------------
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 30)

    workflow_id: str = os.getenv("WORKFLOW_ID", "smart-hospital-mvp-v1")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """Clear the cache (used by tests that patch environment variables)."""
    get_settings.cache_clear()
    return get_settings()


