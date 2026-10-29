"""Admin: provider capabilities, retrieval settings, escalation matrix, audit."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from backend.ai.providers import provider_status
from backend.audit import logger as audit
from backend.copilot import orchestrator
from backend.core.config import get_settings
from rag import escalation_matrix
from streamlit_app.common import db, empty_state, kpi_row, page_header, require_login

st.set_page_config(page_title="Admin", page_icon="⚙️", layout="wide")
user = require_login("admin:config")
settings = get_settings()

page_header("⚙️ Administration", "Provider configuration, retrieval tuning, escalation routing and audit trail.")

tab_providers, tab_retrieval, tab_tools, tab_escalation, tab_audit, tab_workflow = st.tabs(
    ["Providers", "Retrieval", "Copilot tools", "Escalation matrix", "Audit trail", "Workflow"]
)

with tab_providers:
    st.caption(
        "Capabilities are declared per adapter - model IDs are never hard-coded in business logic. "
        "Keys are read from environment variables and are never displayed."
    )
    rows = provider_status(settings)
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    configured = [r["provider"] for r in rows if r["configured"]]
    kpi_row(
        [
            ("Provider order", " → ".join(settings.provider_order)),
            ("Configured", ", ".join(configured) or "none"),
            ("Max output tokens", settings.max_output_tokens),
            ("Session token budget", settings.session_token_budget),
        ]
    )
    if not configured:
        st.warning(
            "No provider is configured. The assistant will retrieve evidence and escalate "
            "instead of generating an answer. Set keys in `.env` to enable generation."
        )
    st.code(
        "AI_PROVIDER_ORDER=groq,gemini\n"
        "GROQ_API_KEY=...\nGROQ_MODEL=...\n"
        "GEMINI_API_KEY=...\nGEMINI_MODEL=...",
        language="bash",
    )

with tab_retrieval:
    st.caption("Current retrieval configuration (set through environment variables).")
    st.json(
        {
            "embedding_model": settings.embedding_model,
            "retrieval_top_k": settings.retrieval_top_k,
            "retrieval_candidates": settings.retrieval_candidates,
            "hybrid_alpha_vector_weight": settings.hybrid_alpha,
            "relevance_threshold": settings.relevance_threshold,
            "chunk_chars": settings.chunk_chars,
            "chunk_overlap": settings.chunk_overlap,
            "max_context_chars": settings.max_context_chars,
            "max_history_turns": settings.max_history_turns,
            "rate_limit_per_minute": settings.rate_limit_per_minute,
        }
    )

with tab_tools:
    with db() as session:
        catalog = orchestrator.tool_catalog(session, user)
    st.caption("Read-only tools exposed to the copilot. Mutations never run through the assistant.")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Tool": t["name"],
                    "Permitted for you": "yes" if t["permitted"] else "no",
                    "Description": t["description"],
                    "Args": ", ".join(f"{k}:{v}" for k, v in t["args"].items()) or "-",
                }
                for t in catalog
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

with tab_escalation:
    st.caption("Static, version-controlled role/topic contact matrix used for handoffs.")
    st.dataframe(pd.DataFrame(escalation_matrix.all_contacts()), use_container_width=True, hide_index=True)
    probe = st.text_input("Test a question for routing", placeholder="How do I claim insurance?")
    if probe:
        contact = escalation_matrix.resolve(probe, role=user.role.value)
        st.success(f"Topic `{contact.topic}` → {contact.desk} ({contact.contact})")

with tab_audit:
    limit = st.slider("Events to show", min_value=20, max_value=500, value=100, step=20)
    with db() as session:
        events = audit.recent(session, limit)
        rows = [
            {
                "ID": e.id,
                "At": e.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "Actor": e.actor,
                "Role": e.actor_role,
                "Action": e.action,
                "Resource": e.resource,
                "Resource ID": e.resource_id or "-",
                "Outcome": e.outcome,
                "Trace": e.trace_id or "-",
                "Detail": e.detail or "-",
            }
            for e in events
        ]
    if not rows:
        empty_state("No audit events recorded yet.")
    else:
        frame = pd.DataFrame(rows)
        action_filter = st.multiselect("Filter by action", sorted(frame["Action"].unique()))
        if action_filter:
            frame = frame[frame["Action"].isin(action_filter)]
        st.dataframe(frame, use_container_width=True, hide_index=True)
        st.download_button(
            "Export audit CSV", frame.to_csv(index=False), file_name="audit_events.csv", mime="text/csv"
        )

with tab_workflow:
    st.caption("The executable LangGraph path and the exported workflow use identical node names.")
    export_path = Path(__file__).resolve().parents[2] / "hospital-copilot-workflow-export.json"
    if export_path.exists():
        data = json.loads(export_path.read_text(encoding="utf-8"))
        kpi_row(
            [
                ("Workflow id", data.get("workflowId", "-")),
                ("Version", data.get("version", "-")),
                ("Nodes", len(data.get("nodes", []))),
                ("Edges", len(data.get("edges", []))),
            ]
        )
        st.dataframe(
            pd.DataFrame([{"Node": n["id"], "Type": n["type"]} for n in data.get("nodes", [])]),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "Download workflow export",
            export_path.read_text(encoding="utf-8"),
            file_name=export_path.name,
            mime="application/json",
        )
    else:
        empty_state("Workflow export file not found.")

