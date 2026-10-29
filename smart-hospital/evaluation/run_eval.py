"""Repeatable RAG evaluation harness.

Measures retrieval recall, answer faithfulness, citation correctness, abstention
accuracy, safety behaviour, latency and approximate token cost.

Run with::

    python -m evaluation.run_eval                 # retrieval + guardrails only
    python -m evaluation.run_eval --with-model    # also calls the configured provider
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

from backend.auth.rbac import CurrentUser
from backend.copilot import orchestrator
from backend.db.session import create_all, session_scope
from backend.guardrails.policy import estimate_tokens, screen_user_message
from backend.models import Role
from rag import retriever

DATASET = Path(__file__).with_name("dataset.json")
REPORT = Path(__file__).with_name("last_report.json")

# Rough public pricing anchors (USD per 1K tokens) for approximate cost only.
APPROX_COST_PER_1K = {"gemini": 0.0001, "groq": 0.00005}


def _corpus_for(case: Dict[str, Any]) -> str:
    return case.get("corpus", "operational_faq")


def evaluate_retrieval(session, cases: List[dict]) -> Dict[str, Any]:
    answerable = [c for c in cases if c["expect"] == "answerable"]
    doc_hits = keyword_hits = sufficient_hits = 0
    latencies: List[float] = []
    rows: List[dict] = []

    for case in answerable:
        started = time.perf_counter()
        result = retriever.search(session, case["question"], corpus=_corpus_for(case))
        latencies.append((time.perf_counter() - started) * 1000)

        doc_hit = any(c.title == case["expected_document"] for c in result.chunks)
        joined = " ".join(c.content.lower() for c in result.chunks)
        keyword_hit = any(kw.lower() in joined for kw in case.get("expected_keywords", []))

        doc_hits += doc_hit
        keyword_hits += keyword_hit
        sufficient_hits += result.sufficient
        rows.append(
            {
                "id": case["id"],
                "document_recall": doc_hit,
                "keyword_recall": keyword_hit,
                "sufficient": result.sufficient,
                "top_score": round(result.top_score, 4),
            }
        )

    total = max(1, len(answerable))
    return {
        "cases": len(answerable),
        "document_recall": round(doc_hits / total, 3),
        "keyword_recall": round(keyword_hits / total, 3),
        "evidence_sufficiency": round(sufficient_hits / total, 3),
        "p50_latency_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95_latency_ms": round(sorted(latencies)[int(len(latencies) * 0.95) - 1], 1) if latencies else 0.0,
        "detail": rows,
    }


def evaluate_abstention(session, cases: List[dict]) -> Dict[str, Any]:
    """Unanswerable questions must not retrieve sufficient evidence."""
    unanswerable = [c for c in cases if c["expect"] == "abstain"]
    correct = 0
    rows = []
    for case in unanswerable:
        result = retriever.search(session, case["question"], corpus=_corpus_for(case))
        abstained = not result.sufficient
        correct += abstained
        rows.append({"id": case["id"], "abstained": abstained, "top_score": round(result.top_score, 4)})
    total = max(1, len(unanswerable))
    return {"cases": len(unanswerable), "abstention_accuracy": round(correct / total, 3), "detail": rows}


def evaluate_guardrails(cases: List[dict]) -> Dict[str, Any]:
    """Blocked / clinical / emergency cases must never reach generation."""
    guarded = [c for c in cases if c["expect"] in ("blocked", "clinical", "emergency")]
    correct = 0
    rows = []
    for case in guarded:
        screen = screen_user_message(case["question"])
        expected = case["expect"]
        actual = screen.category
        hit = (not screen.allowed) and (
            actual == expected or (expected == "blocked" and actual in ("blocked", "clinical"))
        )
        correct += hit
        rows.append({"id": case["id"], "expected": expected, "actual": actual, "blocked": not screen.allowed})
    total = max(1, len(guarded))
    return {"cases": len(guarded), "guardrail_accuracy": round(correct / total, 3), "detail": rows}


def evaluate_generation(session, user: CurrentUser, cases: List[dict]) -> Dict[str, Any]:
    """Optional: exercises the full LangGraph path with a live provider."""
    answerable = [c for c in cases if c["expect"] == "answerable"]
    faithful = cited = 0
    tokens = 0
    latencies: List[float] = []
    providers: Dict[str, int] = {}
    rows = []

    for case in answerable:
        answer = orchestrator.ask(
            session, user, case["question"], session_id=f"eval-{case['id']}"
        )
        latencies.append(answer.elapsed_ms)
        tokens += answer.tokens_used
        if answer.provider:
            providers[answer.provider] = providers.get(answer.provider, 0) + 1

        is_answered = answer.status in ("answered", "answered_repaired")
        has_citation = bool(answer.sources) and "[S" in answer.answer
        faithful += is_answered and bool(answer.validation and answer.validation.get("valid"))
        cited += is_answered and has_citation
        rows.append(
            {
                "id": case["id"],
                "status": answer.status,
                "cited": has_citation,
                "repair_attempts": answer.repair_attempts,
                "tokens": answer.tokens_used,
                "elapsed_ms": answer.elapsed_ms,
            }
        )

    total = max(1, len(answerable))
    cost = sum(
        (tokens / 1000) * APPROX_COST_PER_1K.get(provider, 0.0001) * (count / total)
        for provider, count in providers.items()
    )
    return {
        "cases": len(answerable),
        "answer_faithfulness": round(faithful / total, 3),
        "citation_correctness": round(cited / total, 3),
        "total_tokens": tokens,
        "approx_cost_usd": round(cost, 5),
        "mean_latency_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        "providers_used": providers,
        "detail": rows,
    }


def seed_corpus_if_needed(session, user: CurrentUser) -> None:
    from rag import ingest

    if ingest.list_documents(session):
        return
    faq_dir = Path(__file__).resolve().parents[1] / "data" / "seed" / "faqs"
    mapping = {
        "visiting_hours.txt": ("Hospital Visiting Hours", "visiting", "operational_faq"),
        "appointments.txt": ("Appointment Booking Guide", "appointments", "operational_faq"),
        "admission_billing.txt": ("Admission, Billing and Discharge", "billing", "operational_faq"),
        "insurance_policy.txt": ("Insurance and Cashless Claims", "insurance", "insurance_policy"),
        "lab_preparation.txt": ("Laboratory Preparation Instructions", "laboratory", "clinical_information"),
    }
    for filename, (title, category, corpus) in mapping.items():
        path = faq_dir / filename
        if path.exists():
            ingest.ingest_document(
                session, user, filename=filename, data=path.read_bytes(),
                title=title, category=category, corpus=corpus, mime_type="text/plain",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Hospital FAQ RAG evaluation")
    parser.add_argument("--with-model", action="store_true", help="also call the configured AI provider")
    args = parser.parse_args()

    create_all()
    cases = json.loads(DATASET.read_text(encoding="utf-8"))["cases"]
    user = CurrentUser(username="eval-admin", role=Role.ADMIN)

    with session_scope() as session:
        seed_corpus_if_needed(session, user)
        report: Dict[str, Any] = {
            "dataset": DATASET.name,
            "total_cases": len(cases),
            "retrieval": evaluate_retrieval(session, cases),
            "abstention": evaluate_abstention(session, cases),
            "guardrails": evaluate_guardrails(cases),
        }
        if args.with_model:
            report["generation"] = evaluate_generation(session, user, cases)

    REPORT.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n=== Hospital FAQ RAG evaluation ===")
    print(f"Cases                 : {report['total_cases']}")
    r = report["retrieval"]
    print(f"Document recall       : {r['document_recall']:.1%}  ({r['cases']} answerable cases)")
    print(f"Keyword recall        : {r['keyword_recall']:.1%}")
    print(f"Evidence sufficiency  : {r['evidence_sufficiency']:.1%}")
    print(f"Retrieval p50 / p95   : {r['p50_latency_ms']} ms / {r['p95_latency_ms']} ms")
    print(f"Abstention accuracy   : {report['abstention']['abstention_accuracy']:.1%}")
    print(f"Guardrail accuracy    : {report['guardrails']['guardrail_accuracy']:.1%}")
    if "generation" in report:
        g = report["generation"]
        print(f"Answer faithfulness   : {g['answer_faithfulness']:.1%}")
        print(f"Citation correctness  : {g['citation_correctness']:.1%}")
        print(f"Tokens / approx cost  : {g['total_tokens']} / ${g['approx_cost_usd']}")
        print(f"Mean answer latency   : {g['mean_latency_ms']} ms")
    else:
        print("Generation metrics    : skipped (run with --with-model and a configured provider)")
    print(f"\nFull report written to {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

