# Smart Hospital Management System
# Single image running either the FastAPI backend or the Streamlit UI.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System packages: build tooling for psycopg and PyMuPDF wheels, plus curl for
# container healthchecks.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY backend ./backend
COPY rag ./rag
COPY streamlit_app ./streamlit_app
COPY evaluation ./evaluation
COPY data ./data
COPY alembic ./alembic
COPY tests ./tests
COPY alembic.ini pyproject.toml ./
COPY .streamlit ./.streamlit
COPY hospital-copilot-workflow-export.json hospital-copilot-workflow.yaml ./

# Run as a non-root user.
RUN useradd --create-home --shell /bin/bash hospital \
    && chown -R hospital:hospital /app
USER hospital

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]

