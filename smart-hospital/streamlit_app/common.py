"""Shared Streamlit helpers: auth state, session handling and UI widgets.

The UI calls the backend service layer in-process. RBAC, validation and audit
logging are still enforced inside the services, never in the UI.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, List, Optional

import streamlit as st
from sqlalchemy.orm import Session

from backend.auth.rbac import CurrentUser, allowed_permissions
from backend.core.errors import ApprovalRequiredError, HospitalError
from backend.db.session import SessionLocal, create_all
from backend.models import Role
from backend.services import auth_service

APP_TITLE = "Smart Hospital Management System"


# --------------------------------------------------------------------------
# Session / database
# --------------------------------------------------------------------------
@contextmanager
def db() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@st.cache_resource(show_spinner=False)
def ensure_schema() -> bool:
    """Create tables if they do not exist (no-op after Alembic migrations)."""
    create_all()
    return True


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def current_user() -> Optional[CurrentUser]:
    token = st.session_state.get("token")
    if not token:
        return None
    try:
        return auth_service.current_user_from_token(token)
    except HospitalError:
        st.session_state.pop("token", None)
        return None


def login_form() -> None:
    st.subheader("Sign in")
    with st.form("login"):
        username = st.text_input("Username", placeholder="admin")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)
    if submitted:
        with db() as session:
            try:
                user, token = auth_service.authenticate(session, username.strip(), password)
            except HospitalError as exc:
                st.error(exc.message)
                return
        st.session_state["token"] = token
        st.session_state["display_name"] = user.full_name
        st.success(f"Welcome, {user.full_name}")
        st.rerun()


def require_login(permission: str | None = None) -> CurrentUser:
    """Guard a page. Stops rendering when unauthenticated or unauthorised."""
    ensure_schema()
    user = current_user()
    if not user:
        st.warning("Please sign in from the **Home** page to continue.")
        st.stop()
    if permission and not user.has(permission):
        st.error(f"Your role (`{user.role.value}`) cannot access this page.")
        st.stop()
    render_sidebar(user)
    return user


def render_sidebar(user: CurrentUser) -> None:
    with st.sidebar:
        st.markdown(f"### {user.username}")
        st.caption(f"Role: `{user.role.value}`")
        if user.patient_id:
            st.caption(f"Linked patient id: {user.patient_id}")
        with st.expander("My permissions", expanded=False):
            st.write(", ".join(allowed_permissions(user)) or "none")
        if st.button("Sign out", use_container_width=True):
            for key in ("token", "display_name", "chat_history"):
                st.session_state.pop(key, None)
            st.rerun()


# --------------------------------------------------------------------------
# UI widgets
# --------------------------------------------------------------------------
def page_header(title: str, subtitle: str = "") -> None:
    st.title(title)
    if subtitle:
        st.caption(subtitle)


def kpi_row(items: List[tuple[str, object]], columns: int = 4) -> None:
    cols = st.columns(min(columns, max(1, len(items))))
    for index, (label, value) in enumerate(items):
        cols[index % len(cols)].metric(label, value)


STATUS_COLORS = {
    "booked": "blue", "checked_in": "orange", "completed": "green", "cancelled": "red",
    "active": "orange", "discharged": "green", "available": "green", "occupied": "red",
    "maintenance": "gray", "draft": "gray", "issued": "blue", "partially_paid": "orange",
    "paid": "green", "refunded": "violet", "pending": "orange", "approved": "green",
    "rejected": "red", "submitted": "blue", "in_review": "orange",
}


def status_badge(status: str) -> str:
    return f":{STATUS_COLORS.get(status, 'gray')}[{status.replace('_', ' ').title()}]"


def paginate(items: list, key: str, page_size: int = 10) -> list:
    if len(items) <= page_size:
        return items
    pages = (len(items) + page_size - 1) // page_size
    page = st.number_input(
        f"Page (1-{pages})", min_value=1, max_value=pages, value=1, step=1, key=f"page_{key}"
    )
    start = (int(page) - 1) * page_size
    return items[start : start + page_size]


def empty_state(message: str, hint: str = "") -> None:
    st.info(message + (f"\n\n{hint}" if hint else ""))


def run_action(fn, *args, success: str = "Done.", **kwargs):
    """Execute a service call with consistent user-facing error handling."""
    try:
        result = fn(*args, **kwargs)
    except ApprovalRequiredError as exc:
        st.warning(f"{exc.message} Check the **Approvals** page.")
        return None
    except HospitalError as exc:
        st.error(exc.message)
        return None
    except Exception:  # never show a stack trace to a user
        st.error("Something went wrong while processing the request.")
        return None
    st.success(success)
    return result


def role_options() -> list[str]:
    return [r.value for r in Role]

