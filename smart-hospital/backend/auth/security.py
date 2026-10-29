"""Password hashing and stateless signed tokens (stdlib only)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict

from backend.core.config import get_settings
from backend.core.errors import AuthenticationError

_ITERATIONS = 120_000


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, AttributeError):
        return False


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def create_token(username: str, role: str, patient_id: int | None = None) -> str:
    settings = get_settings()
    payload = {
        "sub": username,
        "role": role,
        "pid": patient_id,
        "exp": int(time.time()) + settings.token_ttl_seconds,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signature = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(signature)}"


def decode_token(token: str) -> Dict[str, Any]:
    settings = get_settings()
    try:
        body, signature = token.split(".")
        expected = hmac.new(settings.secret_key.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64e(expected), signature):
            raise AuthenticationError("Invalid session token.")
        payload = json.loads(_b64d(body))
    except AuthenticationError:
        raise
    except Exception as exc:  # malformed token
        raise AuthenticationError("Invalid session token.") from exc

    if payload.get("exp", 0) < time.time():
        raise AuthenticationError("Session expired, please sign in again.")
    return payload

