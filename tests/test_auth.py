from __future__ import annotations

from app.core.security import create_session_token, decode_session_token, hash_password, verify_password


def test_password_hash_and_verify():
    hashed = hash_password("super-secure-password")
    assert verify_password("super-secure-password", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_session_token_roundtrip(settings):
    token = create_session_token("00000000-0000-0000-0000-000000000001", settings)
    payload = decode_session_token(token, settings)
    assert payload is not None
    assert payload.admin_user_id == "00000000-0000-0000-0000-000000000001"
