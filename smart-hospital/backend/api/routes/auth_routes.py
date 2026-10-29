"""Auth routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.api.deps import current_user, db_session
from backend.api.schemas import LoginRequest, TokenResponse
from backend.auth.rbac import CurrentUser, allowed_permissions
from backend.services import auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, session: Session = Depends(db_session)) -> TokenResponse:
    user, token = auth_service.authenticate(session, payload.username, payload.password)
    me = auth_service.current_user_from_token(token)
    return TokenResponse(
        access_token=token,
        username=user.username,
        role=user.role,
        permissions=list(allowed_permissions(me)),
    )


@router.get("/me", response_model=TokenResponse)
def me(user: CurrentUser = Depends(current_user)) -> TokenResponse:
    return TokenResponse(
        access_token="",
        username=user.username,
        role=user.role.value,
        permissions=list(allowed_permissions(user)),
    )

