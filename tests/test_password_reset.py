"""Tests for forgot password and password reset flow in VEditor."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import models
from app.config import settings
from app.db import SessionLocal
from app.email import send_password_reset_email
from app.main import app
from app.security import (
    create_access_token,
    create_password_reset_token,
    create_session_token,
    decode_access_token,
    decode_password_reset_token,
    decode_session_token,
    hash_password,
    verify_password,
    verify_password_reset_token,
    verify_session_token_not_revoked,
)
from app.tasks import job_send_password_reset_email

# ── Token Tests ─────────────────────────────────────────────────────────────


def test_password_reset_token_lifecycle() -> None:
    pwh = hash_password("OldPassword123!")
    token = create_password_reset_token(
        user_id=42,
        email="user@example.com",
        password_hash=pwh,
        expires_in_hours=1,
    )
    assert isinstance(token, str)

    payload = decode_password_reset_token(token)
    assert payload is not None
    assert payload["user_id"] == 42
    assert payload["email"] == "user@example.com"
    assert payload["type"] == "password_reset"
    assert "pwh" in payload

    assert verify_password_reset_token(payload, pwh) is True


def test_password_reset_token_invalidates_on_password_change() -> None:
    pwh_old = hash_password("OldPassword123!")
    token = create_password_reset_token(
        user_id=42,
        email="user@example.com",
        password_hash=pwh_old,
    )
    payload = decode_password_reset_token(token)
    assert payload is not None
    assert verify_password_reset_token(payload, pwh_old) is True

    # Password is changed
    pwh_new = hash_password("NewPassword123!")
    assert verify_password_reset_token(payload, pwh_new) is False


def test_password_reset_token_expired() -> None:
    pwh = hash_password("OldPassword123!")
    token = create_password_reset_token(
        user_id=42, email="user@example.com", password_hash=pwh, expires_in_hours=1
    )
    assert decode_password_reset_token(token) is not None

    future_time = datetime.now(UTC) + timedelta(hours=2)
    with patch("jwt.api_jwt.datetime") as mock_jwt_dt:
        mock_jwt_dt.now.return_value = future_time
        payload = decode_password_reset_token(token)
        assert payload is None


def test_password_reset_token_tampered() -> None:
    pwh = hash_password("OldPassword123!")
    token = create_password_reset_token(
        user_id=42, email="user@example.com", password_hash=pwh
    )
    tampered = token[:-4] + "abcd"
    assert decode_password_reset_token(tampered) is None
    assert decode_password_reset_token("") is None
    assert decode_password_reset_token(None) is None  # type: ignore[arg-type]


def test_password_reset_token_rejects_wrong_type() -> None:
    access_token = create_access_token(
        user_id=42, email="user@example.com", role="user"
    )
    assert decode_password_reset_token(access_token) is None


def test_password_reset_token_rejects_malformed_claims() -> None:
    import jwt

    from app.security import get_session_secret

    now = datetime.now(UTC)
    # Boolean user_id
    bool_token = jwt.encode(
        {
            "sub": "True",
            "user_id": True,
            "email": "user@example.com",
            "pwh": "fakehash",
            "type": "password_reset",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        get_session_secret(),
        algorithm="HS256",
    )
    assert decode_password_reset_token(bool_token) is None

    # Empty pwh
    empty_pwh_token = jwt.encode(
        {
            "sub": "42",
            "user_id": 42,
            "email": "user@example.com",
            "pwh": "   ",
            "type": "password_reset",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        get_session_secret(),
        algorithm="HS256",
    )
    assert decode_password_reset_token(empty_pwh_token) is None


# ── Email Service & Background Job Tests ────────────────────────────────────


def test_send_password_reset_email_dev_mode(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with patch.object(settings, "smtp_host", ""), caplog.at_level(logging.INFO):
        reset_url = "http://localhost:8000/reset-password?token=dev_token_123"
        result = send_password_reset_email(
            recipient="test@example.com",
            reset_url=reset_url,
        )
        assert result is True
        assert reset_url in caplog.text


def test_send_password_reset_email_prod_mode_unset_smtp() -> None:
    with (
        patch.object(settings, "environment", "production"),
        patch.object(settings, "smtp_host", ""),
    ):
        result = send_password_reset_email(
            recipient="test@example.com",
            reset_url="https://veditor.org/reset-password?token=test",
        )
        assert result is False


def test_job_send_password_reset_email() -> None:
    db = SessionLocal()
    unique_email = f"reset_job_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        # User not found skips
        res_nonexistent = job_send_password_reset_email("none@example.com")
        assert res_nonexistent is True

        # Inactive user skips
        user.is_active = False
        db.commit()
        with patch("app.email.send_password_reset_email") as mock_send:
            res_inactive = job_send_password_reset_email(user.email)
            assert res_inactive is True
            mock_send.assert_not_called()

        user.is_active = True
        db.commit()

        # Normal active user succeeds
        with (
            patch.object(settings, "base_url", "https://veditor.example.com"),
            patch(
                "app.email.send_password_reset_email", return_value=True
            ) as mock_send,
        ):
            res = job_send_password_reset_email(user.email)
            assert res is True
            mock_send.assert_called_once()
            call_kw = mock_send.call_args[1]
            assert call_kw["recipient"] == user.email
            assert "/reset-password?token=" in call_kw["reset_url"]
            assert call_kw["expire_hours"] == settings.password_reset_expire_hours

        # Insecure HTTP base_url in production raises RuntimeError
        with (
            patch.object(settings, "environment", "production"),
            patch.object(settings, "session_secret", "a" * 32),
            patch.object(settings, "base_url", "http://insecure.example.com"),
            pytest.raises(RuntimeError, match="BASE_URL must use HTTPS in production"),
        ):
            job_send_password_reset_email(user.email)

        # Delivery failure raises RuntimeError so RQ records failure
        with (
            patch.object(settings, "base_url", "https://veditor.example.com"),
            patch("app.email.send_password_reset_email", return_value=False),
            pytest.raises(RuntimeError, match="Failed to deliver"),
        ):
            job_send_password_reset_email(user.email)

        # Missing base_url when SMTP enabled raises RuntimeError
        with (
            patch.object(settings, "smtp_host", "smtp.example.com"),
            patch.object(settings, "base_url", ""),
            pytest.raises(RuntimeError, match="BASE_URL must be configured"),
        ):
            job_send_password_reset_email(user.email)
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


# ── Route Integration Tests ─────────────────────────────────────────────────


def test_login_page_renders_forgot_password_link() -> None:
    client = TestClient(app)
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'href="/forgot-password"' in resp.text
    assert "Forgot password?" in resp.text


def test_login_page_renders_reset_success_notice() -> None:
    client = TestClient(app)
    resp = client.get("/login?reset=success")
    assert resp.status_code == 200
    assert "Your password has been reset successfully" in resp.text


def test_forgot_password_page_renders() -> None:
    client = TestClient(app)
    resp = client.get("/forgot-password")
    assert resp.status_code == 200
    assert "Reset Password" in resp.text
    assert 'action="/forgot-password"' in resp.text


def test_forgot_password_page_redirects_authenticated_user() -> None:
    db = SessionLocal()
    unique_email = f"auth_user_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Pass123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        from app.security import create_session_token

        session_token = create_session_token(user.id, user.role)
        client.cookies.set("veditor_session", session_token)

        resp = client.get("/forgot-password", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/studio"
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_forgot_password_invalid_email() -> None:
    client = TestClient(app)
    resp = client.post("/forgot-password", data={"email": "invalid-email"})
    assert resp.status_code == 400
    assert "Please enter a valid email address." in resp.text


def test_forgot_password_anti_enumeration() -> None:
    db = SessionLocal()
    unique_email = f"existing_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Pass123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    inactive_email = f"inactive_{uuid.uuid4().hex[:6]}@example.com"
    inactive_user = models.User(
        email=inactive_email,
        hashed_password=hash_password("Pass123!"),
        role="user",
        is_active=False,
        is_verified=True,
    )
    db.add(inactive_user)
    db.commit()
    db.refresh(inactive_user)

    client = TestClient(app)
    try:
        # 1. Existing active user
        with patch("app.routes.auth.light_queue.enqueue") as mock_enqueue_exist:
            resp_exist = client.post("/forgot-password", data={"email": unique_email})
            assert resp_exist.status_code == 200
            assert "If an account exists with that email address" in resp_exist.text
            mock_enqueue_exist.assert_called_once_with(
                job_send_password_reset_email, unique_email
            )

        # 2. Inactive user
        with patch("app.routes.auth.light_queue.enqueue") as mock_enqueue_inactive:
            resp_inactive = client.post(
                "/forgot-password", data={"email": inactive_email}
            )
            assert resp_inactive.status_code == 200
            assert "If an account exists with that email address" in resp_inactive.text
            mock_enqueue_inactive.assert_called_once_with(
                job_send_password_reset_email, inactive_email
            )

        # 3. Non-existent user
        nonexistent = f"nonexistent_{uuid.uuid4().hex[:6]}@example.com"
        with patch("app.routes.auth.light_queue.enqueue") as mock_enqueue_nonexist:
            resp_nonexist = client.post("/forgot-password", data={"email": nonexistent})
            assert resp_nonexist.status_code == 200
            assert "If an account exists with that email address" in resp_nonexist.text
            mock_enqueue_nonexist.assert_called_once_with(
                job_send_password_reset_email, nonexistent
            )

        # Compare responses after normalizing the submitted email
        norm_exist = resp_exist.text.replace(unique_email, "EMAIL_PLACEHOLDER")
        norm_inactive = resp_inactive.text.replace(inactive_email, "EMAIL_PLACEHOLDER")
        norm_nonexist = resp_nonexist.text.replace(nonexistent, "EMAIL_PLACEHOLDER")
        assert norm_exist == norm_inactive == norm_nonexist

        # Assert neither response contains account-existence disclosures
        for marker in [
            "user found",
            "account was found",
            "user exists",
            "not found",
            "does not exist",
            "unregistered",
        ]:
            assert marker not in norm_exist.lower()
    finally:
        db.execute(
            delete(models.User).where(models.User.id.in_([user.id, inactive_user.id]))
        )
        db.commit()
        db.close()


def test_forgot_password_retry_consistency_on_enqueue_failure() -> None:
    """Enqueue failure clears rate-limit key equally for existing and unknown accounts."""
    db = SessionLocal()
    unique_email = f"retry_exist_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Pass123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        # Existing account: enqueue failure deletes rate key and allows retry
        with patch(
            "app.routes.auth.light_queue.enqueue",
            side_effect=RuntimeError("Redis down"),
        ):
            resp1 = client.post("/forgot-password", data={"email": unique_email})
            assert resp1.status_code == 200
        # Immediate retry succeeds (not 429)
        with patch("app.routes.auth.light_queue.enqueue"):
            resp2 = client.post("/forgot-password", data={"email": unique_email})
            assert resp2.status_code == 200

        # Non-existent account: enqueue failure deletes rate key and allows retry identically
        nonexistent = f"retry_nonexist_{uuid.uuid4().hex[:6]}@example.com"
        with patch(
            "app.routes.auth.light_queue.enqueue",
            side_effect=RuntimeError("Redis down"),
        ):
            resp3 = client.post("/forgot-password", data={"email": nonexistent})
            assert resp3.status_code == 200
        # Immediate retry succeeds (not 429)
        with patch("app.routes.auth.light_queue.enqueue"):
            resp4 = client.post("/forgot-password", data={"email": nonexistent})
            assert resp4.status_code == 200
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_forgot_password_rate_limiting() -> None:
    client = TestClient(app)
    email = f"ratelimit_{uuid.uuid4().hex[:6]}@example.com"

    with patch("app.routes.auth.light_queue.enqueue"):
        resp1 = client.post("/forgot-password", data={"email": email})
        assert resp1.status_code == 200

        resp2 = client.post("/forgot-password", data={"email": email})
        assert resp2.status_code == 429
        assert "Please wait" in resp2.text


def test_forgot_password_redis_down_fails_closed() -> None:
    client = TestClient(app)
    email = f"redisdown_{uuid.uuid4().hex[:6]}@example.com"

    with patch(
        "app.routes.auth.redis_conn.set",
        side_effect=Exception("Redis connection refused"),
    ):
        resp = client.post("/forgot-password", data={"email": email})
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.text


def test_reset_password_scanner_safe_get() -> None:
    db = SessionLocal()
    unique_email = f"scanner_{uuid.uuid4().hex[:6]}@example.com"
    initial_pwh = hash_password("InitialPass123!")
    user = models.User(
        email=unique_email,
        hashed_password=initial_pwh,
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        token = create_password_reset_token(user.id, user.email, user.hashed_password)

        # Scanner pre-fetch GET request
        resp = client.get(f"/reset-password?token={token}")
        assert resp.status_code == 200
        assert "Set New Password" in resp.text
        assert user.email in resp.text

        # Verify database was NOT mutated
        db.refresh(user)
        assert user.hashed_password == initial_pwh
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_reset_password_page_invalid_tokens() -> None:
    client = TestClient(app)

    # Missing token
    resp_empty = client.get("/reset-password?token=")
    assert resp_empty.status_code == 400
    assert "Password reset link is missing or invalid" in resp_empty.text

    # Tampered token
    resp_tampered = client.get("/reset-password?token=invalid.jwt.token")
    assert resp_tampered.status_code == 400
    assert "Password reset link is invalid or has expired" in resp_tampered.text


def test_reset_password_submit_success_and_single_use() -> None:
    db = SessionLocal()
    unique_email = f"reset_full_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("OriginalPassword123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        token = create_password_reset_token(user.id, user.email, user.hashed_password)

        # Submit new password
        resp = client.post(
            "/reset-password",
            data={
                "token": token,
                "password": "BrandNewPassword123!",
                "password_confirm": "BrandNewPassword123!",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?reset=success"

        # Verify DB updated
        db.refresh(user)
        assert verify_password("BrandNewPassword123!", user.hashed_password) is True
        assert verify_password("OriginalPassword123!", user.hashed_password) is False

        # Attempt to use the same token a second time (single-use enforcement)
        resp_reuse = client.post(
            "/reset-password",
            data={
                "token": token,
                "password": "AnotherPassword123!",
                "password_confirm": "AnotherPassword123!",
            },
            follow_redirects=False,
        )
        assert resp_reuse.status_code == 400
        assert "invalid, expired, or has already been used" in resp_reuse.text
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_reset_password_validation_errors() -> None:
    db = SessionLocal()
    unique_email = f"valid_user_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("OldPassword123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        token = create_password_reset_token(user.id, user.email, user.hashed_password)

        # Too short (< 8)
        resp_short = client.post(
            "/reset-password",
            data={"token": token, "password": "short", "password_confirm": "short"},
        )
        assert resp_short.status_code == 400
        assert "at least 8 characters" in resp_short.text

        # Too long (> 256)
        long_pw = "A" * 257
        resp_long = client.post(
            "/reset-password",
            data={"token": token, "password": long_pw, "password_confirm": long_pw},
        )
        assert resp_long.status_code == 400
        assert "not exceed 256 characters" in resp_long.text

        # Passwords mismatch
        resp_mismatch = client.post(
            "/reset-password",
            data={
                "token": token,
                "password": "ValidPassword123!",
                "password_confirm": "Different123!",
            },
        )
        assert resp_mismatch.status_code == 400
        assert "Passwords do not match" in resp_mismatch.text
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_reset_password_verifies_unverified_account() -> None:
    db = SessionLocal()
    unique_email = f"unverified_reset_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("OldPassword123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        token = create_password_reset_token(user.id, user.email, user.hashed_password)
        resp = client.post(
            "/reset-password",
            data={
                "token": token,
                "password": "NewVerifiedPass123!",
                "password_confirm": "NewVerifiedPass123!",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?reset=success"

        db.refresh(user)
        assert user.is_verified is True
        assert user.verified_at is not None
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_reset_password_rejects_deactivated_account() -> None:
    db = SessionLocal()
    unique_email = f"deactivated_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("OldPassword123!"),
        role="user",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        token = create_password_reset_token(user.id, user.email, user.hashed_password)

        # Deactivate user before reset attempt
        user.is_active = False
        db.commit()

        resp = client.post(
            "/reset-password",
            data={
                "token": token,
                "password": "NewAttemptPassword123!",
                "password_confirm": "NewAttemptPassword123!",
            },
        )
        assert resp.status_code == 400
        assert "invalid, expired, or has already been used" in resp.text
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_password_reset_revokes_existing_sessions() -> None:
    """Resetting password revokes older sessions across all devices."""
    db = SessionLocal()
    unique_email = f"session_revoke_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("OriginalPassword123!"),
        role="admin",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        # Establish a valid session token on device A and issue access tokens
        session_token = create_session_token(
            user.id, user.role, password_hash=user.hashed_password
        )
        access_token = create_access_token(
            user.id, user.email, user.role, password_hash=user.hashed_password
        )
        access_token_legacy = create_access_token(user.id, user.email, user.role)
        client.cookies.set("veditor_session", session_token)

        # Before reset: session is valid and accesses /studio without redirection
        resp_before = client.get("/studio", follow_redirects=False)
        assert resp_before.status_code == 200

        # Before reset: bearer access tokens succeed
        bearer_before = client.get(
            "/events",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert bearer_before.status_code == 200

        # Device B initiates and completes password reset
        reset_token = create_password_reset_token(
            user.id, user.email, user.hashed_password
        )
        client_b = TestClient(app)
        reset_resp = client_b.post(
            "/reset-password",
            data={
                "token": reset_token,
                "password": "NewUpdatedPassword123!",
                "password_confirm": "NewUpdatedPassword123!",
            },
            follow_redirects=False,
        )
        assert reset_resp.status_code == 303

        # After reset: Device A's old session token is revoked and cannot access /studio
        resp_after = client.get("/studio", follow_redirects=False)
        assert resp_after.status_code == 302
        assert "/login" in resp_after.headers["location"]

        # API bearer auth with old access tokens is rejected with 401
        api_resp = client.get(
            "/events",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert api_resp.status_code == 401

        api_resp_legacy = client.get(
            "/events",
            headers={"Authorization": f"Bearer {access_token_legacy}"},
        )
        assert api_resp_legacy.status_code == 401

        # Direct unit verification that session revocation detects the change
        old_payload = decode_session_token(session_token)
        assert old_payload is not None
        db.refresh(user)
        assert (
            verify_session_token_not_revoked(
                old_payload, user.hashed_password, user=user
            )
            is False
        )

        # Verification for fingerprint-free tokens:
        legacy_payload = decode_access_token(access_token_legacy)
        assert legacy_payload is not None
        # Evaluated against user state: rejected because token predates password reset
        assert (
            verify_session_token_not_revoked(
                legacy_payload, user.hashed_password, user=user
            )
            is False
        )
        # Evaluated with missing revocation state: must be rejected
        assert (
            verify_session_token_not_revoked(
                legacy_payload, user.hashed_password, session_revoked_at=None
            )
            is False
        )
        # Evaluated with unreadable revocation state: must be rejected
        assert (
            verify_session_token_not_revoked(
                legacy_payload,
                user.hashed_password,
                session_revoked_at="invalid-date",  # type: ignore[arg-type]
            )
            is False
        )

        # New login with new password produces a working session
        login_resp = client.post(
            "/login",
            data={"email": user.email, "password": "NewUpdatedPassword123!"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 303
        new_session = login_resp.cookies.get("veditor_session")
        assert new_session is not None
        client.cookies.set("veditor_session", new_session)
        resp_new = client.get("/studio", follow_redirects=False)
        assert resp_new.status_code == 200
    finally:
        db.execute(delete(models.User).where(models.User.id == user.id))
        db.commit()
        db.close()


def test_password_reset_error_page_renders_configured_expiry() -> None:
    """Error page displays the configured lifetime rather than a hardcoded 1 hour."""
    client = TestClient(app)
    with patch.object(settings, "password_reset_expire_hours", 3):
        resp = client.get("/reset-password?token=invalid_token")
        assert resp.status_code == 400
        assert "valid for 3 hours" in resp.text

    with patch.object(settings, "password_reset_expire_hours", 1):
        resp = client.get("/reset-password?token=invalid_token")
        assert resp.status_code == 400
        assert "valid for 1 hour" in resp.text
