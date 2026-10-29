"""Insurance: policies, eligibility, claims and approval-gated decisions."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from backend.services import billing_service, insurance_service, patient_service
from streamlit_app.common import db, empty_state, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Insurance", page_icon="🛡️", layout="wide")
user = require_login("insurance:read")
page_header("🛡️ Insurance", "Policies, eligibility checks and claims. Claim decisions always need human sign-off.")

with db() as session:
    patients, _total = patient_service.search_patients(session, user, page_size=200)
    policies = insurance_service.list_policies(session, user)
    claims = insurance_service.list_claims(session, user)
    invoices = billing_service.list_invoices(session, user)

patient_labels = {f"{p.full_name} · {p.patient_uid}": p.id for p in patients}
policy_labels = {f"{p.policy_no} · {p.payer_name}": p.id for p in policies}
invoice_labels = {f"{i.number} · {i.total:,.2f} ({i.status})": i.id for i in invoices}

tab_policies, tab_claims = st.tabs(["Policies", "Claims"])

with tab_policies:
    if policies:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Policy": p.policy_no,
                        "Payer": p.payer_name,
                        "Coverage": p.coverage_amount,
                        "Valid from": p.valid_from,
                        "Valid to": p.valid_to,
                        "Active": "yes" if p.valid_from <= date.today() <= p.valid_to else "no",
                    }
                    for p in policies
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        empty_state("No insurance policies recorded.")

    if user.has("insurance:write") and patients:
        with st.form("create_policy"):
            st.caption("Register a policy")
            c1, c2 = st.columns(2)
            patient_label = c1.selectbox("Patient", list(patient_labels))
            payer = c2.text_input("Payer name *", placeholder="SafeCare Health Insurance")
            policy_no = c1.text_input("Policy number *", placeholder="POL-SYN-0002")
            coverage = c2.number_input("Coverage amount", min_value=1000.0, value=200000.0, step=10000.0)
            valid_from = c1.date_input("Valid from", value=date.today() - timedelta(days=30))
            valid_to = c2.date_input("Valid to", value=date.today() + timedelta(days=335))
            if st.form_submit_button("Save policy", type="primary"):
                with db() as session:
                    run_action(
                        insurance_service.create_policy, session, user,
                        patient_id=patient_labels[patient_label], payer_name=payer,
                        policy_no=policy_no, coverage_amount=coverage,
                        valid_from=valid_from, valid_to=valid_to, success="Policy registered.",
                    )
                st.rerun()

with tab_claims:
    if user.has("insurance:write") and policies and invoices:
        with st.form("create_claim"):
            st.caption("Submit a claim (eligibility is validated deterministically)")
            c1, c2 = st.columns(2)
            policy_label = c1.selectbox("Policy", list(policy_labels))
            invoice_label = c2.selectbox("Invoice", list(invoice_labels))
            notes = st.text_input("Notes")
            if st.form_submit_button("Submit claim", type="primary"):
                with db() as session:
                    claim = run_action(
                        insurance_service.create_claim, session, user,
                        policy_id=policy_labels[policy_label],
                        invoice_id=invoice_labels[invoice_label],
                        notes=notes, success="Claim submitted.",
                    )
                if claim:
                    st.info(f"Claim number **{claim.claim_no}** for {claim.requested_amount:,.2f}")
                    st.rerun()

    if not claims:
        empty_state("No claims submitted yet.")
    else:
        for claim in claims:
            with st.expander(f"{claim.claim_no} · {claim.status} · requested {claim.requested_amount:,.2f}"):
                st.markdown(
                    f"Status {status_badge(claim.status)} · Approved **{claim.approved_amount:,.2f}**"
                    + (f" · Decided by `{claim.decided_by}`" if claim.decided_by else "")
                )
                st.caption(claim.notes or "No notes.")
                if not user.has("insurance:write"):
                    continue
                if claim.status in ("submitted", "in_review"):
                    with st.form(f"decide_{claim.id}"):
                        st.caption("Step 1 of 2 - propose a decision for human sign-off.")
                        amount = st.number_input(
                            "Proposed approved amount", min_value=0.0,
                            max_value=float(claim.requested_amount),
                            value=float(claim.requested_amount), step=500.0, key=f"ca_{claim.id}",
                        )
                        rationale = st.text_area("Rationale (min 10 chars) *", key=f"cr_{claim.id}", height=80)
                        if st.form_submit_button("Send for approval"):
                            with db() as session:
                                approval_id = run_action(
                                    insurance_service.request_decision, session, user, claim.id,
                                    proposed_amount=amount, rationale=rationale,
                                    success="Claim decision sent for billing approval.",
                                )
                            if approval_id:
                                st.info(f"Approval ticket #{approval_id} - decide it on the **Approvals** page.")
                if st.button("Apply approved decision", key=f"ap_{claim.id}"):
                    with db() as session:
                        run_action(
                            insurance_service.apply_decision, session, user, claim.id,
                            success="Claim decision applied.",
                        )
                    st.rerun()

