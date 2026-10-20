import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.errors import HeaderParseError
from email.headerregistry import Address
from pathlib import Path
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import ALLOWED_JWT_ALGORITHMS, settings
from app.consts import AuthRole, SSOScopeType


@dataclass(frozen=True, slots=True)
class SSOTokenPayload:
    """
    Typed, validated payload of an SSO JWT.

    Returned by :func:`decode_sso_token` in place of a raw ``dict`` so call
    sites get autocomplete, type-checked attribute access, and the ``role``
    already bound to the canonical :class:`AuthRole` enum and ``scope_type``
    bound to :class:`SSOScopeType`. ``iat``/``exp`` carry Unix-epoch
    timestamps as decoded by PyJWT.
    """

    scope_type: SSOScopeType
    scope_id: int
    role: AuthRole
    exp: int
    iat: int | None = None
    email: str | None = None
    display_name: str | None = None


# Roles an SSO JWT is allowed to carry. Both are valid ``AuthRole`` members, so
# validating against this set is equivalent to validating against the enum.
SSO_VALID_ROLES: tuple[AuthRole, ...] = (AuthRole.ORGANIZER, AuthRole.SPEAKER)


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
    user_id: int,
    role: str,
    expires_in_hours: int | None = None,
    password_hash: str | None = None,
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
    if password_hash:
        payload["pwh"] = get_password_fingerprint(password_hash)
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def verify_session_token_not_revoked(
    payload: dict,
    current_password_hash: str | None = None,
    user: object | None = None,
    session_revoked_at: datetime | float | None = None,
) -> bool:
    """Verifies that a session token has not been revoked by password change or marker."""
    if not payload or not isinstance(payload, dict):
        return False

    token_pwh = payload.get("pwh")
    if token_pwh:
        if not current_password_hash and user is not None:
            current_password_hash = getattr(user, "hashed_password", None)
        if not current_password_hash:
            return False
        current_pwh = get_password_fingerprint(current_password_hash)
        return bool(current_pwh and hmac.compare_digest(token_pwh, current_pwh))

    # For tokens without pwh: consult persisted revocation state
    if session_revoked_at is None and user is not None:
        session_revoked_at = getattr(user, "session_revoked_at", None)

    if session_revoked_at is None:
        user_id = payload.get("user_id")
        if user_id:
            try:
                from app.db import SessionLocal
                from app.models import User

                with SessionLocal() as db:
                    db_user = db.get(User, user_id)
                    if db_user:
                        session_revoked_at = db_user.session_revoked_at
            except Exception:  # noqa: BLE001, S110
                pass

    # Ensure fingerprint-free tokens are rejected when their revocation status
    # cannot be verified (e.g. marker is missing or unreadable), rather than accepted.
    if session_revoked_at is None:
        return False

    iat = payload.get("iat")
    if iat is None:
        return False

    try:
        if isinstance(iat, datetime):
            token_ts = int(iat.timestamp())
        elif isinstance(iat, (int, float)):
            token_ts = int(iat)
        else:
            return False

        if isinstance(session_revoked_at, datetime):
            revoked_ts = int(session_revoked_at.timestamp())
        elif isinstance(session_revoked_at, (int, float)):
            revoked_ts = int(session_revoked_at)
        else:
            return False

        if token_ts <= revoked_ts:
            return False
    except Exception:  # noqa: BLE001
        return False

    return True


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
    user_id: int,
    email: str,
    role: str,
    expires_in_seconds: int | None = None,
    password_hash: str | None = None,
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
    if password_hash:
        payload["pwh"] = get_password_fingerprint(password_hash)
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


def create_email_verification_token(
    user_id: int,
    email: str,
    expires_in_hours: int = 24,
) -> str:
    """
    Generates a signed JWT for email verification carrying user_id and email.
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email.strip().lower(),
        "type": "email_verification",
        "iat": now,
        "exp": now + timedelta(hours=expires_in_hours),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_email_verification_token(token: str) -> dict | None:
    """
    Decodes and validates an email verification token.
    Returns the decoded payload dict if valid, or None if expired, tampered,
    malformed, missing required claims, or not an email verification token.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token,
            get_session_secret(),
            algorithms=list(ALLOWED_JWT_ALGORITHMS),
            options={"require": ["exp", "iat", "sub"]},
        )
        if payload.get("type") != "email_verification":
            return None
        user_id = payload.get("user_id")
        email = payload.get("email")
        sub = payload.get("sub")
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
            or not isinstance(email, str)
            or not email.strip()
            or not isinstance(sub, str)
            or not sub.strip()
        ):
            return None
        return payload
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None


def get_password_fingerprint(password_hash: str) -> str:
    """Calculates a keyed HMAC-SHA256 fingerprint over the user's password hash."""
    if not password_hash or not isinstance(password_hash, str):
        return ""
    return hmac.new(
        get_session_secret().encode("utf-8"),
        password_hash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_password_reset_token(
    user_id: int,
    email: str,
    password_hash: str,
    expires_in_hours: int | None = None,
) -> str:
    """Generates a signed JWT for password reset carrying user_id, email, and password hash fingerprint."""
    if expires_in_hours is None:
        expires_in_hours = settings.password_reset_expire_hours
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "email": email.strip().lower(),
        "pwh": get_password_fingerprint(password_hash),
        "type": "password_reset",
        "iat": now,
        "exp": now + timedelta(hours=expires_in_hours),
    }
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_password_reset_token(token: str) -> dict | None:
    """Decodes and validates a password reset token.

    Returns the decoded payload dict if valid, or None if expired, tampered,
    malformed, missing required claims, or not a password reset token.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token,
            get_session_secret(),
            algorithms=list(ALLOWED_JWT_ALGORITHMS),
            options={"require": ["exp", "iat", "sub"]},
        )
        if payload.get("type") != "password_reset":
            return None
        user_id = payload.get("user_id")
        email = payload.get("email")
        pwh = payload.get("pwh")
        sub = payload.get("sub")
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or user_id <= 0
            or not isinstance(email, str)
            or not email.strip()
            or not isinstance(pwh, str)
            or not pwh.strip()
            or not isinstance(sub, str)
            or not sub.strip()
        ):
            return None
        return payload
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None


def verify_password_reset_token(payload: dict, current_password_hash: str) -> bool:
    """Verifies that the token's password fingerprint matches the user's current password hash."""
    if not payload or not isinstance(payload, dict):
        return False
    token_pwh = payload.get("pwh")
    if not token_pwh or not isinstance(token_pwh, str):
        return False
    current_pwh = get_password_fingerprint(current_password_hash)
    if not current_pwh:
        return False
    return hmac.compare_digest(token_pwh, current_pwh)


def is_valid_email(email: str) -> bool:
    """Validates an email address against RFC 5322 using the Python standard library."""
    if not email or not isinstance(email, str) or "@" not in email:
        return False
    try:
        clean = email.strip()
        addr = Address(addr_spec=clean)
        return bool(
            addr.username
            and addr.domain
            and not addr.domain.startswith(".")
            and not addr.domain.endswith(".")
        )
    except (HeaderParseError, ValueError, IndexError, TypeError) as _exc:
        return False


def create_sso_token(
    scope_type: SSOScopeType | Literal["event", "talk"],
    scope_id: int,
    role: str,
    expires_in_seconds: int | None = None,
    email: str | None = None,
    display_name: str | None = None,
) -> str:
    """
    Generates a short-lived signed SSO token scoped to an event or talk,
    with no user_id or personal identity claims.
    """
    try:
        scope_type_enum = SSOScopeType(scope_type)
    except ValueError, TypeError:
        raise ValueError("scope_type must be 'event' or 'talk'")
    if not isinstance(scope_id, int) or isinstance(scope_id, bool) or scope_id <= 0:
        raise ValueError("scope_id must be a positive integer")
    if role not in ("organizer", "speaker"):
        raise ValueError("role must be 'organizer' or 'speaker'")
    if (
        scope_type_enum is SSOScopeType.EVENT
        and role == "speaker"
        and (not isinstance(email, str) or not email.strip())
    ):
        raise ValueError("email is required for event-scoped speaker tokens")
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
        "scope_type": scope_type_enum.value,
        "scope_id": scope_id,
        "role": role,
        "type": "sso",
        "iat": now,
        "exp": now + timedelta(seconds=expiry),
    }
    if email and isinstance(email, str) and email.strip():
        payload["email"] = email.strip()
    if display_name and isinstance(display_name, str) and display_name.strip():
        payload["display_name"] = display_name.strip()
    return jwt.encode(payload, get_session_secret(), algorithm=settings.jwt_algorithm)


def decode_sso_token(token: str) -> SSOTokenPayload | None:
    """
    Decodes and validates an SSO token.

    Returns a typed :class:`SSOTokenPayload` on success, or ``None`` if the
    token is expired, tampered, malformed, contains user identity claims, or
    fails the SSO-specific claim allow-list. The ``role`` field is normalized
    to an :class:`AuthRole` enum and ``scope_type`` to a :class:`SSOScopeType`
    enum so call sites can pass them straight into ``CurrentUser(...)`` and
    template context.
    """
    if not token or not isinstance(token, str):
        return None
    try:
        payload = jwt.decode(
            token,
            get_session_secret(),
            algorithms=list(ALLOWED_JWT_ALGORITHMS),
            options={"require": ["exp"]},
        )
        if payload.get("type") != "sso":
            return None
        if "user_id" in payload or "sub" in payload:
            return None
        try:
            scope_type_enum = SSOScopeType(payload.get("scope_type"))
        except ValueError, TypeError:
            return None
        scope_id = payload.get("scope_id")
        if not isinstance(scope_id, int) or isinstance(scope_id, bool) or scope_id <= 0:
            return None
        raw_role = payload.get("role")
        try:
            role = AuthRole(raw_role)
        except ValueError, TypeError:
            return None
        if role not in SSO_VALID_ROLES:
            return None
        if (
            scope_type_enum is SSOScopeType.EVENT
            and role is AuthRole.SPEAKER
            and (
                not isinstance(payload.get("email"), str)
                or not payload["email"].strip()
            )
        ):
            return None

        exp = payload.get("exp")
        iat = payload.get("iat")
        # PyJWT returns numeric-date claims as Python ints; defensive checks
        # keep the dataclass honest if a future JWT lib changes that.
        if (
            not isinstance(exp, int)
            or isinstance(exp, bool)
            or (iat is not None and (not isinstance(iat, int) or isinstance(iat, bool)))
        ):
            return None

        email_raw = payload.get("email")
        display_name_raw = payload.get("display_name")
        email = (
            email_raw.strip()
            if isinstance(email_raw, str) and email_raw.strip()
            else None
        )
        display_name = (
            display_name_raw.strip()
            if isinstance(display_name_raw, str) and display_name_raw.strip()
            else None
        )

        return SSOTokenPayload(
            scope_type=scope_type_enum,
            scope_id=scope_id,
            role=role,
            exp=exp,
            iat=iat if isinstance(iat, int) else None,
            email=email,
            display_name=display_name,
        )
    except (jwt.PyJWTError, TypeError, ValueError, AttributeError) as _exc:
        return None
