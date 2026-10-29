"""Human-in-the-loop approvals: discharge, claim decisions, dispensing."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from backend.auth.rbac import APPROVER_ROLES
from backend.guardrails.policy import AUTO_ALLOWED_ACTIONS, HIGH_IMPACT_ACTIONS
from backend.models import ApprovalStatus
from backend.services import approval_service
from streamlit_app.common import db, empty_state, kpi_row, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Approvals", page_icon="✅", layout="wide")
user = require_login("approval:read")
page_header(
    "✅ Human approvals",
    "Only high-impact actions are gated. A request cannot be approved by the person who raised it.",
)

with st.expander("Approval policy", expanded=False):
    st.markdown("**Gated actions (require human sign-off):**")
    for action, reason in HIGH_IMPACT_ACTIONS.items():
        approvers = ", ".join(sorted(r.value for r in APPROVER_ROLES.get(action, set())))
        st.markdown(f"- `{action}` — {reason} _(approvers: {approvers})_")
    st.markdown("**Executed immediately and audited:** " + ", ".join(f"`{a}`" for a in sorted(AUTO_ALLOWED_ACTIONS)))

with db() as session:
    pending = approval_service.list_approvals(session, user, status=ApprovalStatus.PENDING.value)
    approved = approval_service.list_approvals(session, user, status=ApprovalStatus.APPROVED.value)
    rejected = approval_service.list_approvals(session, user, status=ApprovalStatus.REJECTED.value)

kpi_row(
    [("Pending", len(pending)), ("Approved", len(approved)), ("Rejected", len(rejected))],
    columns=3,
)

tab_pending, tab_history = st.tabs(["Pending decisions", "History"])

with tab_pending:
    if not pending:
        empty_state("Nothing is waiting for approval.")
    else:
        for request in pending:
            can_decide = user.can_approve(request.action) and user.has("approval:decide")
            with st.expander(f"#{request.id} · {request.action} · {request.summary}"):
                st.markdown(
                    f"Raised by **{request.requested_by}** at {request.requested_at:%Y-%m-%d %H:%M} · "
                    f"resource `{request.resource_id}` · {status_badge(request.status)}"
                )
                payload = approval_service.payload_of(request)
                if payload:
                    st.json(payload)

                if not can_decide:
                    st.info(
                        f"Your role (`{user.role.value}`) cannot decide `{request.action}`. "
                        f"Allowed: {', '.join(sorted(r.value for r in APPROVER_ROLES.get(request.action, set())))}"
                    )
                    continue
                if request.requested_by == user.username:
                    st.warning("You raised this request, so you cannot approve it (four-eyes rule).")

                with st.form(f"decide_{request.id}"):
                    notes = st.text_area("Decision notes (required to reject)", height=80)
                    c1, c2 = st.columns(2)
                    approve = c1.form_submit_button("Approve", type="primary")
                    reject = c2.form_submit_button("Reject")
                if approve or reject:
                    with db() as session:
                        run_action(
                            approval_service.decide,
                            session,
                            user,
                            request.id,
                            approved=bool(approve),
                            notes=notes,
                            success="Decision recorded. Return to the originating page to execute the action.",
                        )
                    st.rerun()

with tab_history:
    history = approved + rejected
    if not history:
        empty_state("No decisions recorded yet.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "ID": r.id,
                        "Action": r.action,
                        "Resource": r.resource_id,
                        "Requested by": r.requested_by,
                        "Status": r.status,
                        "Decided by": r.decided_by or "-",
                        "Decided at": r.decided_at.strftime("%Y-%m-%d %H:%M") if r.decided_at else "-",
                        "Notes": r.decision_notes or "-",
                    }
                    for r in history
                ]
            ),
            use_container_width=True,
            hide_index=True,
        )

