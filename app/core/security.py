from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer
from passlib.context import CryptContext

from app.core.settings import RuntimeSettings, get_settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@dataclass(slots=True)
class SessionPayload:
    admin_user_id: str
    csrf_token: str


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def build_serializer(settings: RuntimeSettings | None = None) -> URLSafeTimedSerializer:
    actual = settings or get_settings()
    return URLSafeTimedSerializer(actual.app.secret_key, salt="admin-session")


def create_session_token(admin_user_id: str, settings: RuntimeSettings | None = None) -> str:
    csrf_token = generate_csrf_token()
    payload = {
        "admin_user_id": admin_user_id,
        "csrf_token": csrf_token,
        "issued_at": int(time.time()),
    }
    return build_serializer(settings).dumps(payload)


def decode_session_token(token: str, settings: RuntimeSettings | None = None) -> SessionPayload | None:
    actual = settings or get_settings()
    try:
        data = build_serializer(actual).loads(token, max_age=actual.admin.session_max_age_seconds)
    except BadSignature:
        return None
    return SessionPayload(
        admin_user_id=str(data["admin_user_id"]),
        csrf_token=str(data["csrf_token"]),
    )


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def validate_csrf(expected: str, provided: str | None) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(expected, provided)


def redact_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = {}
    for key, value in payload.items():
        lowered = key.lower()
        if any(token in lowered for token in ["token", "secret", "password", "authorization"]):
            redacted[key] = "***redacted***"
        else:
            redacted[key] = value
    return redacted
