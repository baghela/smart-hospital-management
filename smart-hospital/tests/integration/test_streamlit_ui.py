"""Streamlit UI smoke tests.

Runs every page through ``streamlit.testing.AppTest`` to prove the UI renders
without exceptions, both unauthenticated (guard shows a warning and stops) and
authenticated as an administrator.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from backend.auth.security import create_token  # noqa: E402
from backend.models import Role  # noqa: E402
from backend.services import auth_service  # noqa: E402

APP_DIR = Path(__file__).resolve().parents[2] / "streamlit_app"
PAGES = sorted(str(p) for p in (APP_DIR / "pages").glob("*.py"))
ENTRY = str(APP_DIR / "app.py")


@pytest.fixture
def seeded(session, admin, hospital, faq_corpus):
    """A populated database plus an admin login the UI can use."""
    auth_service.create_user(
        session, username="admin", full_name="Asha Admin", role=Role.ADMIN, password="Hospital@123"
    )
    session.commit()
    return create_token("admin", Role.ADMIN.value)


def _run(script: str, token: str | None = None) -> AppTest:
    app = AppTest.from_file(script, default_timeout=60)
    if token:
        app.session_state["token"] = token
        app.session_state["display_name"] = "Asha Admin"
    return app.run()


def test_entry_page_renders_login_when_signed_out(session):
    app = _run(ENTRY)
    assert not app.exception
    assert any("Sign in" in str(h.value) for h in app.subheader)


def test_entry_page_renders_kpis_when_signed_in(seeded):
    app = _run(ENTRY, seeded)
    assert not app.exception
    assert len(app.metric) >= 8  # two KPI rows


@pytest.mark.parametrize("script", PAGES, ids=lambda p: Path(p).stem)
def test_every_page_renders_without_exception_when_signed_out(session, script):
    app = _run(script)
    assert not app.exception, f"{script} raised {app.exception}"


@pytest.mark.parametrize("script", PAGES, ids=lambda p: Path(p).stem)
def test_every_page_renders_without_exception_for_admin(seeded, script):
    app = _run(script, seeded)
    assert not app.exception, f"{script} raised {app.exception}"


def test_all_thirteen_pages_are_present():
    assert len(PAGES) == 13


def test_pages_are_numbered_in_navigation_order():
    stems = [Path(p).stem for p in PAGES]
    numbers = sorted(int(s.split("_", 1)[0]) for s in stems)
    assert numbers == list(range(1, 14))

