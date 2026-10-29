"""Citation parsing and evidence validation.

An answer is only accepted when every claim-bearing sentence carries a valid
``[S<n>]`` marker pointing at retrieved evidence, and the cited passage shares
meaningful vocabulary with the sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Sequence

from rag.embeddings import tokenize
from rag.retriever import RetrievedChunk

_MARKER_RE = re.compile(r"\[S(\d{1,2})\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "for", "of", "and", "or", "you", "your",
    "we", "our", "can", "will", "please", "at", "in", "on", "with", "be", "it",
    "this", "that", "from", "as", "by", "if", "not", "do", "does", "has", "have",
}

ABSTAIN_MESSAGE = (
    "I could not verify this from the approved hospital documents, so I will not "
    "guess. Please contact the desk listed below for a confirmed answer."
)


@dataclass
class ValidationReport:
    valid: bool
    cited_indices: List[int] = field(default_factory=list)
    unsupported_sentences: List[str] = field(default_factory=list)
    invalid_markers: List[int] = field(default_factory=list)
    coverage: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "valid": self.valid,
            "cited_sources": self.cited_indices,
            "unsupported_sentences": self.unsupported_sentences,
            "invalid_markers": self.invalid_markers,
            "coverage": round(self.coverage, 3),
            "reason": self.reason,
        }


def extract_markers(text: str) -> List[int]:
    return [int(m) for m in _MARKER_RE.findall(text or "")]


def _content_terms(text: str) -> set:
    return {t for t in tokenize(text) if t not in _STOPWORDS and len(t) > 2}


def validate_answer(
    answer: str, chunks: Sequence[RetrievedChunk], *, min_overlap: float = 0.4
) -> ValidationReport:
    """Validate that the answer is grounded in the retrieved chunks."""
    if not answer or not answer.strip():
        return ValidationReport(False, reason="empty_answer")
    if not chunks:
        return ValidationReport(False, reason="no_evidence")

    markers = extract_markers(answer)
    valid_range = set(range(1, len(chunks) + 1))
    invalid = sorted({m for m in markers if m not in valid_range})
    if not markers:
        return ValidationReport(False, reason="no_citations")

    sentences = [s.strip() for s in _SENTENCE_RE.split(answer.strip()) if s.strip()]
    unsupported: List[str] = []
    supported = 0

    for sentence in sentences:
        sentence_markers = [m for m in extract_markers(sentence) if m in valid_range]
        terms = _content_terms(sentence)
        if len(terms) < 3:
            continue  # short connector sentence, not a factual claim
        if not sentence_markers:
            unsupported.append(sentence)
            continue
        evidence_terms: set = set()
        for marker in sentence_markers:
            evidence_terms |= _content_terms(chunks[marker - 1].content)
        overlap = len(terms & evidence_terms) / max(1, len(terms))
        if overlap >= min_overlap:
            supported += 1
        else:
            unsupported.append(sentence)

    claim_count = supported + len(unsupported)
    coverage = supported / claim_count if claim_count else 0.0
    valid = not invalid and not unsupported and coverage > 0
    reason = ""
    if invalid:
        reason = "invalid_citation_marker"
    elif unsupported:
        reason = "unsupported_claims"
    elif coverage == 0:
        reason = "no_verifiable_claims"

    return ValidationReport(
        valid=valid,
        cited_indices=sorted({m for m in markers if m in valid_range}),
        unsupported_sentences=unsupported,
        invalid_markers=invalid,
        coverage=coverage,
        reason=reason,
    )


def strip_unsupported(answer: str, report: ValidationReport) -> str:
    """Conservative repair: remove sentences that evidence does not support."""
    cleaned = answer
    for sentence in report.unsupported_sentences:
        cleaned = cleaned.replace(sentence, "").strip()
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def format_sources(chunks: Sequence[RetrievedChunk]) -> List[dict]:
    return [
        {
            "marker": f"S{index}",
            "title": chunk.title,
            "section": chunk.section,
            "page": chunk.page,
            "version": chunk.version,
            "score": round(chunk.score, 4),
            "preview": chunk.content[:320] + ("..." if len(chunk.content) > 320 else ""),
        }
        for index, chunk in enumerate(chunks, start=1)
    ]

