import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import ALLOWED_JWT_ALGORITHMS, settings

_hasher = PasswordHasher()
_session_secret: str | None = None


def get_session_secret() -> str:
    """
    Returns the session and JWT signing secret.

    If SESSION_SECRET is provided in configuration/environment, it is used.
    If unset in a production environment, a RuntimeError is raised.
    In development or test environments, a secret is generated and persisted locally
    in `<DATA_DIR>/.session_secret` so sessions survive dev server restarts.
    """
    global _session_secret
    if settings.session_secret:
        if len(settings.session_secret.encode("utf-8")) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 bytes long")
        return settings.session_secret

    if settings.environment.lower() in ("production", "prod"):
        raise RuntimeError(
            "SESSION_SECRET must be explicitly set in production environment"
        )

    if _session_secret:
        return _session_secret

    secret_file = Path(settings.data_dir) / ".session_secret"
    if secret_file.exists():
        # storage-boundary-exempt: read local session secret for dev reload
        secret = secret_file.read_text(encoding="utf-8").strip()
        if secret:
            _session_secret = secret
            return _session_secret

    # storage-boundary-exempt: create data directory if needed for dev secret
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_hex(32)
    try:
        # storage-boundary-exempt: exclusive create with 0600 mode to avoid exposure race
        fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # storage-boundary-exempt: write secret through open file descriptor
        with open(fd, "w", encoding="utf-8") as f:
            f.write(generated)
        _session_secret = generated
    except FileExistsError:
        # storage-boundary-exempt: read winning secret created concurrently
        _session_secret = secret_file.read_text(encoding="utf-8").strip()
    return _session_secret


def hash_password(plain: str) -> str:
    """Hashes a plaintext password using Argon2id."""
    if not isinstance(plain, str):
        raise TypeError("Password must be a string")
    if not plain:
        raise ValueError("Password cannot be empty")
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """
    Verifies a plaintext password against an Argon2 hash.
    Returns True if valid, False otherwise without raising exceptions.
    """
    if (
        not plain
        or not hashed
        or not isinstance(plain, str)
        or not isinstance(hashed, str)
    ):
        return False
    try:
        return bool(_hasher.verify(hashed, plain))
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
        TypeError,
    ) as _exc:
        return False


def create_session_token(
    user_id: int, role: str, expires_in_hours: int | None = None
) -> str:
    """
    Generates a stateless signed session token carrying user_id and role,
    suitable for HTTP-only cookies.
    """
    now = datetime.now(UTC)
    expiry = (
        expires_in_hours
        if expires_in_hours is not None
        else settings.session_token_expire_hours
    )
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "role": role,
        "type": "session",
        "iat": now,
        "exp": now + timedelta(hours=expiry),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_session_token(token: str) -> dict | None:
    """
    Decodes and validates a session token.
    Returns the decoded payload dict if valid, or None if expired, tampered,
    malformed, missing required claims, or not a session token.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token, get_session_secret(), algorithms=list(ALLOWED_JWT_ALGORITHMS)
        )
        if payload.get("type") != "session":
            return None
        if (
            not isinstance(payload.get("user_id"), int)
            or not isinstance(payload.get("role"), str)
            or not isinstance(payload.get("sub"), str)
        ):
            return None
        return payload
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None


def create_access_token(
    user_id: int, email: str, role: str, expires_in_seconds: int | None = None
) -> str:
    """
    Generates a signed JWT access token carrying user_id, email, and role,
    suitable for Bearer header authentication.
    """
    now = datetime.now(UTC)
    expiry = (
        expires_in_seconds
        if expires_in_seconds is not None
        else settings.access_token_expire_seconds
    )
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(seconds=expiry),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict | None:
    """
    Decodes and validates an access token.
    Returns the decoded payload dict if valid, or None if expired, tampered,
    malformed, missing required claims, or not an access token.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token, get_session_secret(), algorithms=list(ALLOWED_JWT_ALGORITHMS)
        )
        if payload.get("type") != "access":
            return None
        if (
            not isinstance(payload.get("user_id"), int)
            or not isinstance(payload.get("email"), str)
            or not isinstance(payload.get("role"), str)
            or not isinstance(payload.get("sub"), str)
        ):
            return None
        return payload
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None


def create_sso_token(
    scope_type: Literal["event", "talk"],
    scope_id: int,
    role: str,
    expires_in_seconds: int | None = None,
) -> str:
    """
    Generates a short-lived signed SSO token scoped to an event or talk,
    with no user_id or personal identity claims.
    """
    if scope_type not in ("event", "talk"):
        raise ValueError("scope_type must be 'event' or 'talk'")
    if not isinstance(scope_id, int) or isinstance(scope_id, bool) or scope_id <= 0:
        raise ValueError("scope_id must be a positive integer")
    if role not in ("organizer", "speaker"):
        raise ValueError("role must be 'organizer' or 'speaker'")
    if expires_in_seconds is not None and (
        not isinstance(expires_in_seconds, int)
        or isinstance(expires_in_seconds, bool)
        or expires_in_seconds <= 0
    ):
        raise ValueError("expires_in_seconds must be a positive integer")

    now = datetime.now(UTC)
    expiry = (
        expires_in_seconds
        if expires_in_seconds is not None
        else settings.sso_token_expire_seconds
    )
    payload = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "role": role,
        "type": "sso",
        "iat": now,
        "exp": now + timedelta(seconds=expiry),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_sso_token(token: str) -> dict | None:
    """
    Decodes and validates an SSO token.
    Returns the decoded payload dict if valid, or None if expired, tampered,
    malformed, containing user_id, or missing required claims.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token, get_session_secret(), algorithms=list(ALLOWED_JWT_ALGORITHMS)
        )
        if payload.get("type") != "sso":
            return None
        if "user_id" in payload or "sub" in payload or "email" in payload:
            return None
        if payload.get("scope_type") not in ("event", "talk"):
            return None
        if not isinstance(payload.get("scope_id"), int):
            return None
        if payload.get("role") not in ("organizer", "speaker"):
            return None
        return payload
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None
