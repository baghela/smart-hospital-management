"""Copilot orchestrator: single entry point used by the API and the UI."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from backend.ai.providers import ProviderRouter
from backend.auth.rbac import CurrentUser
from backend.copilot.graph import build_graph, run_fallback
from backend.copilot.tools import ToolRegistry
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.guardrails.policy import RATE_LIMITER, TOKEN_BUDGET, bound_history
from backend.models import ChatFeedback

logger = get_logger("copilot.orchestrator")

_COMPILED_GRAPH = None


def compiled_graph():
    """Lazily compile (and cache) the LangGraph app."""
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        _COMPILED_GRAPH = build_graph()
    return _COMPILED_GRAPH


@dataclass
class CopilotAnswer:
    answer: str
    status: str
    sources: List[dict] = field(default_factory=list)
    escalation: Optional[dict] = None
    validation: Optional[dict] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    corpus: str = "operational_faq"
    tokens_used: int = 0
    tokens_remaining: int = 0
    elapsed_ms: int = 0
    repair_attempts: int = 0
    fallback_used: bool = False
    tool_calls: List[dict] = field(default_factory=list)
    node_trace: List[dict] = field(default_factory=list)
    trace_id: str = ""
    engine: str = "langgraph"
    provider_error: Optional[str] = None
    provider_diagnostics: Optional[dict] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def ask(
    session: Session,
    user: CurrentUser,
    question: str,
    *,
    session_id: str = "default",
    history: Optional[List[dict]] = None,
    provider_order: Optional[List[str]] = None,
) -> CopilotAnswer:
    """Run the full guarded RAG workflow and return a validated answer."""
    settings = get_settings()
    RATE_LIMITER.check(f"{user.username}:copilot")

    registry = ToolRegistry(session, user)
    state: Dict[str, Any] = {
        "session": session,
        "user": user,
        "registry": registry,
        "router": ProviderRouter(provider_order or settings.provider_order, settings),
        "question": question,
        "session_id": session_id,
        "history": bound_history(history or []),
        "route": None,
        "status": "",
        "answer": "",
    }

    app = compiled_graph()
    if app is not None:
        final = app.invoke(state, {"recursion_limit": 40})
        engine = "langgraph"
    else:
        final = run_fallback(state)
        engine = "deterministic-fallback"

    return CopilotAnswer(
        answer=final.get("answer", ""),
        status=final.get("status", "unknown"),
        sources=final.get("sources", []),
        escalation=final.get("escalation"),
        validation=final.get("validation"),
        provider=final.get("provider"),
        model=final.get("model"),
        corpus=final.get("corpus", "operational_faq"),
        tokens_used=final.get("tokens_used", 0),
        tokens_remaining=TOKEN_BUDGET.remaining(session_id),
        elapsed_ms=final.get("elapsed_ms", 0),
        repair_attempts=final.get("repair_attempts", 0),
        fallback_used=final.get("fallback_used", False),
        tool_calls=final.get("tool_calls", []),
        node_trace=final.get("node_trace", []),
        trace_id=final.get("trace_id", ""),
        engine=engine,
        provider_error=final.get("provider_error"),
        provider_diagnostics=final.get("provider_diagnostics"),
    )


def stream_answer(answer: CopilotAnswer, chunk_size: int = 48) -> Iterator[str]:
    """Stream a validated answer to the UI.

    Streaming happens *after* citation validation on purpose: unverified text is
    never shown to a user.
    """
    text = answer.answer
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def record_feedback(
    session: Session,
    user: CurrentUser,
    *,
    question: str,
    answer: str,
    helpful: bool,
    notes: str = "",
) -> ChatFeedback:
    entry = ChatFeedback(
        username=user.username,
        question=question[:500],
        answer_preview=answer[:500],
        helpful=helpful,
        notes=notes[:500] or None,
    )
    session.add(entry)
    session.commit()
    return entry


def tool_catalog(session: Session, user: CurrentUser) -> List[dict]:
    return ToolRegistry(session, user).describe()

