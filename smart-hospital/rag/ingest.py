"""Document ingestion: validate, extract, sanitize, chunk, embed and index."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.core.errors import NotFoundError, ValidationError
from backend.guardrails.policy import screen_document_text, validate_upload
from backend.models import DocumentStatus, FaqChunk, FaqDocument
from rag.chunking import extract_and_chunk
from rag.embeddings import get_embedder

# Separate corpora keep operational FAQ answers away from clinical / policy text.
CORPORA = ("operational_faq", "insurance_policy", "clinical_information")


def ingest_document(
    session: Session,
    user: CurrentUser,
    *,
    filename: str,
    data: bytes,
    title: str,
    category: str = "operational",
    corpus: str = "operational_faq",
    version: str = "1.0",
    effective_date: date | None = None,
    mime_type: str | None = None,
    approved: bool = True,
) -> FaqDocument:
    """Ingest an approved hospital document into the retrieval index."""
    user.require("faq:ingest")
    validate_upload(filename, len(data or b""), mime_type)
    if corpus not in CORPORA:
        raise ValidationError(f"Unknown corpus '{corpus}'. Allowed: {', '.join(CORPORA)}")
    if not title.strip():
        raise ValidationError("Document title is required.")

    content_hash = hashlib.sha256(data).hexdigest()
    duplicate = session.scalar(select(FaqDocument).where(FaqDocument.content_hash == content_hash))
    if duplicate:
        raise ValidationError(
            f"This file is already indexed as '{duplicate.title}' v{duplicate.version}."
        )
    if session.scalar(
        select(FaqDocument).where(FaqDocument.title == title.strip(), FaqDocument.version == version)
    ):
        raise ValidationError(f"'{title}' version {version} already exists - bump the version.")

    _pages, chunks = extract_and_chunk(filename, data)
    if not chunks:
        raise ValidationError("No usable text could be extracted from the document.")

    document = FaqDocument(
        title=title.strip(),
        category=category.strip().lower() or "operational",
        corpus=corpus,
        version=version,
        status=DocumentStatus.APPROVED.value if approved else DocumentStatus.PENDING.value,
        effective_date=effective_date or date.today(),
        source_filename=filename,
        content_hash=content_hash,
        uploaded_by=user.username,
    )
    session.add(document)
    session.flush()

    embedder = get_embedder()
    sanitized: List[str] = []
    flags: List[str] = []
    for chunk in chunks:
        clean, chunk_flags = screen_document_text(chunk.content)
        sanitized.append(clean)
        flags.extend(chunk_flags)
    vectors = embedder.embed_many(sanitized)

    for chunk, clean, vector in zip(chunks, sanitized, vectors):
        session.add(
            FaqChunk(
                document_id=document.id,
                chunk_index=chunk.index,
                section=chunk.section[:160],
                page=chunk.page,
                content=clean,
                token_estimate=chunk.token_estimate,
                embedding_json=json.dumps(vector),
            )
        )

    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="faq_ingest",
        resource="faq_document",
        resource_id=document.id,
        detail=f"chunks={len(chunks)};sanitized={len(set(flags))}",
    )
    session.commit()
    return document


def reindex_document(session: Session, user: CurrentUser, document_id: int) -> FaqDocument:
    """Recompute embeddings for an existing document (e.g. after model change)."""
    user.require("faq:ingest")
    document = get_document(session, document_id)
    embedder = get_embedder()
    chunks = list(session.scalars(select(FaqChunk).where(FaqChunk.document_id == document.id)))
    vectors = embedder.embed_many([c.content for c in chunks])
    for chunk, vector in zip(chunks, vectors):
        chunk.embedding_json = json.dumps(vector)
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="faq_reindex",
        resource="faq_document",
        resource_id=document.id,
        detail=f"chunks={len(chunks)}",
    )
    session.commit()
    return document


def delete_document(session: Session, user: CurrentUser, document_id: int) -> None:
    user.require("faq:delete")
    document = get_document(session, document_id)
    session.delete(document)  # chunks cascade
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="faq_delete",
        resource="faq_document",
        resource_id=document_id,
    )
    session.commit()


def set_status(session: Session, user: CurrentUser, document_id: int, status: DocumentStatus) -> FaqDocument:
    user.require("faq:ingest")
    document = get_document(session, document_id)
    document.status = status.value
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role.value,
        action="faq_status",
        resource="faq_document",
        resource_id=document_id,
        detail=status.value,
    )
    session.commit()
    return document


def get_document(session: Session, document_id: int) -> FaqDocument:
    document = session.get(FaqDocument, document_id)
    if not document:
        raise NotFoundError(f"Document {document_id} not found.")
    return document


def list_documents(session: Session, *, corpus: str | None = None) -> List[FaqDocument]:
    stmt = select(FaqDocument)
    if corpus:
        stmt = stmt.where(FaqDocument.corpus == corpus)
    return list(session.scalars(stmt.order_by(FaqDocument.id.desc())))


def ingest_text(
    session: Session,
    user: CurrentUser,
    *,
    title: str,
    text: str,
    category: str = "operational",
    corpus: str = "operational_faq",
    version: str = "1.0",
) -> FaqDocument:
    """Convenience helper used by the seeder and tests."""
    return ingest_document(
        session,
        user,
        filename=f"{title.lower().replace(' ', '_')}.txt",
        data=text.encode("utf-8"),
        title=title,
        category=category,
        corpus=corpus,
        version=version,
        mime_type="text/plain",
    )

