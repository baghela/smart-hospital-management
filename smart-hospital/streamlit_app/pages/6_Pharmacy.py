"""Pharmacy: inventory, prescriptions and approval-gated dispensing."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.services import directory_service, patient_service, pharmacy_service
from streamlit_app.common import db, empty_state, kpi_row, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Pharmacy", page_icon="💊", layout="wide")
user = require_login("pharmacy:read")
page_header("💊 Pharmacy", "Inventory and prescriptions. Dispensing requires pharmacist sign-off.")

with db() as session:
    items = pharmacy_service.list_items(session, user)
    prescriptions = pharmacy_service.list_prescriptions(session, user)
    patients, _total = patient_service.search_patients(session, user, page_size=200)
    doctors = directory_service.list_doctors(session, user)

patient_labels = {f"{p.full_name} · {p.patient_uid}": p.id for p in patients}
patient_names = {p.id: p.full_name for p in patients}
doctor_labels = {f"{d.full_name} ({d.code})": d.id for d in doctors}
item_labels = {f"{i.name} · {i.sku} (stock {i.stock_qty})": i.id for i in items}

low_stock = [i for i in items if i.stock_qty <= i.reorder_level]
kpi_row(
    [
        ("Inventory items", len(items)),
        ("Low stock items", len(low_stock)),
        ("Active prescriptions", sum(1 for p in prescriptions if p.status == "active")),
        ("Dispensed", sum(1 for p in prescriptions if p.status == "dispensed")),
    ]
)

tab_stock, tab_rx = st.tabs(["Inventory", "Prescriptions"])

with tab_stock:
    if not items:
        empty_state("No pharmacy items yet.")
    else:
        frame = pd.DataFrame(
            [
                {
                    "SKU": i.sku,
                    "Name": i.name,
                    "Form": i.form,
                    "Unit price": i.unit_price,
                    "Stock": i.stock_qty,
                    "Reorder level": i.reorder_level,
                    "Low stock": "yes" if i.stock_qty <= i.reorder_level else "no",
                }
                for i in items
            ]
        )
        st.dataframe(frame, use_container_width=True, hide_index=True)

    if user.has("pharmacy:write"):
        with st.form("upsert_item"):
            st.caption("Add or update an inventory item")
            c1, c2, c3 = st.columns(3)
            sku = c1.text_input("SKU *", placeholder="MED006")
            name = c2.text_input("Name *")
            form = c3.selectbox("Form", ["tablet", "capsule", "syrup", "sachet", "injection"])
            price = c1.number_input("Unit price", min_value=0.0, value=10.0, step=0.5)
            stock = c2.number_input("Stock quantity", min_value=0, value=50, step=10)
            reorder = c3.number_input("Reorder level", min_value=0, value=20, step=5)
            if st.form_submit_button("Save item", type="primary"):
                with db() as session:
                    run_action(
                        pharmacy_service.upsert_item, session, user,
                        sku=sku, name=name, unit_price=price, stock_qty=int(stock),
                        form=form, reorder_level=int(reorder), success="Inventory updated.",
                    )
                st.rerun()

with tab_rx:
    if user.role.value in ("doctor", "admin") and patients and doctors and items:
        with st.form("create_rx"):
            st.caption("Create a prescription (doctors only - the assistant never prescribes)")
            c1, c2 = st.columns(2)
            patient_label = c1.selectbox("Patient", list(patient_labels))
            doctor_label = c2.selectbox("Prescribing doctor", list(doctor_labels))
            item_label = c1.selectbox("Medicine", list(item_labels))
            quantity = c2.number_input("Quantity", min_value=1, value=10, step=1)
            dosage = st.text_input("Dosage", value="1-0-1")
            if st.form_submit_button("Create prescription", type="primary"):
                with db() as session:
                    rx = run_action(
                        pharmacy_service.create_prescription, session, user,
                        patient_id=patient_labels[patient_label],
                        doctor_id=doctor_labels[doctor_label],
                        item_id=item_labels[item_label],
                        quantity=int(quantity), dosage=dosage,
                        success="Prescription created.",
                    )
                if rx:
                    st.rerun()

    if not prescriptions:
        empty_state("No prescriptions recorded.")
    else:
        for rx in prescriptions:
            with st.expander(f"{rx.reference} · {patient_names.get(rx.patient_id, rx.patient_id)} · {rx.item.name}"):
                st.markdown(
                    f"Quantity **{rx.quantity}** · Dosage `{rx.dosage}` · Status {status_badge(rx.status)}"
                )
                if rx.dispensed_by:
                    st.caption(f"Dispensed after approval by {rx.dispensed_by}")
                if rx.status == "active" and user.has("pharmacy:write"):
                    c1, c2 = st.columns(2)
                    if c1.button("Request dispense approval", key=f"rq_{rx.id}"):
                        with db() as session:
                            approval_id = run_action(
                                pharmacy_service.request_dispense, session, user, rx.id,
                                success="Dispense sent for pharmacist approval.",
                            )
                        if approval_id:
                            st.info(f"Approval ticket #{approval_id} - decide it on the **Approvals** page.")
                    if c2.button("Complete dispense (needs approval)", key=f"dp_{rx.id}"):
                        with db() as session:
                            run_action(
                                pharmacy_service.complete_dispense, session, user, rx.id,
                                success="Medication dispensed and stock updated.",
                            )
                        st.rerun()

