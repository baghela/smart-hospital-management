"""Hybrid retrieval: BM25 keyword scoring + dense vector similarity.

Candidate merging uses reciprocal-rank fusion plus a weighted score blend, with
optional lexical reranking, metadata filtering and a relevance threshold so the
assistant can abstain when evidence is weak.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.models import DocumentStatus, FaqChunk, FaqDocument
from rag.embeddings import cosine, get_embedder, tokenize


@dataclass
class RetrievedChunk:
    chunk_id: int
    document_id: int
    title: str
    category: str
    corpus: str
    version: str
    section: str
    page: int
    content: str
    bm25: float = 0.0
    vector: float = 0.0
    score: float = 0.0
    relevance: float = 0.0
    coverage: float = 0.0
    rank: int = 0

    def citation(self) -> str:
        return f"{self.title} v{self.version} - {self.section} (p.{self.page})"

    def as_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "title": self.title,
            "section": self.section,
            "page": self.page,
            "version": self.version,
            "corpus": self.corpus,
            "category": self.category,
            "score": round(self.score, 4),
            "relevance": round(self.relevance, 4),
            "bm25": round(self.bm25, 4),
            "vector": round(self.vector, 4),
            "content": self.content,
            "citation": self.citation(),
        }


@dataclass
class RetrievalResult:
    query: str
    chunks: List[RetrievedChunk] = field(default_factory=list)
    top_score: float = 0.0
    sufficient: bool = False
    corpus: str = "operational_faq"

    def context_text(self, max_chars: int) -> str:
        parts, used = [], 0
        for index, chunk in enumerate(self.chunks, start=1):
            block = f"[S{index}] {chunk.citation()}\n{chunk.content}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)


class BM25Index:
    """Compact in-memory BM25 (Okapi) over the active corpus."""

    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [list(d) for d in docs]
        self.doc_len = [len(d) for d in self.docs]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.freqs = [Counter(d) for d in self.docs]
        self.df: Counter = Counter()
        for freq in self.freqs:
            self.df.update(freq.keys())
        self.n = len(self.docs)

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_terms: Sequence[str], index: int) -> float:
        if not self.avgdl:
            return 0.0
        freq = self.freqs[index]
        length = self.doc_len[index]
        total = 0.0
        for term in query_terms:
            tf = freq.get(term, 0)
            if not tf:
                continue
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * length / self.avgdl)
            total += self.idf(term) * numerator / denominator
        return total

    def scores(self, query_terms: Sequence[str]) -> List[float]:
        return [self.score(query_terms, i) for i in range(self.n)]


def _normalise(values: Sequence[float]) -> List[float]:
    if not values:
        return []
    top = max(values)
    return [v / top if top > 0 else 0.0 for v in values]


# Generic words carry no retrieval signal, so they are excluded from coverage.
# "hospital"-type domain words appear in every document and would otherwise give
# a false coverage score to completely unrelated questions.
_COVERAGE_STOPWORDS = {
    "what", "when", "where", "which", "who", "how", "the", "a", "an", "is", "are",
    "do", "does", "did", "can", "could", "should", "would", "will", "i", "my", "me",
    "we", "our", "you", "your", "for", "to", "of", "in", "on", "at", "and", "or",
    "be", "it", "this", "that", "from", "with", "there", "here", "any", "about",
    "need", "needed", "get", "take", "much", "many", "long", "hours", "time",
    "hospital", "please", "tell", "know", "someone", "available", "possible",
}

_SUFFIXES = ("ements", "ement", "ations", "ation", "ings", "ing", "ies", "ed", "es", "s")


def stem(token: str) -> str:
    """Very light suffix stripping so 'visit' matches 'visiting'.

    A full stemmer is overkill here and would add a dependency; this handles the
    inflections that actually appear in hospital policy text.
    """
    for suffix in _SUFFIXES:
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            base = token[: -len(suffix)]
            return base[:-1] if suffix == "ies" and base.endswith("i") else base
    return token


def _content_terms(text: str) -> set:
    return {
        stem(t) for t in tokenize(text) if t not in _COVERAGE_STOPWORDS and len(t) > 2
    }


def absolute_relevance(query: str, chunk_text: str, raw_cosine: float) -> tuple[float, float]:
    """Scale-stable relevance used for the abstention decision.

    Fusion scores are rank-normalised, so the top result always scores ~1.0 and
    cannot be thresholded. This combines un-normalised cosine similarity with
    stemmed query-term coverage, both bounded to 0..1.
    """
    query_terms = _content_terms(query)
    if not query_terms:
        return 0.0, 0.0
    coverage = len(query_terms & _content_terms(chunk_text)) / len(query_terms)
    return round(0.5 * max(0.0, raw_cosine) + 0.5 * coverage, 6), round(coverage, 6)


def load_corpus(
    session: Session,
    *,
    corpus: str = "operational_faq",
    category: Optional[str] = None,
    approved_only: bool = True,
) -> List[tuple[FaqChunk, FaqDocument]]:
    stmt = select(FaqChunk, FaqDocument).join(FaqDocument, FaqChunk.document_id == FaqDocument.id)
    if corpus:
        stmt = stmt.where(FaqDocument.corpus == corpus)
    if category:
        stmt = stmt.where(FaqDocument.category == category)
    if approved_only:
        stmt = stmt.where(FaqDocument.status == DocumentStatus.APPROVED.value)
    return list(session.execute(stmt).all())


def search(
    session: Session,
    query: str,
    *,
    corpus: str = "operational_faq",
    category: Optional[str] = None,
    top_k: Optional[int] = None,
    rerank: bool = True,
) -> RetrievalResult:
    """Hybrid BM25 + vector search with fusion, reranking and thresholding."""
    settings = get_settings()
    top_k = top_k or settings.retrieval_top_k
    rows = load_corpus(session, corpus=corpus, category=category)
    result = RetrievalResult(query=query, corpus=corpus)
    if not rows or not query.strip():
        return result

    chunks = [row[0] for row in rows]
    documents = [row[1] for row in rows]

    query_terms = tokenize(query)
    bm25 = BM25Index([tokenize(c.content + " " + c.section) for c in chunks])
    bm25_scores = _normalise(bm25.scores(query_terms))

    embedder = get_embedder()
    query_vector = embedder.embed(query)
    raw_vector_scores: List[float] = []
    for chunk in chunks:
        try:
            embedding = json.loads(chunk.embedding_json) if chunk.embedding_json else []
        except json.JSONDecodeError:
            embedding = []
        raw_vector_scores.append(max(0.0, cosine(query_vector, embedding)))
    vector_scores = _normalise(raw_vector_scores)

    alpha = settings.hybrid_alpha
    candidate_count = min(len(chunks), settings.retrieval_candidates)

    # Reciprocal rank fusion across both rankings, then weighted blend.
    bm25_rank = {i: r for r, i in enumerate(sorted(range(len(chunks)), key=lambda i: -bm25_scores[i]))}
    vec_rank = {i: r for r, i in enumerate(sorted(range(len(chunks)), key=lambda i: -vector_scores[i]))}
    fused: Dict[int, float] = {}
    for i in range(len(chunks)):
        rrf = 1 / (60 + bm25_rank[i] + 1) + 1 / (60 + vec_rank[i] + 1)
        blended = alpha * vector_scores[i] + (1 - alpha) * bm25_scores[i]
        fused[i] = 0.75 * blended + 0.25 * (rrf * 60)

    ordered = sorted(fused, key=lambda i: -fused[i])[:candidate_count]

    retrieved: List[RetrievedChunk] = []
    for i in ordered:
        chunk, document = chunks[i], documents[i]
        relevance, coverage = absolute_relevance(query, chunk.content, raw_vector_scores[i])
        retrieved.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=document.id,
                title=document.title,
                category=document.category,
                corpus=document.corpus,
                version=document.version,
                section=chunk.section,
                page=chunk.page,
                content=chunk.content,
                bm25=bm25_scores[i],
                vector=raw_vector_scores[i],
                score=fused[i],
                relevance=relevance,
                coverage=coverage,
            )
        )

    if rerank:
        retrieved = _lexical_rerank(query_terms, retrieved)

    deduped = _deduplicate(retrieved)[:top_k]
    for rank, chunk in enumerate(deduped, start=1):
        chunk.rank = rank

    result.chunks = deduped
    # Thresholding uses the scale-stable relevance, not the normalised fusion score.
    result.top_score = max((c.relevance for c in deduped), default=0.0)
    result.sufficient = result.top_score >= settings.relevance_threshold and bool(deduped)
    return result


def _lexical_rerank(query_terms: Sequence[str], chunks: List[RetrievedChunk]) -> List[RetrievedChunk]:
    """Cheap deterministic reranker: term coverage + section-title match."""
    unique_terms = set(query_terms)
    if not unique_terms:
        return chunks
    for chunk in chunks:
        content_terms = set(tokenize(chunk.content))
        coverage = len(unique_terms & content_terms) / len(unique_terms)
        section_hit = 0.1 if unique_terms & set(tokenize(chunk.section)) else 0.0
        chunk.score = round(0.8 * chunk.score + 0.2 * coverage + section_hit, 6)
    return sorted(chunks, key=lambda c: -c.score)


def _deduplicate(chunks: Iterable[RetrievedChunk]) -> List[RetrievedChunk]:
    """Drop near-duplicate passages to save tokens."""
    seen: List[set] = []
    unique: List[RetrievedChunk] = []
    for chunk in chunks:
        signature = set(tokenize(chunk.content)[:60])
        if any(signature and len(signature & other) / max(1, len(signature)) > 0.85 for other in seen):
            continue
        seen.append(signature)
        unique.append(chunk)
    return unique

