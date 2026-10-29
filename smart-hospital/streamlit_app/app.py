"""Smart Hospital Management System - Streamlit entry point.

Run with::

    streamlit run streamlit_app/app.py
"""
from __future__ import annotations

import streamlit as st

from backend.core.config import get_settings
from streamlit_app.common import (
    APP_TITLE,
    current_user,
    db,
    ensure_schema,
    kpi_row,
    login_form,
    page_header,
    render_sidebar,
)
from backend.services import report_service

st.set_page_config(page_title=APP_TITLE, page_icon="🏥", layout="wide")

ensure_schema()
settings = get_settings()
user = current_user()

page_header(f"🏥 {APP_TITLE}", "Operational hospital workflows with an evidence-grounded AI copilot.")

if not user:
    left, right = st.columns([1, 1])
    with left:
        login_form()
        st.caption("Seeded demo accounts: `admin`, `drmehta`, `nurse1`, `reception1`, `lab1`, `pharma1`, `billing1`")
    with right:
        st.subheader("What this system does")
        st.markdown(
            """
            - **Patients** - registration, consent, search and profiles
            - **Appointments** - booking, reschedule, cancel, check-in, queue
            - **Admissions** - interactive bed board, transfers, gated discharge
            - **Lab & Pharmacy** - orders, specimens, results, inventory, dispensing
            - **Billing & Insurance** - itemised invoices, payments, refunds, claims
            - **FAQ Assistant** - hybrid RAG with citations and safe abstention
            - **Approvals** - human sign-off for discharge, claims and dispensing
            """
        )
        st.info(
            "No real patient data is used. All records are synthetic. "
            "The assistant never diagnoses, prescribes or approves claims."
        )
    st.stop()

render_sidebar(user)

with db() as session:
    kpis = report_service.dashboard_kpis(session, user)

st.subheader("Live overview")
kpi_row(
    [
        ("Active patients", kpis["patients_total"]),
        ("Appointments today", kpis["appointments_today"]),
        ("Checked in now", kpis["checked_in_now"]),
        ("Active admissions", kpis["active_admissions"]),
    ]
)
kpi_row(
    [
        ("Bed occupancy", f"{kpis['bed_occupancy_pct']}%"),
        ("Pending approvals", kpis["pending_approvals"]),
        ("Outstanding amount", f"{kpis['outstanding_amount']:,.2f}"),
        ("FAQ documents", kpis["faq_documents"]),
    ]
)

st.divider()
st.markdown(
    f"""
    **Workflow id:** `{settings.workflow_id}` &nbsp;|&nbsp;
    **Provider chain:** `{' → '.join(settings.provider_order)}` &nbsp;|&nbsp;
    **Datastore:** `{'PostgreSQL' if not settings.is_sqlite else 'SQLite (dev)'}`

    Use the sidebar to open a module. Pages you are not permitted to use will
    tell you so instead of showing empty screens.
    """
)

