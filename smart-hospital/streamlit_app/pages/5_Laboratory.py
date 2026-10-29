"""Laboratory: orders, specimen tracking and results."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.services import lab_service, patient_service
from streamlit_app.common import db, empty_state, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Laboratory", page_icon="🧪", layout="wide")
user = require_login("lab:read")
page_header("🧪 Laboratory", "Order tests, track specimens and record results. Results are entered by lab staff only.")

with db() as session:
    patients, _total = patient_service.search_patients(session, user, page_size=200)
    orders = lab_service.list_orders(session, user)

patient_labels = {f"{p.full_name} · {p.patient_uid}": p.id for p in patients}
patient_names = {p.id: p.full_name for p in patients}

tab_order, tab_track = st.tabs(["Order a test", "Track orders"])

with tab_order:
    if not user.has("lab:write"):
        st.info("Your role can view lab orders but not create them.")
    elif not patients:
        empty_state("No patients registered yet.")
    else:
        with st.form("order_test"):
            patient_label = st.selectbox("Patient", list(patient_labels))
            test_code = st.selectbox(
                "Test",
                list(lab_service.TEST_CATALOG),
                format_func=lambda c: f"{c} - {lab_service.TEST_CATALOG[c][0]} ({lab_service.TEST_CATALOG[c][1]:.0f})",
            )
            if st.form_submit_button("Place order", type="primary"):
                with db() as session:
                    order = run_action(
                        lab_service.order_test,
                        session, user,
                        patient_id=patient_labels[patient_label],
                        test_code=test_code,
                        success="Lab order created.",
                    )
                if order:
                    st.info(f"Order reference **{order.reference}**")
                    st.rerun()

with tab_track:
    status_filter = st.selectbox(
        "Status", ["all", "ordered", "collected", "in_progress", "resulted", "cancelled"]
    )
    with db() as session:
        orders = lab_service.list_orders(
            session, user, status=None if status_filter == "all" else status_filter
        )
        records = [
            {
                "id": o.id,
                "Reference": o.reference,
                "Patient": patient_names.get(o.patient_id, o.patient_id),
                "Test": o.test_name,
                "Status": o.status,
                "Specimen": o.specimen_id or "-",
                "Result": o.result_value or "-",
            }
            for o in orders
        ]

    if not records:
        empty_state("No lab orders match this filter.")
    else:
        for row in records:
            with st.expander(f"{row['Reference']} · {row['Patient']} · {row['Test']} · {row['Status']}"):
                st.markdown(f"Status: {status_badge(row['Status'])} · Specimen: `{row['Specimen']}`")
                if not user.has("lab:write"):
                    st.caption("Read-only view for your role.")
                    continue
                c1, c2, c3 = st.columns(3)
                if row["Status"] == "ordered" and c1.button("Collect specimen", key=f"col_{row['id']}"):
                    with db() as session:
                        run_action(lab_service.collect_specimen, session, user, row["id"], success="Specimen collected.")
                    st.rerun()
                if row["Status"] == "collected" and c2.button("Start processing", key=f"st_{row['id']}"):
                    with db() as session:
                        run_action(lab_service.start_processing, session, user, row["id"], success="Processing started.")
                    st.rerun()
                if row["Status"] == "in_progress":
                    with c3.form(f"res_{row['id']}"):
                        value = st.text_input("Result value *", key=f"rv_{row['id']}")
                        notes = st.text_input("Notes", key=f"rn_{row['id']}")
                        if st.form_submit_button("Record result"):
                            with db() as session:
                                run_action(
                                    lab_service.record_result, session, user, row["id"],
                                    value=value, notes=notes, success="Result recorded.",
                                )
                            st.rerun()

        st.dataframe(pd.DataFrame(records).drop(columns=["id"]), use_container_width=True, hide_index=True)

