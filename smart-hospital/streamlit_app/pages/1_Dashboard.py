"""Dashboard: live KPIs, trends and operational reports."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.services import report_service
from streamlit_app.common import db, empty_state, kpi_row, page_header, require_login

st.set_page_config(page_title="Dashboard", page_icon="📊", layout="wide")
user = require_login()
page_header("📊 Operations dashboard", "Every number below is computed from live database state.")

with db() as session:
    kpis = report_service.dashboard_kpis(session, user)
    trend = report_service.appointments_trend(session, 7)
    revenue = report_service.revenue_by_status(session)
    load = report_service.department_load(session)
    feedback = report_service.faq_feedback_stats(session)

kpi_row(
    [
        ("Patients", kpis["patients_total"]),
        ("Appointments today", kpis["appointments_today"]),
        ("Active admissions", kpis["active_admissions"]),
        ("Beds occupied", f"{kpis['beds_occupied']}/{kpis['beds_total']}"),
    ]
)
kpi_row(
    [
        ("Pending approvals", kpis["pending_approvals"]),
        ("Pending lab orders", kpis["pending_lab_orders"]),
        ("Open claims", kpis["open_claims"]),
        ("Revenue today", f"{kpis['revenue_today']:,.2f}"),
    ]
)

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("Appointments - last 7 days")
    frame = pd.DataFrame(trend)
    if frame["appointments"].sum() == 0:
        empty_state("No appointments recorded in the last 7 days.")
    else:
        st.bar_chart(frame.set_index("date"))

with right:
    st.subheader("Invoice value by status")
    if not revenue:
        empty_state("No invoices raised yet.", "Create one from the Billing page.")
    else:
        st.dataframe(pd.DataFrame(revenue), use_container_width=True, hide_index=True)

st.subheader("Department workload")
if not load:
    empty_state("No departments configured yet.")
else:
    st.dataframe(pd.DataFrame(load), use_container_width=True, hide_index=True)

st.subheader("FAQ assistant feedback")
kpi_row(
    [
        ("Responses rated", feedback["feedback_total"]),
        ("Marked helpful", feedback["helpful"]),
        ("Helpful rate", f"{feedback['helpful_rate']}%"),
    ],
    columns=3,
)

