"""FastAPI dependencies: DB session, current user, rate limiting."""
from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from backend.auth.rbac import CurrentUser
from backend.core.errors import AuthenticationError
from backend.db.session import get_session
from backend.guardrails.policy import RATE_LIMITER
from backend.services import auth_service


def db_session() -> Iterator[Session]:
    yield from get_session()


def current_user(authorization: str = Header(default="")) -> CurrentUser:
    if not authorization.lower().startswith("bearer "):
        raise AuthenticationError("Missing bearer token.")
    return auth_service.current_user_from_token(authorization.split(" ", 1)[1].strip())


def rate_limited(request: Request, user: CurrentUser = Depends(current_user)) -> CurrentUser:
    RATE_LIMITER.check(f"{user.username}:{request.url.path}")
    return user

