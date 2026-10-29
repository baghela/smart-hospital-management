"""User authentication service."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.audit import logger as audit
from backend.auth.rbac import CurrentUser
from backend.auth.security import create_token, decode_token, hash_password, verify_password
from backend.core.errors import AuthenticationError, ValidationError
from backend.models import Role, User


def create_user(
    session: Session,
    *,
    username: str,
    full_name: str,
    role: Role,
    password: str,
    patient_id: int | None = None,
) -> User:
    if session.scalar(select(User).where(User.username == username)):
        raise ValidationError(f"Username '{username}' already exists.")
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    user = User(
        username=username,
        full_name=full_name,
        role=role.value,
        password_hash=hash_password(password),
        patient_id=patient_id,
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session: Session, username: str, password: str) -> tuple[User, str]:
    user = session.scalar(select(User).where(User.username == username))
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        audit.record(
            session,
            actor=username or "unknown",
            actor_role="unknown",
            action="login",
            resource="user",
            outcome="failure",
        )
        session.commit()
        raise AuthenticationError("Invalid username or password.")
    token = create_token(user.username, user.role, user.patient_id)
    audit.record(
        session,
        actor=user.username,
        actor_role=user.role,
        action="login",
        resource="user",
        resource_id=user.id,
    )
    session.commit()
    return user, token


def current_user_from_token(token: str) -> CurrentUser:
    payload = decode_token(token)
    try:
        role = Role(payload["role"])
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Token carries an unknown role.") from exc
    return CurrentUser(username=payload["sub"], role=role, patient_id=payload.get("pid"))


def list_users(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.username)))

