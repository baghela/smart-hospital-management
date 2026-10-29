"""Discharge summaries and follow-up appointment scheduling."""
from __future__ import annotations

import streamlit as st

from backend.models import AdmissionStatus
from backend.services import admission_service, directory_service, discharge_service, patient_service
from streamlit_app.common import db, empty_state, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Discharge", page_icon="📤", layout="wide")
user = require_login("bed:read")
page_header("📤 Discharge & follow-up", "Generated from recorded data only - no AI-authored clinical content.")

with db() as session:
    discharged = admission_service.list_admissions(session, user, status=AdmissionStatus.DISCHARGED.value)
    active = admission_service.list_admissions(session, user, status=AdmissionStatus.ACTIVE.value)
    patients, _total = patient_service.search_patients(session, user, page_size=200)
    doctors = directory_service.list_doctors(session, user)

patient_names = {p.id: f"{p.full_name} ({p.patient_uid})" for p in patients}
doctor_labels = {f"{d.full_name} ({d.code})": d.id for d in doctors}

tab_summary, tab_follow = st.tabs(["Discharge summaries", "Follow-up booking"])

with tab_summary:
    records = discharged + active
    if not records:
        empty_state("No admissions recorded yet.")
    else:
        for admission in records:
            label = f"{admission.reference} · {patient_names.get(admission.patient_id, admission.patient_id)}"
            with st.expander(f"{label} · {admission.status}"):
                st.markdown(f"Status {status_badge(admission.status)}")
                with db() as session:
                    try:
                        summary = discharge_service.build_discharge_summary(session, user, admission.id)
                    except Exception:
                        summary = "Summary unavailable."
                st.code(summary, language="text")
                st.download_button(
                    "Download summary",
                    data=summary,
                    file_name=f"{admission.reference}_discharge_summary.txt",
                    key=f"dl_{admission.id}",
                )

with tab_follow:
    if not discharged:
        empty_state("No discharged admissions to follow up.", "Complete a discharge from the Beds page first.")
    elif not doctors:
        empty_state("No doctors available to schedule a follow-up.")
    else:
        admission_labels = {
            f"{a.reference} · {patient_names.get(a.patient_id, a.patient_id)}": a.id for a in discharged
        }
        with st.form("follow_up"):
            admission_label = st.selectbox("Discharged admission", list(admission_labels))
            doctor_label = st.selectbox("Follow-up doctor", list(doctor_labels))
            days = st.slider("Days after today", min_value=1, max_value=60, value=7)
            if st.form_submit_button("Book first free slot", type="primary"):
                with db() as session:
                    appointment = run_action(
                        discharge_service.schedule_follow_up,
                        session,
                        user,
                        admission_id=admission_labels[admission_label],
                        doctor_id=doctor_labels[doctor_label],
                        days_ahead=days,
                        success="Follow-up appointment booked.",
                    )
                if appointment:
                    st.info(f"Reference **{appointment.reference}** at {appointment.slot_start:%Y-%m-%d %H:%M}")

