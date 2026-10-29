"""Billing: itemised invoices, payments, refunds and receipts."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.services import billing_service, patient_service
from streamlit_app.common import db, empty_state, kpi_row, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Billing", page_icon="🧾", layout="wide")
user = require_login("billing:read")
page_header("🧾 Billing", "Itemised invoices with deterministic totals, payments, refunds and receipts.")

with db() as session:
    patients, _total = patient_service.search_patients(session, user, page_size=200)
    invoices = billing_service.list_invoices(session, user)

patient_labels = {f"{p.full_name} · {p.patient_uid}": p.id for p in patients}
patient_names = {p.id: p.full_name for p in patients}

outstanding = sum(i.total - i.amount_paid + i.amount_refunded for i in invoices)
kpi_row(
    [
        ("Invoices", len(invoices)),
        ("Paid", sum(1 for i in invoices if i.status == "paid")),
        ("Billed value", f"{sum(i.total for i in invoices):,.2f}"),
        ("Outstanding", f"{outstanding:,.2f}"),
    ]
)

tab_create, tab_manage = st.tabs(["Create invoice", "Manage invoices"])

with tab_create:
    if not user.has("billing:write"):
        st.info("Your role can view invoices but not create them.")
    elif not patients:
        empty_state("No patients registered yet.")
    else:
        with st.form("create_invoice"):
            patient_label = st.selectbox("Patient", list(patient_labels))
            if st.form_submit_button("Create draft invoice", type="primary"):
                with db() as session:
                    invoice = run_action(
                        billing_service.create_invoice, session, user,
                        patient_id=patient_labels[patient_label], success="Draft invoice created.",
                    )
                if invoice:
                    st.info(f"Invoice number **{invoice.number}**")
                    st.rerun()

with tab_manage:
    status_filter = st.selectbox("Status", ["all", "draft", "issued", "partially_paid", "paid", "refunded"])
    with db() as session:
        invoices = billing_service.list_invoices(
            session, user, status=None if status_filter == "all" else status_filter
        )
        summaries = [
            (inv, billing_service.invoice_summary(inv), patient_names.get(inv.patient_id, inv.patient_id))
            for inv in invoices
        ]

    if not summaries:
        empty_state("No invoices match this filter.")
    else:
        for invoice, summary, patient_name in summaries:
            with st.expander(f"{invoice.number} · {patient_name} · {invoice.status} · {invoice.total:,.2f}"):
                st.markdown(
                    f"Status {status_badge(invoice.status)} · Subtotal **{summary['subtotal']:,.2f}** · "
                    f"Tax **{summary['tax']:,.2f}** · Total **{summary['total']:,.2f}** · "
                    f"Paid **{summary['paid']:,.2f}** · Outstanding **{summary['outstanding']:,.2f}**"
                )
                if summary["line_items"]:
                    st.dataframe(pd.DataFrame(summary["line_items"]), use_container_width=True, hide_index=True)
                else:
                    st.caption("No line items yet.")

                if not user.has("billing:write"):
                    continue

                if invoice.status in ("draft", "issued"):
                    with st.form(f"item_{invoice.id}"):
                        st.caption("Add a line item")
                        c1, c2, c3, c4 = st.columns(4)
                        description = c1.text_input("Description *", key=f"d_{invoice.id}")
                        category = c2.selectbox(
                            "Category", ["consultation", "laboratory", "pharmacy", "bed", "procedure", "service"],
                            key=f"c_{invoice.id}",
                        )
                        price = c3.number_input("Unit price", min_value=0.0, value=100.0, step=50.0, key=f"p_{invoice.id}")
                        qty = c4.number_input("Qty", min_value=1, value=1, step=1, key=f"q_{invoice.id}")
                        if st.form_submit_button("Add item"):
                            with db() as session:
                                run_action(
                                    billing_service.add_item, session, user, invoice.id,
                                    description=description, unit_price=price,
                                    quantity=int(qty), category=category, success="Line item added.",
                                )
                            st.rerun()

                c1, c2, c3 = st.columns(3)
                if invoice.status == "draft" and c1.button("Issue invoice", key=f"iss_{invoice.id}"):
                    with db() as session:
                        run_action(billing_service.issue_invoice, session, user, invoice.id, success="Invoice issued.")
                    st.rerun()

                if invoice.status in ("issued", "partially_paid"):
                    with c2.popover("Record payment"):
                        amount = st.number_input(
                            "Amount", min_value=0.01, value=float(max(summary["outstanding"], 0.01)),
                            step=100.0, key=f"pa_{invoice.id}",
                        )
                        method = st.selectbox("Method", ["cash", "card", "upi", "wallet"], key=f"pm_{invoice.id}")
                        if st.button("Save payment", key=f"pb_{invoice.id}"):
                            with db() as session:
                                payment = run_action(
                                    billing_service.record_payment, session, user, invoice.id,
                                    amount=amount, method=method, success="Payment recorded.",
                                )
                                if payment:
                                    fresh = billing_service.get_invoice(session, user, invoice.id)
                                    st.code(billing_service.receipt_text(fresh, payment), language="text")

                if invoice.amount_paid > invoice.amount_refunded:
                    with c3.popover("Refund"):
                        refund_amount = st.number_input(
                            "Refund amount", min_value=0.01,
                            value=float(invoice.amount_paid - invoice.amount_refunded),
                            step=50.0, key=f"ra_{invoice.id}",
                        )
                        reason = st.text_input("Reason *", key=f"rr_{invoice.id}")
                        if st.button("Process refund", key=f"rb_{invoice.id}"):
                            with db() as session:
                                run_action(
                                    billing_service.refund, session, user, invoice.id,
                                    amount=refund_amount, reason=reason, success="Refund processed.",
                                )
                            st.rerun()

