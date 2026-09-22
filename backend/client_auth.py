"""Authentication helpers for the public Tienda Canela dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a salted PBKDF2 hash suitable for a private environment variable."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, iterations, salt, expected = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(digest, _b64decode(expected))
    except (TypeError, ValueError):
        return False


def create_session_token(username: str, secret: str, *, ttl_seconds: int) -> str:
    expires_at = int(time.time()) + ttl_seconds
    payload = f"{expires_at}|{username}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64encode(payload)}.{_b64encode(signature)}"


def verify_session_token(token: str, secret: str, username: str) -> bool:
    if not token or not secret or not username:
        return False
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        expires_at, token_username = payload.decode("utf-8").split("|", 1)
        return token_username == username and int(expires_at) >= int(time.time())
    except (TypeError, ValueError, UnicodeDecodeError):
        return False


def auth_config() -> tuple[str, str, str]:
    return (
        os.environ.get("CLIENT_DASHBOARD_USERNAME", "").strip(),
        os.environ.get("CLIENT_DASHBOARD_PASSWORD_HASH", "").strip(),
        os.environ.get("CLIENT_DASHBOARD_SESSION_SECRET", "").strip(),
    )


def auth_is_configured() -> bool:
    username, password_hash, session_secret = auth_config()
    return bool(username and password_hash and len(session_secret) >= 32)
