"""Bed board, admissions, transfers and approval-gated discharge."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.models import AdmissionStatus, BedStatus
from backend.services import admission_service, patient_service
from streamlit_app.common import (
    db,
    empty_state,
    kpi_row,
    page_header,
    require_login,
    run_action,
    status_badge,
)

st.set_page_config(page_title="Beds & Admissions", page_icon="🛏️", layout="wide")
user = require_login("bed:read")
page_header("🛏️ Beds & admissions", "Interactive bed board. Discharge requires an approved clinician sign-off.")

with db() as session:
    board = admission_service.bed_board(session, user)
    summary = admission_service.bed_availability_summary(session)
    admissions = admission_service.list_admissions(session, user, status=AdmissionStatus.ACTIVE.value)
    patients, _total = patient_service.search_patients(session, user, page_size=200)

patient_names = {p.id: f"{p.full_name} ({p.patient_uid})" for p in patients}
total_beds = len(board)
occupied = sum(1 for b in board if b["status"] == BedStatus.OCCUPIED.value)

kpi_row(
    [
        ("Total beds", total_beds),
        ("Occupied", occupied),
        ("Available", sum(1 for b in board if b["status"] == BedStatus.AVAILABLE.value)),
        ("Occupancy", f"{round(100 * occupied / total_beds, 1) if total_beds else 0}%"),
    ]
)

tab_board, tab_admit, tab_manage = st.tabs(["Bed board", "Admit patient", "Manage admissions"])

with tab_board:
    if not board:
        empty_state("No beds configured yet.", "Create a ward with beds from the Admin page or seed data.")
    else:
        st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)
        st.caption("Bed grid - green available, red occupied, grey maintenance.")
        wards = sorted({b["ward"] for b in board})
        for ward in wards:
            st.markdown(f"**{ward}**")
            beds = [b for b in board if b["ward"] == ward]
            cols = st.columns(min(8, len(beds)))
            for index, bed in enumerate(beds):
                with cols[index % len(cols)]:
                    st.markdown(f"`{bed['bed']}`")
                    st.markdown(status_badge(bed["status"]))
                    if bed["admission_ref"]:
                        st.caption(bed["admission_ref"])

with tab_admit:
    if not user.has("admission:write"):
        st.info("Your role cannot admit patients.")
    else:
        with db() as session:
            free_beds = admission_service.available_beds(session, user)
        if not free_beds or not patients:
            empty_state("No free beds or no registered patients available.")
        else:
            bed_labels = {f"{b.label} (bed id {b.id})": b.id for b in free_beds}
            with st.form("admit"):
                patient_label = st.selectbox("Patient", list(patient_names.values()))
                bed_label = st.selectbox("Available bed", list(bed_labels))
                reason = st.text_input("Reason for admission *", placeholder="Observation for chest discomfort")
                if st.form_submit_button("Admit patient", type="primary"):
                    patient_id = next(pid for pid, label in patient_names.items() if label == patient_label)
                    with db() as session:
                        admission = run_action(
                            admission_service.admit_patient,
                            session,
                            user,
                            patient_id=patient_id,
                            bed_id=bed_labels[bed_label],
                            reason=reason,
                            success="Patient admitted.",
                        )
                    if admission:
                        st.info(f"Admission reference **{admission.reference}**")
                        st.rerun()

with tab_manage:
    if not admissions:
        empty_state("No active admissions.")
    else:
        for admission in admissions:
            with st.expander(f"{admission.reference} · {patient_names.get(admission.patient_id, admission.patient_id)}"):
                st.markdown(
                    f"Admitted **{admission.admitted_at:%Y-%m-%d %H:%M}** · "
                    f"Status {status_badge(admission.status)} · Bed id `{admission.bed_id}`"
                )
                st.caption(admission.reason or "No reason recorded.")

                col1, col2 = st.columns(2)
                if user.has("bed:write"):
                    with col1.popover("Transfer bed"):
                        with db() as session:
                            free_beds = admission_service.available_beds(session, user)
                        if not free_beds:
                            st.caption("No free beds available.")
                        else:
                            labels = {f"{b.label} (id {b.id})": b.id for b in free_beds}
                            choice = st.selectbox("New bed", list(labels), key=f"tb_{admission.id}")
                            if st.button("Transfer", key=f"tbtn_{admission.id}"):
                                with db() as session:
                                    run_action(
                                        admission_service.transfer_bed,
                                        session, user, admission.id, labels[choice],
                                        success="Patient transferred.",
                                    )
                                st.rerun()

                if user.has("discharge:request"):
                    with col2.popover("Request discharge"):
                        st.caption("Step 1 of 2 - raises a human approval ticket.")
                        summary_text = st.text_area(
                            "Discharge summary (min 20 chars) *", key=f"ds_{admission.id}", height=100
                        )
                        if st.button("Submit for approval", key=f"dr_{admission.id}"):
                            with db() as session:
                                approval_id = run_action(
                                    admission_service.request_discharge,
                                    session, user, admission.id, summary=summary_text,
                                    success="Discharge sent for clinician approval.",
                                )
                            if approval_id:
                                st.info(f"Approval ticket #{approval_id} - decide it on the **Approvals** page.")

                    if st.button("Complete discharge (needs approval)", key=f"dc_{admission.id}"):
                        with db() as session:
                            run_action(
                                admission_service.complete_discharge,
                                session, user, admission.id,
                                success="Patient discharged and bed released.",
                            )
                        st.rerun()

