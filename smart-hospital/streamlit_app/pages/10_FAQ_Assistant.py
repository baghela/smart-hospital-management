"""FAQ assistant: hybrid RAG chat with citations, evidence and escalation."""
from __future__ import annotations

import time

import streamlit as st

from backend.copilot import orchestrator
from backend.core.config import get_settings
from backend.guardrails.policy import TOKEN_BUDGET
from streamlit_app.common import db, page_header, require_login

st.set_page_config(page_title="FAQ Assistant", page_icon="💬", layout="wide")
user = require_login("faq:ask")
settings = get_settings()

page_header(
    "💬 Hospital FAQ assistant",
    "Answers come only from approved hospital documents, with citations. "
    "No diagnosis, no medication advice, no claim approvals.",
)

session_key = f"chat_{user.username}"
if session_key not in st.session_state:
    st.session_state[session_key] = []

with st.sidebar:
    st.markdown("### Assistant settings")
    corpus_hint = st.selectbox(
        "Knowledge area",
        ["auto", "operational_faq", "insurance_policy", "clinical_information"],
        help="Auto routes by question keywords. Operational FAQ is kept separate from clinical and policy content.",
    )
    provider_choice = st.multiselect(
        "Provider order",
        ["groq", "gemini"],
        default=[p for p in settings.provider_order if p in ("groq", "gemini")] or ["groq", "gemini"],
        help="Providers are tried in this order; configure API keys in .env",
    )
    stream_output = st.checkbox("Stream the response", value=True)
    st.caption(f"Token budget left: **{TOKEN_BUDGET.remaining(session_key)}**")
    if st.button("Clear chat", use_container_width=True):
        st.session_state[session_key] = []
        TOKEN_BUDGET.reset(session_key)
        st.rerun()

# --- transcript -----------------------------------------------------------
for message in st.session_state[session_key]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        meta = message.get("meta")
        if meta:
            if meta.get("status") == "provider_error" and meta.get("provider_error"):
                st.error(f"AI provider call failed: {meta['provider_error']}")
            elif meta.get("status") == "no_provider":
                reasons = "; ".join(f"{k}: {v}" for k, v in (meta.get("provider_diagnostics") or {}).items())
                st.error(f"No AI provider is usable. {reasons}")
            if meta.get("sources"):
                with st.expander(f"Sources ({len(meta['sources'])})"):
                    for source in meta["sources"]:
                        st.markdown(
                            f"**[{source['marker']}] {source['title']}** v{source['version']} — "
                            f"{source['section']} (p.{source['page']}) · score {source['score']}"
                        )
                        st.caption(source["preview"])
            if meta.get("escalation"):
                esc = meta["escalation"]
                st.warning(
                    f"Escalated to **{esc['desk']}** · {esc['contact']} · {esc['email']} "
                    f"({esc['hours']})" + (f" · ref `{esc.get('reference', '-')}`" if esc.get("reference") else "")
                )
            with st.expander("Execution trace"):
                st.json(
                    {
                        "engine": meta.get("engine"),
                        "status": meta.get("status"),
                        "provider": meta.get("provider"),
                        "model": meta.get("model"),
                        "corpus": meta.get("corpus"),
                        "repair_attempts": meta.get("repair_attempts"),
                        "tokens_used": meta.get("tokens_used"),
                        "elapsed_ms": meta.get("elapsed_ms"),
                        "validation": meta.get("validation"),
                        "tool_calls": meta.get("tool_calls"),
                        "nodes": [n["node"] for n in meta.get("node_trace", [])],
                        "trace_id": meta.get("trace_id"),
                        "provider_error": meta.get("provider_error"),
                        "provider_diagnostics": meta.get("provider_diagnostics"),
                    }
                )

# --- new question ---------------------------------------------------------
question = st.chat_input("Ask about visiting hours, appointments, admission, billing, insurance or lab preparation")

if question:
    st.session_state[session_key].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    history = [
        {"role": m["role"], "content": m["content"]} for m in st.session_state[session_key][:-1]
    ]
    forced_question = question
    if corpus_hint != "auto":
        forced_question = f"{question}"  # corpus routing stays deterministic in the graph

    with st.chat_message("assistant"):
        placeholder = st.empty()
        placeholder.markdown("_Retrieving approved documents..._")
        with db() as session:
            answer = orchestrator.ask(
                session,
                user,
                forced_question,
                session_id=session_key,
                history=history,
                provider_order=provider_choice or None,
            )

        if stream_output:
            buffer = ""
            for chunk in orchestrator.stream_answer(answer):
                buffer += chunk
                placeholder.markdown(buffer + "▌")
                time.sleep(0.01)
            placeholder.markdown(buffer)
        else:
            placeholder.markdown(answer.answer)

        meta = answer.to_dict()
        st.session_state[session_key].append(
            {"role": "assistant", "content": answer.answer, "meta": meta}
        )

        if answer.status in ("answered", "answered_repaired"):
            st.caption(
                f"Verified against {len(answer.sources)} cited passage(s) · "
                f"{answer.provider}/{answer.model} · {answer.elapsed_ms} ms · {answer.tokens_used} tokens"
            )
        elif answer.status == "no_provider":
            reasons = "; ".join(f"{k}: {v}" for k, v in (answer.provider_diagnostics or {}).items())
            st.info(
                "No AI provider is usable, so the assistant escalated instead of guessing. "
                "Add `GROQ_API_KEY` or `GEMINI_API_KEY` to `.env` and restart Streamlit."
                + (f"\n\nDetails: {reasons}" if reasons else "")
            )
        elif answer.status == "provider_error":
            st.error(f"AI provider call failed: {answer.provider_error}")

        col1, col2 = st.columns(2)
        if col1.button("👍 Helpful", key=f"up_{len(st.session_state[session_key])}"):
            with db() as session:
                orchestrator.record_feedback(
                    session, user, question=question, answer=answer.answer, helpful=True
                )
            st.success("Thanks for the feedback.")
        if col2.button("👎 Not helpful", key=f"down_{len(st.session_state[session_key])}"):
            with db() as session:
                orchestrator.record_feedback(
                    session, user, question=question, answer=answer.answer, helpful=False
                )
            st.success("Recorded - this answer will be reviewed.")
    st.rerun()

