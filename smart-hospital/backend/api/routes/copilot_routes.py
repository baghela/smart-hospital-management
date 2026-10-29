"""Copilot / RAG routes: ask, stream, feedback, document management."""
from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import AskRequest, AskResponse, DocumentOut, FeedbackRequest
from backend.auth.rbac import CurrentUser
from backend.copilot import orchestrator
from backend.models import DocumentStatus
from rag import escalation_matrix, ingest

router = APIRouter(tags=["copilot"])


@router.post("/copilot/ask", response_model=AskResponse)
def ask(
    payload: AskRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    answer = orchestrator.ask(
        session,
        user,
        payload.question,
        session_id=payload.session_id,
        history=payload.history,
        provider_order=payload.provider_order,
    )
    return AskResponse(**answer.to_dict())


@router.post("/copilot/ask/stream")
def ask_stream(
    payload: AskRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    """Streams the answer only after citation validation has passed."""
    answer = orchestrator.ask(
        session,
        user,
        payload.question,
        session_id=payload.session_id,
        history=payload.history,
        provider_order=payload.provider_order,
    )
    return StreamingResponse(orchestrator.stream_answer(answer), media_type="text/plain")


@router.get("/copilot/tools")
def tools(session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)):
    return orchestrator.tool_catalog(session, user)


@router.post("/copilot/feedback", status_code=201)
def feedback(
    payload: FeedbackRequest,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    entry = orchestrator.record_feedback(
        session,
        user,
        question=payload.question,
        answer=payload.answer,
        helpful=payload.helpful,
        notes=payload.notes,
    )
    return {"id": entry.id, "recorded": True}


@router.get("/copilot/escalation-matrix")
def escalation(_user: CurrentUser = Depends(current_user)):
    return escalation_matrix.all_contacts()


# --- documents ------------------------------------------------------------
@router.post("/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    category: str = Form("operational"),
    corpus: str = Form("operational_faq"),
    version: str = Form("1.0"),
    effective: date | None = Form(None),
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    data = await file.read()
    return ingest.ingest_document(
        session,
        user,
        filename=file.filename or "upload.txt",
        data=data,
        title=title,
        category=category,
        corpus=corpus,
        version=version,
        effective_date=effective,
        mime_type=file.content_type,
    )


@router.get("/documents", response_model=List[DocumentOut])
def list_documents(
    corpus: str | None = None,
    session: Session = Depends(db_session),
    _user: CurrentUser = Depends(current_user),
):
    return ingest.list_documents(session, corpus=corpus)


@router.post("/documents/{document_id}/reindex", response_model=DocumentOut)
def reindex(
    document_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    return ingest.reindex_document(session, user, document_id)


@router.post("/documents/{document_id}/status", response_model=DocumentOut)
def set_status(
    document_id: int,
    status: str,
    session: Session = Depends(db_session),
    user: CurrentUser = Depends(current_user),
):
    return ingest.set_status(session, user, document_id, DocumentStatus(status))


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(
    document_id: int, session: Session = Depends(db_session), user: CurrentUser = Depends(current_user)
):
    ingest.delete_document(session, user, document_id)

