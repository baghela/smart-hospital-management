"""Retrieval relevance, citation validation and escalation routing tests."""
from __future__ import annotations

import pytest

from rag import escalation_matrix, retriever
from rag.chunking import chunk_pages, detect_section, extract_pages
from rag.citations import extract_markers, strip_unsupported, validate_answer
from rag.embeddings import cosine, get_embedder


def test_chunking_preserves_pages_and_sections():
    text = "VISITING HOURS\nGeneral ward visiting is 11:00 to 13:00 every single day of the week.\n"
    pages = extract_pages("policy.txt", text.encode())
    chunks = chunk_pages(pages)
    assert chunks
    assert chunks[0].page == 1
    assert all(c.token_estimate > 0 for c in chunks)


def test_section_detection():
    assert detect_section("VISITING HOURS", "General") == "VISITING HOURS"
    assert detect_section("general ward visiting is 11:00", "VISITING HOURS") == "VISITING HOURS"


def test_embeddings_are_normalised_and_deterministic():
    embedder = get_embedder()
    first = embedder.embed("ICU visiting hours")
    second = embedder.embed("ICU visiting hours")
    assert first == second
    assert cosine(first, second) == pytest.approx(1.0, abs=1e-6)
    assert cosine(first, embedder.embed("pharmacy stock levels")) < 0.99


def test_stable_hash_is_process_independent():
    """An index written in one process must be queryable from another.

    Python's built-in ``hash()`` is randomised per interpreter, so the embedder
    must not use it. This pins the expected digest values.
    """
    from rag.embeddings import stable_hash

    assert stable_hash("visiting:512") == stable_hash("visiting:512")
    # Known-good values computed with blake2b; a change here breaks stored vectors.
    assert stable_hash("icu") == 11388939469703629480
    assert stable_hash("visiting") == 12904460172813377188


def test_embedding_vector_is_pinned():
    """Guards against silently changing the embedding function."""
    from rag.embeddings import HashingEmbedder

    vector = HashingEmbedder(dim=8).embed("icu visiting hours")
    assert len(vector) == 8
    assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-9)
    assert [round(v, 6) for v in vector] == [
        round(v, 6) for v in HashingEmbedder(dim=8).embed("icu visiting hours")
    ]


def test_escalation_reference_is_process_independent():
    first = escalation_matrix.ticket_reference("billing", "How do I pay my bill?")
    second = escalation_matrix.ticket_reference("billing", "  how do I PAY my bill?  ")
    assert first == second
    assert first.startswith("ESC-BIL-")


def test_hybrid_retrieval_finds_relevant_passage(session, faq_corpus):
    result = retriever.search(session, "What are the ICU visiting hours?")
    assert result.chunks
    assert result.sufficient
    assert any("ICU" in c.content for c in result.chunks)
    top = result.chunks[0]
    assert top.bm25 >= 0 and top.vector >= 0
    assert "p." in top.citation()


def test_retrieval_recall_on_known_questions(session, faq_corpus):
    cases = {
        "visiting hours for general ward": "visiting",
        "how do I reschedule my appointment": "reschedul",
        "how long is a consultation slot": "30 minutes",
    }
    hits = 0
    for question, expected in cases.items():
        result = retriever.search(session, question)
        if any(expected.lower() in c.content.lower() for c in result.chunks):
            hits += 1
    assert hits / len(cases) >= 0.66


def test_unknown_question_scores_below_known_question(session, faq_corpus):
    known = retriever.search(session, "ICU visiting hours")
    unknown = retriever.search(session, "quantum chromodynamics lattice gauge theory")
    assert known.top_score > unknown.top_score


def test_empty_index_returns_no_evidence(session):
    result = retriever.search(session, "anything at all")
    assert not result.chunks and not result.sufficient


def _chunk(content: str, index: int = 1):
    return retriever.RetrievedChunk(
        chunk_id=index, document_id=1, title="Visiting Policy", category="visiting",
        corpus="operational_faq", version="1.0", section="ICU", page=2, content=content,
    )


def test_valid_citation_passes_validation():
    chunks = [_chunk("ICU visiting is limited to 17:00 to 18:00 daily and one attendant per patient.")]
    report = validate_answer("ICU visiting is limited to 17:00 to 18:00 daily [S1].", chunks)
    assert report.valid
    assert report.cited_indices == [1]


def test_uncited_answer_is_rejected():
    chunks = [_chunk("ICU visiting is limited to 17:00 to 18:00 daily.")]
    report = validate_answer("ICU visiting is open all day and night for everyone.", chunks)
    assert not report.valid
    assert report.reason == "no_citations"


def test_invalid_marker_is_rejected():
    chunks = [_chunk("ICU visiting is limited to 17:00 to 18:00 daily.")]
    report = validate_answer("ICU visiting is limited to 17:00 to 18:00 [S7].", chunks)
    assert not report.valid
    assert report.invalid_markers == [7]


def test_unsupported_claim_is_detected_and_strippable():
    chunks = [_chunk("ICU visiting is limited to 17:00 to 18:00 daily.")]
    answer = "ICU visiting is limited to 17:00 to 18:00 daily [S1]. Parking is free for every visitor [S1]."
    report = validate_answer(answer, chunks)
    assert not report.valid
    assert report.unsupported_sentences
    repaired = strip_unsupported(answer, report)
    assert "Parking" not in repaired


def test_markers_are_extracted():
    assert extract_markers("facts [S1] and more [S3]") == [1, 3]


def test_validation_without_evidence_fails():
    assert validate_answer("Anything [S1].", []).reason == "no_evidence"


@pytest.mark.parametrize(
    "question,expected_topic",
    [
        ("How do I pay my hospital bill?", "billing"),
        ("What documents are needed for a cashless claim?", "insurance"),
        ("How do I book an appointment?", "appointment"),
        ("Do I need to fast before the blood test?", "laboratory"),
        ("When can I collect medicines?", "pharmacy"),
    ],
)
def test_escalation_topic_routing(question, expected_topic):
    assert escalation_matrix.classify_topic(question) == expected_topic


def test_escalation_contact_resolution_and_reference():
    contact = escalation_matrix.resolve("How do I claim insurance?", role="patient")
    assert contact.topic == "insurance"
    assert "@" in contact.email and contact.contact
    reference = escalation_matrix.ticket_reference(contact.topic, "How do I claim insurance?")
    assert reference.startswith("ESC-INS-")


def test_emergency_contact_is_available_24x7():
    contact = escalation_matrix.resolve("chest pain", emergency=True)
    assert contact.topic == "emergency"
    assert "24" in contact.hours


def test_role_fallback_routing():
    contact = escalation_matrix.resolve("something unclassifiable xyzzy", role="pharmacist")
    assert contact.topic in ("pharmacy", "default")


def test_matrix_lists_all_desks():
    rows = escalation_matrix.all_contacts()
    topics = {row["topic"] for row in rows}
    assert {"default", "emergency", "billing", "insurance"} <= topics

