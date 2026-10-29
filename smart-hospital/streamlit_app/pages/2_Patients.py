"""Patient directory: registration, search, profile and consent."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from backend.services import patient_service
from streamlit_app.common import (
    db,
    empty_state,
    page_header,
    paginate,
    require_login,
    run_action,
    status_badge,
)

st.set_page_config(page_title="Patients", page_icon="🧑", layout="wide")
user = require_login("patient:read")
page_header("🧑 Patient directory", "Search registered patients or register a new one.")

tab_search, tab_register = st.tabs(["Search & profiles", "Register patient"])

with tab_search:
    query = st.text_input("Search by name, patient ID or phone", placeholder="e.g. Aarav or PAT-000001")
    with db() as session:
        rows, total = patient_service.search_patients(session, user, query=query, page=1, page_size=200)
        data = [
            {
                "id": p.id,
                "Patient ID": p.patient_uid,
                "Name": p.full_name,
                "DOB": p.date_of_birth,
                "Gender": p.gender,
                "Phone": p.phone,
                "Blood": p.blood_group or "-",
                "Consent": "yes" if p.consent_treatment else "no",
            }
            for p in rows
        ]

    st.caption(f"{total} patient(s) matched.")
    if not data:
        empty_state("No patients found.", "Try a different search or register a new patient.")
    else:
        for row in paginate(data, key="patients", page_size=10):
            with st.expander(f"{row['Name']}  ·  {row['Patient ID']}"):
                left, right = st.columns([2, 1])
                left.write(
                    {
                        "Date of birth": str(row["DOB"]),
                        "Gender": row["Gender"],
                        "Phone": row["Phone"],
                        "Blood group": row["Blood"],
                        "Treatment consent": row["Consent"],
                    }
                )
                if user.has("patient:write"):
                    with right.form(f"update_{row['id']}"):
                        st.caption("Update contact details")
                        phone = st.text_input("Phone", value=row["Phone"], key=f"ph_{row['id']}")
                        share = st.checkbox("Consent to data sharing", key=f"cs_{row['id']}")
                        if st.form_submit_button("Save"):
                            with db() as session:
                                run_action(
                                    patient_service.update_patient,
                                    session,
                                    user,
                                    row["id"],
                                    phone=phone,
                                    consent_data_sharing=share,
                                    success="Patient updated.",
                                )
                            st.rerun()
        st.dataframe(pd.DataFrame(data).drop(columns=["id"]), use_container_width=True, hide_index=True)

with tab_register:
    if not user.has("patient:write"):
        st.info("Only administrators and receptionists can register patients.")
    else:
        with st.form("register_patient", clear_on_submit=False):
            col1, col2 = st.columns(2)
            full_name = col1.text_input("Full name *")
            dob = col2.date_input(
                "Date of birth *", value=date(1990, 1, 1), min_value=date(1900, 1, 1), max_value=date.today()
            )
            gender = col1.selectbox("Gender *", ["male", "female", "other", "undisclosed"])
            phone = col2.text_input("Phone *", placeholder="9800011001")
            email = col1.text_input("Email")
            blood = col2.selectbox("Blood group", ["", "O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"])
            address = st.text_area("Address", height=80)
            consent = st.checkbox("Patient has given treatment consent *")
            share = st.checkbox("Patient consents to data sharing with insurers")
            submitted = st.form_submit_button("Register patient", type="primary")

        if submitted:
            with db() as session:
                patient = run_action(
                    patient_service.register_patient,
                    session,
                    user,
                    full_name=full_name,
                    date_of_birth=dob,
                    gender=gender,
                    phone=phone,
                    email=email or None,
                    address=address or None,
                    blood_group=blood or None,
                    consent_treatment=consent,
                    consent_data_sharing=share,
                    success="Patient registered.",
                )
            if patient:
                st.info(f"Assigned patient ID: **{patient.patient_uid}** {status_badge('active')}")

