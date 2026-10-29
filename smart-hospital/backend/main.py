"""FastAPI application: routers, error handling, tracing and health checks."""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.api.routes import (
    admin_routes,
    admission_routes,
    appointment_routes,
    approval_routes,
    auth_routes,
    billing_routes,
    clinical_routes,
    copilot_routes,
    patient_routes,
)
from backend.core.config import get_settings
from backend.core.errors import HospitalError
from backend.core.logging import configure_logging, get_logger
from backend.db.session import engine

configure_logging()
logger = get_logger("api")
settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "Smart Hospital Management System API with an evidence-grounded FAQ "
        "copilot. Mutating high-impact actions require human approval."
    ),
)

for router in (
    auth_routes.router,
    patient_routes.router,
    appointment_routes.router,
    admission_routes.router,
    billing_routes.router,
    clinical_routes.router,
    approval_routes.router,
    copilot_routes.router,
    admin_routes.router,
):
    app.include_router(router, prefix="/api")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:12]
    request.state.trace_id = trace_id
    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.exception_handler(HospitalError)
async def hospital_error_handler(request: Request, exc: HospitalError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.message,
            "code": exc.code,
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Pydantic error payloads can carry exception objects in ``ctx``; keep only
    # JSON-serialisable fields so the response never fails to render.
    details = [
        {
            "field": ".".join(str(part) for part in error.get("loc", [])),
            "message": str(error.get("msg", "invalid value")),
            "type": str(error.get("type", "value_error")),
        }
        for error in exc.errors()[:5]
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": "Request validation failed.",
            "code": "validation_error",
            "details": details,
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    # Never leak stack traces or secrets to the caller.
    logger.exception("unhandled_error", extra={"trace_id": getattr(request.state, "trace_id", None)})
    return JSONResponse(
        status_code=500,
        content={
            "error": "An internal error occurred.",
            "code": "internal_error",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "app": settings.app_name, "workflow_id": settings.workflow_id}


@app.get("/health/db", tags=["health"])
def health_db():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"database": "ok", "dialect": engine.dialect.name}
    except Exception:
        return JSONResponse(status_code=503, content={"database": "unavailable"})

