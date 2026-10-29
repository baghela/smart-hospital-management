"""Evaluation regression tests - keeps retrieval quality from silently drifting."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.auth.rbac import CurrentUser
from backend.models import Role
from evaluation.run_eval import (
    DATASET,
    evaluate_abstention,
    evaluate_guardrails,
    evaluate_retrieval,
    seed_corpus_if_needed,
)

MIN_DOCUMENT_RECALL = 0.95
MIN_KEYWORD_RECALL = 0.95
MIN_EVIDENCE_SUFFICIENCY = 0.95
MIN_ABSTENTION_ACCURACY = 1.0
MIN_GUARDRAIL_ACCURACY = 1.0
# The abstention threshold must sit inside a comfortable gap, not scrape past it.
MIN_SEPARATION_MARGIN = 0.15


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(Path(DATASET).read_text(encoding="utf-8"))["cases"]


@pytest.fixture
def eval_session(session):
    user = CurrentUser(username="eval-admin", role=Role.ADMIN)
    seed_corpus_if_needed(session, user)
    return session


def test_dataset_is_well_formed(cases):
    assert len(cases) >= 20
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    expectations = {c["expect"] for c in cases}
    assert expectations <= {"answerable", "abstain", "blocked", "clinical", "emergency"}
    for case in cases:
        if case["expect"] == "answerable":
            assert case["expected_document"] and case["expected_keywords"]


def test_retrieval_recall_meets_threshold(eval_session, cases):
    report = evaluate_retrieval(eval_session, cases)
    assert report["document_recall"] >= MIN_DOCUMENT_RECALL, report
    assert report["keyword_recall"] >= MIN_KEYWORD_RECALL, report
    assert report["evidence_sufficiency"] >= MIN_EVIDENCE_SUFFICIENCY, report


def test_retrieval_latency_is_reasonable(eval_session, cases):
    report = evaluate_retrieval(eval_session, cases)
    assert report["p95_latency_ms"] < 2000, report


def test_abstention_on_unanswerable_questions(eval_session, cases):
    report = evaluate_abstention(eval_session, cases)
    assert report["abstention_accuracy"] >= MIN_ABSTENTION_ACCURACY, report


def test_guardrails_catch_every_unsafe_case(cases):
    report = evaluate_guardrails(cases)
    assert report["guardrail_accuracy"] >= MIN_GUARDRAIL_ACCURACY, report


def test_answerable_scores_separate_from_unanswerable(eval_session, cases):
    answerable = evaluate_retrieval(eval_session, cases)["detail"]
    unanswerable = evaluate_abstention(eval_session, cases)["detail"]
    worst_answerable = min(r["top_score"] for r in answerable)
    best_unanswerable = max(r["top_score"] for r in unanswerable)
    margin = worst_answerable - best_unanswerable
    assert margin >= MIN_SEPARATION_MARGIN, {
        "worst_answerable": worst_answerable,
        "best_unanswerable": best_unanswerable,
        "margin": round(margin, 4),
    }


def test_configured_threshold_sits_inside_the_margin(eval_session, cases):
    """The shipped RELEVANCE_THRESHOLD must separate both populations."""
    from backend.core.config import get_settings

    threshold = get_settings().relevance_threshold
    answerable = evaluate_retrieval(eval_session, cases)["detail"]
    unanswerable = evaluate_abstention(eval_session, cases)["detail"]
    assert max(r["top_score"] for r in unanswerable) < threshold <= min(
        r["top_score"] for r in answerable
    )


def test_stemming_matches_inflected_forms():
    """'visit' must match 'visiting' or answerable questions score too low."""
    from rag.retriever import stem

    assert stem("visiting") == stem("visit") == "visit"
    assert stem("rescheduled") == stem("reschedule").rstrip("e") or stem("rescheduled")
    assert stem("policies").startswith("polic")
    assert stem("icu") == "icu"  # too short to strip


def test_generic_domain_words_do_not_create_false_relevance():
    """A question that only shares the word 'hospital' must not look relevant."""
    from rag.retriever import absolute_relevance

    relevance, coverage = absolute_relevance(
        "What is the hospital share price on the stock exchange?",
        "General ward visiting hours are 11:00 to 13:00 every day at the hospital.",
        raw_cosine=0.3,
    )
    assert coverage == 0.0
    assert relevance < 0.25

