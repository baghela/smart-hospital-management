"""Document ingestion and index management (authorised staff only)."""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from backend.core.config import get_settings
from backend.models import DocumentStatus
from rag import ingest, retriever
from streamlit_app.common import db, empty_state, page_header, require_login, run_action, status_badge

st.set_page_config(page_title="Documents", page_icon="📄", layout="wide")
user = require_login("faq:ingest")
settings = get_settings()

page_header(
    "📄 Knowledge documents",
    "Upload approved hospital documents. Uploads are validated, sanitised against "
    "prompt injection, chunked with page/section metadata, then embedded.",
)

tab_upload, tab_manage, tab_test = st.tabs(["Upload", "Manage index", "Test retrieval"])

with tab_upload:
    with st.form("upload_doc"):
        uploaded = st.file_uploader(
            f"Document ({', '.join(settings.allowed_upload_ext)}, max {settings.max_upload_mb} MB)",
            type=[e.lstrip(".") for e in settings.allowed_upload_ext],
        )
        c1, c2 = st.columns(2)
        title = c1.text_input("Title *", placeholder="Hospital Visiting Hours")
        version = c2.text_input("Version", value="1.0")
        category = c1.selectbox(
            "Category", ["operational", "appointments", "billing", "insurance", "laboratory", "visiting"]
        )
        corpus = c2.selectbox(
            "Corpus",
            list(ingest.CORPORA),
            help="Operational FAQ is kept separate from insurance policy and clinical information.",
        )
        effective = st.date_input("Effective date", value=date.today())
        confirmed = st.checkbox("I confirm this document is approved for publication *")
        submitted = st.form_submit_button("Ingest & index", type="primary")

    if submitted:
        if not uploaded:
            st.error("Select a file to upload.")
        elif not confirmed:
            st.error("Approval confirmation is required before indexing.")
        else:
            with st.spinner("Extracting, sanitising, chunking and embedding..."):
                with db() as session:
                    document = run_action(
                        ingest.ingest_document,
                        session,
                        user,
                        filename=uploaded.name,
                        data=uploaded.getvalue(),
                        title=title,
                        category=category,
                        corpus=corpus,
                        version=version,
                        effective_date=effective,
                        mime_type=uploaded.type,
                        success="Document indexed.",
                    )
            if document:
                st.info(f"Indexed **{document.title}** v{document.version} from `{document.source_filename}`")

with tab_manage:
    with db() as session:
        documents = ingest.list_documents(session)
        rows = [
            {
                "id": d.id,
                "Title": d.title,
                "Version": d.version,
                "Corpus": d.corpus,
                "Category": d.category,
                "Status": d.status,
                "Effective": d.effective_date,
                "Chunks": len(d.chunks),
                "Uploaded by": d.uploaded_by,
            }
            for d in documents
        ]

    if not rows:
        empty_state("No documents indexed yet.", "Upload one from the Upload tab or run the seeder.")
    else:
        st.dataframe(pd.DataFrame(rows).drop(columns=["id"]), use_container_width=True, hide_index=True)
        for row in rows:
            with st.expander(f"{row['Title']} v{row['Version']} · {row['Chunks']} chunks"):
                st.markdown(f"Status {status_badge(row['Status'])} · Corpus `{row['Corpus']}`")
                c1, c2, c3 = st.columns(3)
                if c1.button("Re-index", key=f"ri_{row['id']}"):
                    with db() as session:
                        run_action(ingest.reindex_document, session, user, row["id"], success="Re-indexed.")
                    st.rerun()
                new_status = c2.selectbox(
                    "Status", [s.value for s in DocumentStatus], index=[s.value for s in DocumentStatus].index(row["Status"]),
                    key=f"ss_{row['id']}",
                )
                if c2.button("Apply status", key=f"sa_{row['id']}"):
                    with db() as session:
                        run_action(
                            ingest.set_status, session, user, row["id"], DocumentStatus(new_status),
                            success="Status updated.",
                        )
                    st.rerun()
                if user.has("faq:delete"):
                    with c3.popover("Delete"):
                        st.caption("This removes the document and all of its chunks.")
                        if st.button("Confirm delete", key=f"dd_{row['id']}"):
                            with db() as session:
                                run_action(ingest.delete_document, session, user, row["id"], success="Deleted.")
                            st.rerun()

with tab_test:
    st.caption("Run the hybrid retriever directly to inspect scoring before users ask.")
    query = st.text_input("Test query", placeholder="What are the ICU visiting hours?")
    corpus_choice = st.selectbox("Corpus", list(ingest.CORPORA), key="test_corpus")
    if st.button("Run retrieval") and query:
        with db() as session:
            result = retriever.search(session, query, corpus=corpus_choice)
        st.markdown(
            f"Top score **{result.top_score:.3f}** · threshold `{settings.relevance_threshold}` · "
            f"sufficient: **{result.sufficient}**"
        )
        if not result.chunks:
            empty_state("No passages retrieved for this query.")
        else:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Rank": c.rank,
                            "Title": c.title,
                            "Section": c.section,
                            "Page": c.page,
                            "BM25": round(c.bm25, 3),
                            "Vector": round(c.vector, 3),
                            "Score": round(c.score, 3),
                        }
                        for c in result.chunks
                    ]
                ),
                use_container_width=True,
                hide_index=True,
            )

