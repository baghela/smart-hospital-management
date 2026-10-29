"""Appointments: availability, booking, reschedule, cancel, check-in, queue."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from backend.ai.providers import ProviderRouter
from backend.copilot.booking_agent import BookingAgent, BookingState
from backend.services import appointment_service, directory_service, patient_service
from streamlit_app.common import (
    db,
    empty_state,
    page_header,
    paginate,
    require_login,
    run_action,
    status_badge,
)

st.set_page_config(page_title="Appointments", page_icon="📅", layout="wide")
user = require_login("appointment:read")
page_header("📅 Appointments", "Book, reschedule, cancel and check in patients. Double booking is blocked by the database.")

with db() as session:
    doctors = directory_service.list_doctors(session, user)
    patients, _total = patient_service.search_patients(session, user, page_size=200)

doctor_labels = {f"{d.full_name} · {d.specialty} ({d.code})": d.id for d in doctors}
patient_labels = {f"{p.full_name} · {p.patient_uid}": p.id for p in patients}

tab_book, tab_agent, tab_list, tab_queue = st.tabs(["Book", "🤖 Book with agent", "Schedule", "Today's queue"])

# --------------------------------------------------------------------------
with tab_agent:
    if not user.has("appointment:write"):
        st.info("Your role can view appointments but not book them.")
    elif not doctors or not patients:
        empty_state("A doctor and a patient are required before booking.", "Seed the database or add them first.")
    else:
        st.caption(
            "Tell the assistant which doctor you want (and optionally a date). It lists the free slots, "
            "asks you to confirm, then books and blocks the slot."
        )
        state_key = f"booking_agent_state_{user.username}"
        chat_key = f"booking_agent_chat_{user.username}"
        slots_key = f"booking_agent_slots_{user.username}"
        confirm_key = f"booking_agent_confirm_{user.username}"

        col1, col2, col3 = st.columns([2, 2, 1])
        agent_patient = col1.selectbox("Patient", list(patient_labels), key="agent_patient")
        agent_reason = col2.text_input("Reason for visit", placeholder="Routine consultation", key="agent_reason")
        if col3.button("Start over", use_container_width=True, key="agent_reset"):
            for key in (state_key, chat_key, slots_key, confirm_key):
                st.session_state.pop(key, None)
            st.rerun()

        if state_key not in st.session_state:
            st.session_state[state_key] = BookingState()
            st.session_state[slots_key] = []
            st.session_state[confirm_key] = False
            with db() as session:
                greeting = BookingAgent(session, user).greeting()
            st.session_state[chat_key] = [{"role": "assistant", "content": greeting}]

        def _agent_turn(action: str, payload: str = "") -> None:
            with db() as session:
                agent = BookingAgent(session, user, router=ProviderRouter())
                state = st.session_state[state_key]
                kwargs = {"patient_id": patient_labels[agent_patient], "reason": agent_reason}
                try:
                    if action == "slot":
                        reply = agent.select_slot(payload, state)
                    elif action == "confirm":
                        reply = agent.confirm(state, **kwargs)
                    else:
                        reply = agent.handle(payload, state, **kwargs)
                except Exception:  # never show a stack trace to a user
                    st.session_state[chat_key].append(
                        {"role": "assistant", "content": "Something went wrong while processing the request."}
                    )
                    return
            st.session_state[state_key] = reply.state
            st.session_state[slots_key] = reply.slots
            st.session_state[confirm_key] = reply.awaiting_confirmation
            st.session_state[chat_key].append({"role": "assistant", "content": reply.message})

        chat_box = st.container(height=380)
        with chat_box:
            for message in st.session_state[chat_key]:
                with st.chat_message(message["role"]):
                    st.markdown(message["content"])

        offered = st.session_state.get(slots_key) or []
        if offered:
            st.markdown("**Available slots** — click one to select:")
            cols = st.columns(6)
            for index, slot in enumerate(offered):
                if cols[index % 6].button(slot, key=f"agent_slot_{slot}", use_container_width=True):
                    st.session_state[chat_key].append({"role": "user", "content": slot})
                    _agent_turn("slot", slot)
                    st.rerun()

        if st.session_state.get(confirm_key):
            c1, c2, _ = st.columns([1, 1, 3])
            if c1.button("✅ Confirm booking", type="primary", key="agent_confirm"):
                st.session_state[chat_key].append({"role": "user", "content": "yes"})
                _agent_turn("confirm")
                st.rerun()
            if c2.button("✖ Choose another", key="agent_decline"):
                st.session_state[chat_key].append({"role": "user", "content": "no"})
                _agent_turn("message", "no")
                st.rerun()

        with st.form("agent_chat_form", clear_on_submit=True):
            prompt = st.text_input(
                "Message", placeholder="e.g. Book Dr. Mehta tomorrow", label_visibility="collapsed"
            )
            if st.form_submit_button("Send") and prompt.strip():
                st.session_state[chat_key].append({"role": "user", "content": prompt.strip()})
                _agent_turn("message", prompt.strip())
                st.rerun()

# --------------------------------------------------------------------------
with tab_book:
    if not user.has("appointment:write"):
        st.info("Your role can view appointments but not book them.")
    elif not doctors or not patients:
        empty_state("A doctor and a patient are required before booking.", "Seed the database or add them first.")
    else:
        col1, col2, col3 = st.columns(3)
        doctor_label = col1.selectbox("Doctor", list(doctor_labels))
        patient_label = col2.selectbox("Patient", list(patient_labels))
        day = col3.date_input("Date", value=date.today() + timedelta(days=1), min_value=date.today())

        doctor_id = doctor_labels[doctor_label]
        with st.spinner("Checking availability..."):
            with db() as session:
                slots = appointment_service.available_slots(session, user, doctor_id, day)

        if not slots:
            empty_state("No free slots for this doctor on the selected date.", "Try another date or doctor.")
        else:
            with st.form("book_appointment"):
                slot_label = st.selectbox("Available slot", [s.strftime("%H:%M") for s in slots])
                reason = st.text_input("Reason for visit", placeholder="Routine consultation")
                if st.form_submit_button("Confirm booking", type="primary"):
                    slot = datetime.combine(day, datetime.strptime(slot_label, "%H:%M").time())
                    with db() as session:
                        appointment = run_action(
                            appointment_service.book_appointment,
                            session,
                            user,
                            patient_id=patient_labels[patient_label],
                            doctor_id=doctor_id,
                            slot_start=slot,
                            reason=reason,
                            success="Appointment booked.",
                        )
                    if appointment:
                        st.info(f"Reference **{appointment.reference}** at {slot:%Y-%m-%d %H:%M}")
                        st.rerun()

# --------------------------------------------------------------------------
with tab_list:
    col1, col2, col3 = st.columns(3)
    filter_day = col1.date_input("Filter by date", value=date.today(), key="filter_day")
    use_day = col2.checkbox("Apply date filter", value=False)
    status_filter = col3.selectbox("Status", ["all", "booked", "checked_in", "completed", "cancelled"])

    with db() as session:
        rows, total = appointment_service.list_appointments(
            session,
            user,
            day=filter_day if use_day else None,
            status=None if status_filter == "all" else status_filter,
            page_size=200,
        )
        doctor_names = {d.id: d.full_name for d in directory_service.list_doctors(session, user)}
        patient_names = {p.id: f"{p.full_name} ({p.patient_uid})" for p in patients}
        records = [
            {
                "id": a.id,
                "Reference": a.reference,
                "Patient": patient_names.get(a.patient_id, a.patient_id),
                "Doctor": doctor_names.get(a.doctor_id, a.doctor_id),
                "When": a.slot_start.strftime("%Y-%m-%d %H:%M"),
                "Status": a.status,
                "Queue": a.queue_number or "-",
            }
            for a in rows
        ]

    st.caption(f"{total} appointment(s).")
    if not records:
        empty_state("No appointments match the current filters.")
    else:
        for row in paginate(records, key="appointments", page_size=8):
            with st.expander(f"{row['Reference']} · {row['Patient']} · {row['When']} · {row['Status']}"):
                st.markdown(f"Doctor: **{row['Doctor']}** · Status: {status_badge(row['Status'])}")
                if user.has("appointment:write") and row["Status"] in ("booked", "checked_in"):
                    c1, c2, c3 = st.columns(3)
                    if row["Status"] == "booked" and c1.button("Check in", key=f"ci_{row['id']}"):
                        with db() as session:
                            run_action(
                                appointment_service.check_in, session, user, row["id"],
                                success="Patient checked in.",
                            )
                        st.rerun()

                    with c2.popover("Reschedule"):
                        new_day = st.date_input("New date", value=date.today() + timedelta(days=1), key=f"rd_{row['id']}")
                        new_time = st.text_input("New time (HH:MM)", value="10:00", key=f"rt_{row['id']}")
                        if st.button("Apply", key=f"rb_{row['id']}"):
                            try:
                                slot = datetime.combine(new_day, datetime.strptime(new_time, "%H:%M").time())
                            except ValueError:
                                st.error("Time must be in HH:MM format.")
                            else:
                                with db() as session:
                                    run_action(
                                        appointment_service.reschedule_appointment,
                                        session, user, row["id"], slot,
                                        success="Appointment rescheduled.",
                                    )
                                st.rerun()

                    with c3.popover("Cancel"):
                        reason = st.text_input("Cancellation reason", key=f"cr_{row['id']}")
                        if st.button("Confirm cancel", key=f"cb_{row['id']}"):
                            with db() as session:
                                run_action(
                                    appointment_service.cancel_appointment,
                                    session, user, row["id"], reason or "not provided",
                                    success="Appointment cancelled.",
                                )
                            st.rerun()

                if row["Status"] == "checked_in" and user.has("encounter:write"):
                    with st.form(f"encounter_{row['id']}"):
                        st.caption("Complete the visit (clinician signed documentation)")
                        complaint = st.text_input("Chief complaint *", key=f"cc_{row['id']}")
                        observations = st.text_area("Observations", key=f"ob_{row['id']}", height=70)
                        plan = st.text_area("Plan", key=f"pl_{row['id']}", height=70)
                        if st.form_submit_button("Sign & complete"):
                            with db() as session:
                                run_action(
                                    appointment_service.complete_appointment,
                                    session, user, row["id"],
                                    chief_complaint=complaint,
                                    observations=observations,
                                    plan=plan,
                                    success="Encounter signed and visit completed.",
                                )
                            st.rerun()

        st.dataframe(pd.DataFrame(records).drop(columns=["id"]), use_container_width=True, hide_index=True)

# --------------------------------------------------------------------------
with tab_queue:
    if not doctors:
        empty_state("No doctors configured.")
    else:
        queue_doctor = st.selectbox("Doctor", list(doctor_labels), key="queue_doctor")
        with db() as session:
            queue = appointment_service.today_queue(session, user, doctor_labels[queue_doctor])
            patient_names = {p.id: p.full_name for p in patients}
            queue_rows = [
                {
                    "Queue #": a.queue_number,
                    "Reference": a.reference,
                    "Patient": patient_names.get(a.patient_id, a.patient_id),
                    "Slot": a.slot_start.strftime("%H:%M"),
                    "Checked in": a.checked_in_at.strftime("%H:%M") if a.checked_in_at else "-",
                }
                for a in queue
            ]
        if not queue_rows:
            empty_state("Nobody is currently checked in for this doctor.")
        else:
            st.dataframe(pd.DataFrame(queue_rows), use_container_width=True, hide_index=True)

