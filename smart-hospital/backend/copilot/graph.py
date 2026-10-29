"""LangGraph execution graph for the hospital FAQ copilot.

The graph mirrors ``hospital-copilot-workflow-export.json`` node-for-node so the
exported workflow and the executable code are the same path:

    workflow_start -> auth_rbac_gate -> input_guardrail_scan -> intent_router
      -> policy_resource_loader -> hybrid_retrieval_loop
      -> evidence_sufficiency_branch -> token_budget_check -> provider_selector
      -> grounded_answer_generation -> citation_validator
      -> (conservative_rewrite loop | escalation_router) -> answer_preview
      -> feedback_capture -> audit_persist -> complete_end

If ``langgraph`` is installed the compiled ``StateGraph`` is used; otherwise an
equivalent deterministic runner executes the identical node functions, so the
module always runs locally.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from backend.ai.providers import ProviderRouter
from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.copilot.prompts import REPAIR_PROMPT, SYSTEM_PROMPT, build_user_prompt
from backend.copilot.tools import ToolRegistry
from backend.core.config import get_settings
from backend.core.errors import ProviderError
from backend.core.logging import get_logger
from backend.guardrails.policy import (
    CLINICAL_BOUNDARY_MESSAGE,
    TOKEN_BUDGET,
    bound_history,
    estimate_tokens,
    screen_user_message,
)
from rag import escalation_matrix
from rag.citations import ABSTAIN_MESSAGE, format_sources, strip_unsupported, validate_answer
from rag.retriever import RetrievedChunk, search

logger = get_logger("copilot.graph")

MAX_REPAIR_ATTEMPTS = 2

State = Dict[str, Any]


# --------------------------------------------------------------------------
# Node implementations (pure functions over the state dict)
# --------------------------------------------------------------------------
def _trace(state: State, node: str, detail: str = "") -> None:
    state.setdefault("node_trace", []).append({"node": node, "detail": detail})


def workflow_start(state: State) -> State:
    state.setdefault("trace_id", uuid.uuid4().hex[:12])
    state.setdefault("repair_attempts", 0)
    state.setdefault("started_at", time.perf_counter())
    _trace(state, "workflow_start")
    return state


def auth_rbac_gate(state: State) -> State:
    user: CurrentUser = state["user"]
    if not user.has("faq:ask"):
        state.update(
            route="blocked",
            answer="Your role is not permitted to use the FAQ assistant.",
            status="denied",
        )
    _trace(state, "auth_rbac_gate", user.role.value)
    return state


def input_guardrail_scan(state: State) -> State:
    if state.get("route") == "blocked":
        return state
    screen = screen_user_message(state["question"])
    state["guardrail_flags"] = screen.flags
    if not screen.allowed:
        contact = escalation_matrix.resolve(
            state["question"],
            role=state["user"].role.value,
            emergency=screen.category == "emergency",
        )
        state.update(
            route="clinical_boundary_block" if screen.category == "clinical" else "guardrail_block",
            answer=screen.message or CLINICAL_BOUNDARY_MESSAGE,
            status=screen.category,
            escalation=contact.as_dict(),
        )
    _trace(state, "input_guardrail_scan", ",".join(screen.flags) or "clean")
    return state


def intent_router(state: State) -> State:
    """Deterministic routing between corpora and database tools - zero tokens."""
    if state.get("route"):
        return state
    question = state["question"].lower()
    corpus = "operational_faq"
    if any(k in question for k in ("insurance", "claim", "policy", "cashless", "tpa", "pre-auth")):
        corpus = "insurance_policy"
    elif any(k in question for k in ("preparation", "fasting", "before the test", "lab instruction")):
        corpus = "clinical_information"
    state["corpus"] = corpus
    state["topic"] = escalation_matrix.classify_topic(state["question"])
    _trace(state, "intent_router", f"{corpus}:{state['topic']}")
    return state


def policy_resource_loader(state: State) -> State:
    """Loads the tool catalogue the caller is actually allowed to use."""
    if state.get("route"):
        return state
    registry: ToolRegistry = state["registry"]
    state["tool_catalog"] = [t["name"] for t in registry.describe() if t["permitted"]]
    _trace(state, "policy_resource_loader", f"tools={len(state['tool_catalog'])}")
    return state


def hospital_tool_registry(state: State) -> State:
    """Invoke read-only database tools when the question references records."""
    if state.get("route"):
        return state
    registry: ToolRegistry = state["registry"]
    question = state["question"].lower()
    facts: List[str] = []

    if any(k in question for k in ("bed", "ward", "admission availability", "beds free")):
        result = registry.invoke("get_bed_availability")
        if "error" not in result:
            facts.append("bed_availability=" + json.dumps(result["wards"]))

    invoice_ref = _extract_token(state["question"], "INV-")
    if invoice_ref:
        result = registry.invoke("get_invoice_summary", invoice_no=invoice_ref)
        facts.append(f"invoice_{invoice_ref}=" + json.dumps(result))

    claim_ref = _extract_token(state["question"], "CLM-")
    if claim_ref:
        result = registry.invoke("get_claim_status", claim_no=claim_ref)
        facts.append(f"claim_{claim_ref}=" + json.dumps(result))

    if state["user"].patient_id and "my appointment" in question:
        result = registry.invoke("get_patient_appointments", patient_id=state["user"].patient_id)
        facts.append("my_appointments=" + json.dumps(result))

    state["tool_facts"] = "\n".join(facts)[: get_settings().max_context_chars // 2]
    state["tool_calls"] = registry.calls
    _trace(state, "hospital_tool_registry", f"calls={len(registry.calls)}")
    return state


def hybrid_retrieval_loop(state: State) -> State:
    if state.get("route"):
        return state
    session: Session = state["session"]
    settings = get_settings()
    result = search(session, state["question"], corpus=state["corpus"], top_k=settings.retrieval_top_k)

    # Widen once if the first pass is weak (bounded retry, not an LLM loop).
    if not result.sufficient and state["corpus"] != "operational_faq":
        fallback = search(session, state["question"], corpus="operational_faq", top_k=settings.retrieval_top_k)
        if fallback.top_score > result.top_score:
            result = fallback
            state["corpus"] = "operational_faq"

    state["chunks"] = result.chunks
    state["top_score"] = result.top_score
    state["evidence_sufficient"] = result.sufficient
    _trace(state, "hybrid_retrieval_loop", f"hits={len(result.chunks)} top={result.top_score:.3f}")
    return state


def evidence_sufficiency_branch(state: State) -> State:
    if state.get("route"):
        return state
    if not state.get("evidence_sufficient"):
        state["route"] = "escalation_router"
    _trace(state, "evidence_sufficiency_branch", str(state.get("evidence_sufficient")))
    return state


def token_budget_check(state: State) -> State:
    if state.get("route"):
        return state
    settings = get_settings()
    chunks: List[RetrievedChunk] = state["chunks"]
    evidence = "\n\n".join(
        f"[S{i}] {c.citation()}\n{c.content}" for i, c in enumerate(chunks, start=1)
    )[: settings.max_context_chars]
    state["evidence"] = evidence
    projected = estimate_tokens(SYSTEM_PROMPT + evidence + state["question"]) + settings.max_output_tokens
    try:
        TOKEN_BUDGET.check(state["session_id"], projected)
    except Exception as exc:  # GuardrailError
        state.update(route="budget_block", answer=str(exc), status="budget_exceeded")
    state["projected_tokens"] = projected
    _trace(state, "token_budget_check", f"projected={projected}")
    return state


def provider_selector(state: State) -> State:
    if state.get("route"):
        return state
    router: ProviderRouter = state["router"]
    available = router.available()
    state["provider_chain"] = [p.name for p in available]
    if not available:
        diagnostics = router.diagnose()
        state.update(route="escalation_router", status="no_provider", provider_diagnostics=diagnostics)
        logger.warning(
            "no_ai_provider_available: " + "; ".join(f"{k}: {v}" for k, v in diagnostics.items()),
            extra={"event": "no_provider", "trace_id": state.get("trace_id")},
        )
    _trace(state, "provider_selector", ",".join(state["provider_chain"]) or "none")
    return state


def grounded_answer_generation(state: State) -> State:
    if state.get("route"):
        return state
    router: ProviderRouter = state["router"]
    settings = get_settings()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in bound_history(state.get("history", [])):
        messages.append({"role": turn["role"], "content": turn["content"][:600]})
    messages.append(
        {
            "role": "user",
            "content": build_user_prompt(state["question"], state["evidence"], state.get("tool_facts", "")),
        }
    )
    try:
        result = router.complete(messages, max_tokens=settings.max_output_tokens)
    except ProviderError as exc:
        state.update(route="escalation_router", status="provider_error", provider_error=exc.message)
        logger.error(
            f"answer_generation_failed: {exc.message}",
            extra={"event": "provider_error", "trace_id": state.get("trace_id")},
        )
        _trace(state, "grounded_answer_generation", "provider_error")
        return state

    TOKEN_BUDGET.consume(state["session_id"], result.total_tokens)
    state.update(
        draft=result.text,
        provider=result.provider,
        model=result.model,
        tokens_used=result.total_tokens,
        latency_ms=result.latency_ms,
        fallback_used=result.fallback_used,
    )
    _trace(state, "grounded_answer_generation", f"{result.provider}:{result.total_tokens}t")
    return state


def citation_validator(state: State) -> State:
    if state.get("route"):
        return state
    draft = (state.get("draft") or "").strip()
    if "INSUFFICIENT_EVIDENCE" in draft.upper():
        state["route"] = "escalation_router"
        state["status"] = "abstained"
        _trace(state, "citation_validator", "model_abstained")
        return state

    report = validate_answer(draft, state["chunks"])
    state["validation"] = report.as_dict()
    if report.valid:
        state["answer"] = draft
        state["status"] = "answered"
        state["route"] = "answer_preview"
    elif state["repair_attempts"] < MAX_REPAIR_ATTEMPTS:
        state["route"] = "conservative_rewrite"
    else:
        repaired = strip_unsupported(draft, report)
        if repaired and validate_answer(repaired, state["chunks"]).valid:
            state.update(answer=repaired, status="answered_repaired", route="answer_preview")
        else:
            state["route"] = "escalation_router"
            state["status"] = "unverified"
    _trace(state, "citation_validator", report.reason or "valid")
    return state


def conservative_rewrite(state: State) -> State:
    state["repair_attempts"] += 1
    router: ProviderRouter = state["router"]
    settings = get_settings()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_user_prompt(state["question"], state["evidence"], state.get("tool_facts", "")),
        },
        {"role": "assistant", "content": state.get("draft", "")},
        {"role": "user", "content": REPAIR_PROMPT},
    ]
    try:
        result = router.complete(messages, max_tokens=settings.max_output_tokens)
        TOKEN_BUDGET.consume(state["session_id"], result.total_tokens)
        state["draft"] = result.text
        state["tokens_used"] = state.get("tokens_used", 0) + result.total_tokens
    except ProviderError as exc:
        logger.error(
            f"conservative_rewrite_failed: {exc.message}",
            extra={"event": "provider_error", "trace_id": state.get("trace_id")},
        )
        state["route"] = "escalation_router"
        state["status"] = "provider_error"
        state["provider_error"] = exc.message
    else:
        state["route"] = None  # back to citation_validator
    _trace(state, "conservative_rewrite", f"attempt={state['repair_attempts']}")
    return state


def escalation_router(state: State) -> State:
    """Role-mapped handoff when the answer cannot be verified."""
    contact = escalation_matrix.resolve(state["question"], role=state["user"].role.value)
    reference = escalation_matrix.ticket_reference(contact.topic, state["question"])
    state["escalation"] = {**contact.as_dict(), "reference": reference}
    if not state.get("status"):
        state["status"] = "escalated"
    state["answer"] = f"{ABSTAIN_MESSAGE}\n\n{contact.message()}\nReference: `{reference}`"
    state["route"] = "escalation_end"
    _trace(state, "escalation_router", contact.topic)
    return state


def answer_preview(state: State) -> State:
    state["sources"] = format_sources(state.get("chunks", []))
    state["route"] = "feedback_capture"
    _trace(state, "answer_preview", f"sources={len(state['sources'])}")
    return state


def feedback_capture(state: State) -> State:
    state["feedback_enabled"] = True
    state["route"] = "audit_persist"
    _trace(state, "feedback_capture")
    return state


def audit_persist(state: State) -> State:
    session: Session = state["session"]
    user: CurrentUser = state["user"]
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="copilot_answer",
        resource="faq_chat",
        outcome=state.get("status", "unknown"),
        trace_id=state.get("trace_id"),
        detail=f"provider={state.get('provider')};tokens={state.get('tokens_used', 0)}",
    )
    session.commit()
    state["route"] = "complete_end"
    state["elapsed_ms"] = int((time.perf_counter() - state["started_at"]) * 1000)
    _trace(state, "audit_persist")
    return state


def _extract_token(text: str, prefix: str) -> Optional[str]:
    for word in text.replace(",", " ").split():
        token = word.strip(".,;:()[]").upper()
        if token.startswith(prefix) and len(token) > len(prefix):
            return token
    return None


# --------------------------------------------------------------------------
# Graph assembly
# --------------------------------------------------------------------------
NODE_SEQUENCE: List[tuple[str, Callable[[State], State]]] = [
    ("workflow_start", workflow_start),
    ("auth_rbac_gate", auth_rbac_gate),
    ("input_guardrail_scan", input_guardrail_scan),
    ("intent_router", intent_router),
    ("policy_resource_loader", policy_resource_loader),
    ("hospital_tool_registry", hospital_tool_registry),
    ("hybrid_retrieval_loop", hybrid_retrieval_loop),
    ("evidence_sufficiency_branch", evidence_sufficiency_branch),
    ("token_budget_check", token_budget_check),
    ("provider_selector", provider_selector),
    ("grounded_answer_generation", grounded_answer_generation),
    ("citation_validator", citation_validator),
]

TERMINAL_ROUTES = {"blocked", "guardrail_block", "clinical_boundary_block", "budget_block"}


def build_graph():
    """Compile the LangGraph ``StateGraph``; returns ``None`` if unavailable."""
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:  # pragma: no cover - optional dependency
        logger.info("langgraph_not_installed", extra={"event": "fallback_runner"})
        return None

    graph = StateGraph(dict)
    for name, fn in NODE_SEQUENCE:
        graph.add_node(name, fn)
    graph.add_node("conservative_rewrite", conservative_rewrite)
    graph.add_node("escalation_router", escalation_router)
    graph.add_node("answer_preview", answer_preview)
    graph.add_node("feedback_capture", feedback_capture)
    graph.add_node("audit_persist", audit_persist)

    graph.add_edge(START, "workflow_start")

    def guard_branch(state: State) -> str:
        route = state.get("route")
        if route in TERMINAL_ROUTES:
            return "audit_persist"
        if route == "escalation_router":
            return "escalation_router"
        return "continue"

    # Linear spine with early-exit guards after each gate.
    for index, (name, _fn) in enumerate(NODE_SEQUENCE[:-1]):
        nxt = NODE_SEQUENCE[index + 1][0]
        graph.add_conditional_edges(
            name,
            guard_branch,
            {"continue": nxt, "escalation_router": "escalation_router", "audit_persist": "audit_persist"},
        )

    def validator_branch(state: State) -> str:
        route = state.get("route")
        if route == "conservative_rewrite":
            return "conservative_rewrite"
        if route == "escalation_router":
            return "escalation_router"
        return "answer_preview"

    graph.add_conditional_edges(
        "citation_validator",
        validator_branch,
        {
            "conservative_rewrite": "conservative_rewrite",
            "escalation_router": "escalation_router",
            "answer_preview": "answer_preview",
        },
    )
    graph.add_conditional_edges(
        "conservative_rewrite",
        lambda s: "escalation_router" if s.get("route") == "escalation_router" else "citation_validator",
        {"citation_validator": "citation_validator", "escalation_router": "escalation_router"},
    )
    graph.add_edge("escalation_router", "audit_persist")
    graph.add_edge("answer_preview", "feedback_capture")
    graph.add_edge("feedback_capture", "audit_persist")
    graph.add_edge("audit_persist", END)
    return graph.compile()


def run_fallback(state: State) -> State:
    """Deterministic executor following the identical node path."""
    for name, fn in NODE_SEQUENCE:
        state = fn(state)
        route = state.get("route")
        if route in TERMINAL_ROUTES:
            return audit_persist(state)
        if route == "escalation_router":
            return audit_persist(escalation_router(state))

    while True:
        route = state.get("route")
        if route == "conservative_rewrite":
            state = conservative_rewrite(state)
            if state.get("route") == "escalation_router":
                return audit_persist(escalation_router(state))
            state = citation_validator(state)
            continue
        if route == "escalation_router":
            return audit_persist(escalation_router(state))
        if route == "answer_preview":
            state = answer_preview(state)
            state = feedback_capture(state)
            return audit_persist(state)
        return audit_persist(state)

