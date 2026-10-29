"""Conversational appointment-booking agent.

Flow: the user names a doctor (optionally a date) -> the agent lists free slots
-> the user picks a slot -> the agent asks for explicit confirmation -> only then
the slot is booked through ``appointment_service.book_appointment``. That call
inserts the row in the ``appointments`` table and blocks the slot via the
``uq_appointment_doctor_active_slot`` constraint (RBAC + audit are enforced in
the service layer, never here).

Understanding is deterministic first (fuzzy doctor-name matching, date/time
parsing). A configured LLM provider is used only as an optional fallback to
extract a doctor name from free text; it never decides or performs the booking.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import List, Optional

from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.errors import ConflictError, HospitalError, ProviderError
from backend.core.logging import get_logger
from backend.models import Doctor
from backend.services import appointment_service, directory_service

logger = get_logger("copilot.booking_agent")

STAGE_DOCTOR = "ask_doctor"
STAGE_SLOT = "choose_slot"
STAGE_CONFIRM = "confirm"
STAGE_DONE = "done"

YES_WORDS = {"yes", "y", "yeah", "yep", "confirm", "confirmed", "ok", "okay", "sure", "book", "proceed", "go ahead"}
NO_WORDS = {"no", "n", "nope", "cancel", "stop", "back", "change", "other"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_NAME_STOPWORDS = {"dr", "doctor", "mr", "mrs", "ms", "prof"}


@dataclass
class BookingState:
    stage: str = STAGE_DOCTOR
    doctor_id: Optional[int] = None
    doctor_name: str = ""
    day: Optional[date] = None
    slots: List[str] = field(default_factory=list)  # "HH:MM"
    selected_slot: Optional[str] = None
    appointment_reference: Optional[str] = None


@dataclass
class AgentReply:
    message: str
    state: BookingState
    slots: List[str] = field(default_factory=list)
    awaiting_confirmation: bool = False
    booked: bool = False
    appointment_id: Optional[int] = None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _normalise(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _NAME_STOPWORDS]


def _name_score(doctor: Doctor, words: List[str]) -> float:
    """Score how well the message mentions this doctor (0 = not at all)."""
    if doctor.code.lower() in words:
        return 10.0
    name_tokens = [t for t in _normalise(doctor.full_name) if len(t) > 1]
    score = 0.0
    for token in name_tokens:
        best = max((SequenceMatcher(None, token, w).ratio() for w in words if len(w) > 1), default=0.0)
        if best == 1.0:
            score += 1.0
        elif best >= 0.8:  # tolerate small typos, e.g. "mehra" vs "mehta"
            score += 0.7
    return score


def match_doctors(doctors: List[Doctor], text: str) -> List[Doctor]:
    words = _normalise(text)
    scored = [(d, _name_score(d, words)) for d in doctors]
    scored = [(d, s) for d, s in scored if s > 0]
    if not scored:
        return []
    top = max(s for _, s in scored)
    return [d for d, s in scored if s == top]


def parse_day(text: str, today: Optional[date] = None) -> Optional[date]:
    today = today or date.today()
    lower = text.lower()
    if "day after tomorrow" in lower:
        return today + timedelta(days=2)
    if "tomorrow" in lower:
        return today + timedelta(days=1)
    if "today" in lower:
        return today
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lower)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None
    dmy = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lower)
    if dmy:
        year = int(dmy.group(3)) if dmy.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            parsed = date(year, int(dmy.group(2)), int(dmy.group(1)))
        except ValueError:
            return None
        if not dmy.group(3) and parsed < today:
            parsed = parsed.replace(year=today.year + 1)
        return parsed
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", lower):
            delta = (index - today.weekday()) % 7 or 7
            if "this" in lower and index == today.weekday():
                delta = 0
            return today + timedelta(days=delta)
    return None


def parse_time(text: str) -> Optional[str]:
    lower = text.lower()
    hm = re.search(r"\b(\d{1,2})[:.](\d{2})\s*(am|pm)?\b", lower)
    if hm:
        hour, minute, meridian = int(hm.group(1)), int(hm.group(2)), hm.group(3)
    else:
        h = re.search(r"\b(\d{1,2})\s*(am|pm)\b", lower)
        if not h:
            return None
        hour, minute, meridian = int(h.group(1)), 0, h.group(2)
    if meridian == "pm" and hour < 12:
        hour += 12
    if meridian == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour:02d}:{minute:02d}"


def _is_yes(text: str) -> bool:
    lower = text.strip().lower().rstrip("!.")
    return lower in YES_WORDS or lower.startswith(("yes", "confirm"))


def _is_no(text: str) -> bool:
    lower = text.strip().lower().rstrip("!.")
    return lower in NO_WORDS or lower.startswith(("no ", "cancel", "don't", "do not"))


def _llm_doctor_name(text: str, router) -> Optional[str]:
    """Optional LLM fallback: extract only the doctor's name from free text."""
    if router is None or not router.available():
        return None
    messages = [
        {
            "role": "system",
            "content": (
                "Extract the doctor's name the user wants to book with. Reply ONLY with JSON "
                '{"doctor_name": "<name or empty>"}. Do not add anything else.'
            ),
        },
        {"role": "user", "content": text[:500]},
    ]
    try:
        result = router.complete(messages, max_tokens=60)
        payload = re.search(r"\{.*\}", result.text, re.S)
        name = json.loads(payload.group(0)).get("doctor_name", "") if payload else ""
    except (ProviderError, ValueError, AttributeError):
        logger.warning("booking_agent_llm_extract_failed")
        return None
    return name.strip() or None


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class BookingAgent:
    """Stateless driver; the caller keeps ``BookingState`` between turns."""

    def __init__(self, session: Session, user: CurrentUser, *, router=None) -> None:
        self.session = session
        self.user = user
        self.router = router

    # -- public API ----------------------------------------------------
    def greeting(self) -> str:
        return (
            "Hi! I can book an appointment for you. Which doctor would you like to see? "
            "You can also mention a date, e.g. *\"Dr. Mehta tomorrow\"*."
        )

    def handle(
        self,
        message: str,
        state: BookingState,
        *,
        patient_id: Optional[int],
        reason: str = "",
    ) -> AgentReply:
        message = (message or "").strip()
        if not message:
            return AgentReply("Please type a message.", state, slots=state.slots)
        if state.stage == STAGE_DONE:
            state = BookingState()
        if state.stage == STAGE_CONFIRM:
            return self._handle_confirm(message, state, patient_id=patient_id, reason=reason)
        if state.stage == STAGE_SLOT:
            return self._handle_slot(message, state)
        return self._handle_doctor(message, state)

    def select_slot(self, slot: str, state: BookingState) -> AgentReply:
        """Called when the user clicks a slot button in the UI."""
        return self._handle_slot(slot, state)

    def confirm(self, state: BookingState, *, patient_id: Optional[int], reason: str = "") -> AgentReply:
        return self._handle_confirm("yes", state, patient_id=patient_id, reason=reason)

    # -- stages --------------------------------------------------------
    def _handle_doctor(self, message: str, state: BookingState) -> AgentReply:
        doctors = directory_service.list_doctors(self.session, self.user)
        if not doctors:
            return AgentReply("There are no active doctors configured yet.", state)

        candidates = match_doctors(doctors, message)
        if not candidates:
            llm_name = _llm_doctor_name(message, self.router)
            if llm_name:
                candidates = match_doctors(doctors, llm_name)
        if not candidates:
            names = ", ".join(f"{d.full_name} ({d.specialty})" for d in doctors[:10])
            return AgentReply(
                f"I couldn't find that doctor. Available doctors: {names}. Which one would you like?",
                state,
            )
        if len(candidates) > 1:
            names = ", ".join(f"{d.full_name} ({d.code}, {d.specialty})" for d in candidates)
            return AgentReply(f"I found several matches: {names}. Please be more specific.", state)

        doctor = candidates[0]
        state.doctor_id = doctor.id
        state.doctor_name = doctor.full_name
        state.day = parse_day(message) or date.today() + timedelta(days=1)
        reply = self._offer_slots(state)
        requested = parse_time(message)
        if requested and requested in state.slots:
            return self._handle_slot(requested, state)
        return reply

    def _handle_slot(self, message: str, state: BookingState) -> AgentReply:
        new_day = parse_day(message)
        if new_day and new_day != state.day:
            state.day = new_day
            return self._offer_slots(state)

        # Allow switching doctor mid-conversation.
        doctors = directory_service.list_doctors(self.session, self.user)
        switched = match_doctors(doctors, message)
        if len(switched) == 1 and switched[0].id != state.doctor_id and not parse_time(message):
            return self._handle_doctor(message, BookingState())

        chosen = parse_time(message)
        if not chosen:
            pick = re.fullmatch(r"\s*(?:slot\s*)?#?(\d{1,2})\s*", message.lower())
            if pick and 1 <= int(pick.group(1)) <= len(state.slots):
                chosen = state.slots[int(pick.group(1)) - 1]
        if not chosen:
            return AgentReply(
                "Please pick one of the listed times (e.g. `10:30`), or give another date.",
                state,
                slots=state.slots,
            )

        self._refresh_slots(state)
        if chosen not in state.slots:
            return AgentReply(
                f"{chosen} is not available for {state.doctor_name} on {state.day:%a %d %b %Y}. "
                "Please choose one of the free slots below.",
                state,
                slots=state.slots,
            )
        state.selected_slot = chosen
        state.stage = STAGE_CONFIRM
        return AgentReply(
            f"Please confirm: **{state.doctor_name}** on **{state.day:%a %d %b %Y}** at **{chosen}**. "
            "Shall I book and block this slot? (yes / no)",
            state,
            awaiting_confirmation=True,
        )

    def _handle_confirm(
        self, message: str, state: BookingState, *, patient_id: Optional[int], reason: str
    ) -> AgentReply:
        if _is_no(message):
            state.selected_slot = None
            state.stage = STAGE_SLOT
            self._refresh_slots(state)
            return AgentReply("No problem, nothing was booked. Pick another slot:", state, slots=state.slots)
        if not _is_yes(message):
            return AgentReply(
                "Please reply **yes** to confirm the booking or **no** to choose another slot.",
                state,
                awaiting_confirmation=True,
            )
        if not patient_id:
            return AgentReply(
                "Please select the patient for this appointment first, then confirm again.",
                state,
                awaiting_confirmation=True,
            )

        slot_start = datetime.combine(state.day, datetime.strptime(state.selected_slot, "%H:%M").time())
        try:
            appointment = appointment_service.book_appointment(
                self.session,
                self.user,
                patient_id=patient_id,
                doctor_id=state.doctor_id,
                slot_start=slot_start,
                reason=(reason or "").strip() or "Booked via booking assistant",
            )
        except ConflictError as exc:
            state.selected_slot = None
            state.stage = STAGE_SLOT
            self._refresh_slots(state)
            return AgentReply(f"{exc.message} Please choose another slot:", state, slots=state.slots)
        except HospitalError as exc:
            return AgentReply(f"Booking failed: {exc.message}", state, awaiting_confirmation=True)

        audit.record(
            self.session,
            actor=self.user.username,
            actor_role=self.user.role.value,
            action="booking_agent_confirmed",
            resource="appointment",
            resource_id=appointment.id,
        )
        self.session.commit()

        state.stage = STAGE_DONE
        state.appointment_reference = appointment.reference
        return AgentReply(
            f"✅ Booked! Reference **{appointment.reference}** with **{state.doctor_name}** on "
            f"**{slot_start:%a %d %b %Y}** at **{slot_start:%H:%M}**. The slot is now blocked for others.",
            state,
            booked=True,
            appointment_id=appointment.id,
        )

    # -- helpers -------------------------------------------------------
    def _refresh_slots(self, state: BookingState) -> None:
        slots = appointment_service.available_slots(self.session, self.user, state.doctor_id, state.day)
        state.slots = [s.strftime("%H:%M") for s in slots]

    def _offer_slots(self, state: BookingState) -> AgentReply:
        if state.day < date.today():
            state.stage = STAGE_SLOT
            state.slots = []
            return AgentReply("That date is in the past. Please give a future date.", state)
        self._refresh_slots(state)
        state.stage = STAGE_SLOT
        if not state.slots:
            return AgentReply(
                f"**{state.doctor_name}** has no free slots on {state.day:%a %d %b %Y}. "
                "Try another date (e.g. *\"next Monday\"* or *2026-11-05*).",
                state,
            )
        return AgentReply(
            f"**{state.doctor_name}** has {len(state.slots)} free slot(s) on **{state.day:%a %d %b %Y}**. "
            "Pick a time (click a slot or type e.g. `10:30`), or name another date.",
            state,
            slots=state.slots,
        )
