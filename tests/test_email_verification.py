"""Tests for email verification flow in VEditor."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.config import settings
from app.db import SessionLocal
from app.email import send_verification_email
from app.main import app
from app.security import (
    create_access_token,
    create_email_verification_token,
    decode_email_verification_token,
    hash_password,
)
from app.tasks import job_send_verification_email

# ── Token Tests ─────────────────────────────────────────────────────────────


def test_verification_token_lifecycle():
    token = create_email_verification_token(
        user_id=42, email="user@example.com", expires_in_hours=24
    )
    assert isinstance(token, str)

    payload = decode_email_verification_token(token)
    assert payload is not None
    assert payload["user_id"] == 42
    assert payload["email"] == "user@example.com"
    assert payload["type"] == "email_verification"


def test_verification_token_expired():
    token = create_email_verification_token(
        user_id=42, email="user@example.com", expires_in_hours=1
    )
    assert decode_email_verification_token(token) is not None

    # Advance time past token expiration in PyJWT's claim validator
    future_time = datetime.now(UTC) + timedelta(hours=2)
    with patch("jwt.api_jwt.datetime") as mock_jwt_dt:
        mock_jwt_dt.now.return_value = future_time
        payload = decode_email_verification_token(token)
        assert payload is None


def test_verification_token_tampered():
    token = create_email_verification_token(user_id=42, email="user@example.com")
    tampered = token[:-4] + "abcd"
    assert decode_email_verification_token(tampered) is None
    assert decode_email_verification_token("") is None
    assert decode_email_verification_token(None) is None  # type: ignore[arg-type]


def test_verification_token_rejects_wrong_type():
    access_token = create_access_token(
        user_id=42, email="user@example.com", role="user"
    )
    assert decode_email_verification_token(access_token) is None


def test_verification_token_rejects_bool_and_empty_claims():
    import jwt

    from app.security import get_session_secret

    now = datetime.now(UTC)
    # Boolean user_id
    bool_token = jwt.encode(
        {
            "sub": "True",
            "user_id": True,
            "email": "user@example.com",
            "type": "email_verification",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        get_session_secret(),
        algorithm="HS256",
    )
    assert decode_email_verification_token(bool_token) is None

    # Empty email
    empty_email_token = jwt.encode(
        {
            "sub": "42",
            "user_id": 42,
            "email": "   ",
            "type": "email_verification",
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        get_session_secret(),
        algorithm="HS256",
    )
    assert decode_email_verification_token(empty_email_token) is None


# ── Email Service Tests ─────────────────────────────────────────────────────


def test_send_verification_email_dev_mode():
    with patch.object(settings, "smtp_host", ""):
        result = send_verification_email(
            recipient="test@example.com",
            verify_url="http://localhost:8000/verify-email?token=xyz",
        )
        assert result is True


def test_send_verification_email_production_mode_unset_smtp():
    with (
        patch.object(settings, "smtp_host", ""),
        patch.object(settings, "environment", "production"),
        patch("app.email.logger.error") as mock_log_err,
    ):
        result = send_verification_email(
            recipient="test@example.com",
            verify_url="http://localhost:8000/verify-email?token=xyz",
        )
        assert result is False
        mock_log_err.assert_called_once()
        # Verify bearer token/URL is never logged in production
        logged_msg = mock_log_err.call_args[0][0]
        assert "token=xyz" not in logged_msg


def test_send_verification_email_smtp_success():
    with (
        patch.object(settings, "smtp_host", "smtp.example.com"),
        patch.object(settings, "smtp_port", 587),
        patch.object(settings, "smtp_ssl", False),
        patch.object(settings, "smtp_tls", True),
        patch.object(settings, "smtp_user", "smtp_user"),
        patch.object(settings, "smtp_password", "smtp_pass"),
        patch("smtplib.SMTP") as mock_smtp,
    ):
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_server

        result = send_verification_email(
            recipient="test@example.com",
            verify_url="http://localhost:8000/verify-email?token=xyz",
        )
        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("smtp_user", "smtp_pass")
        mock_server.send_message.assert_called_once()


def test_send_verification_email_smtp_failure():
    with (
        patch.object(settings, "smtp_host", "smtp.example.com"),
        patch("smtplib.SMTP", side_effect=OSError("Connection refused")),
    ):
        result = send_verification_email(
            recipient="test@example.com",
            verify_url="http://localhost:8000/verify-email?token=xyz",
        )
        assert result is False


# ── Worker Job Tests ────────────────────────────────────────────────────────


def test_job_send_verification_email():
    db = SessionLocal()
    user = models.User(
        email=f"worker_test_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password=hash_password("Pass1234!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        with (
            patch.object(settings, "base_url", "https://veditor.example.com"),
            patch("app.email.send_verification_email", return_value=True) as mock_send,
        ):
            res = job_send_verification_email(user.id, user.email, "fake_token")
            assert res is True
            mock_send.assert_called_once()
            _, kwargs = mock_send.call_args
            assert kwargs["recipient"] == user.email
            assert "fake_token" in kwargs["verify_url"]
            assert "https://veditor.example.com/verify-email" in kwargs["verify_url"]

        # If user is already verified, worker skips quietly
        user.is_verified = True
        db.commit()

        with (
            patch.object(settings, "base_url", "https://veditor.example.com"),
            patch("app.email.send_verification_email") as mock_send,
        ):
            res = job_send_verification_email(user.id, user.email, "fake_token")
            assert res is True
            mock_send.assert_not_called()

        # Delivery failure raises RuntimeError so RQ records the job as failed
        user.is_verified = False
        db.commit()

        with (
            patch.object(settings, "base_url", "https://veditor.example.com"),
            patch("app.email.send_verification_email", return_value=False),
            pytest.raises(RuntimeError, match="Failed to deliver"),
        ):
            job_send_verification_email(user.id, user.email, "fake_token")

        # Missing BASE_URL when SMTP is enabled raises RuntimeError
        with (
            patch.object(settings, "smtp_host", "smtp.example.com"),
            patch.object(settings, "base_url", ""),
            pytest.raises(RuntimeError, match="BASE_URL must be configured"),
        ):
            job_send_verification_email(user.id, user.email, "fake_token")
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


# ── Route Integration Tests ─────────────────────────────────────────────────


def test_signup_creates_unverified_user_and_enqueues_email():
    client = TestClient(app)
    unique_email = f"signup_test_{uuid.uuid4().hex[:6]}@example.com"

    with patch("app.routes.auth.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            "/signup",
            data={
                "email": unique_email,
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/verify-email/pending?email=")
        # No session cookie should be issued on signup
        assert "veditor_session" not in resp.cookies

        # Background job was enqueued
        mock_enqueue.assert_called_once()
        args = mock_enqueue.call_args[0]
        assert args[0] == job_send_verification_email
        assert args[2] == unique_email

    # Check user in DB is unverified
    db = SessionLocal()
    user = db.query(models.User).filter(models.User.email == unique_email).first()
    try:
        assert user is not None
        assert user.is_active is True
        assert user.is_verified is False
        assert user.verified_at is None
    finally:
        if user:
            db.delete(user)
            db.commit()
        db.close()


def test_signup_enqueue_failure_retains_user_and_shows_recovery_state():
    client = TestClient(app)
    unique_email = f"signup_retry_{uuid.uuid4().hex[:6]}@example.com"

    with patch(
        "app.routes.auth.light_queue.enqueue",
        side_effect=RuntimeError("Redis connection failed"),
    ):
        resp = client.post(
            "/signup",
            data={
                "email": unique_email,
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert "/verify-email/pending" in resp.headers["location"]
        assert "error=delivery_failed" in resp.headers["location"]

        # Follow redirect and verify recovery alert message is shown
        resp_pending = client.get(resp.headers["location"])
        assert resp_pending.status_code == 200
        assert "unable to deliver the verification email" in resp_pending.text
        assert "Resend Verification Email" in resp_pending.text

    # User remains created in DB as unverified so they can retry without losing their account
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == unique_email).first()
        assert user is not None
        assert user.is_verified is False
    finally:
        if user:
            db.delete(user)
            db.commit()
        db.close()


def test_unverified_user_cannot_login():
    db = SessionLocal()
    unique_email = f"unverified_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        # Web UI login rejection
        resp = client.post(
            "/login",
            data={"email": unique_email, "password": "Password123!"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Please verify your email address" in resp.text
        assert "veditor_session" not in resp.cookies

        # API bearer token rejection
        api_resp = client.post(
            "/api/auth/token",
            json={"email": unique_email, "password": "Password123!"},
        )
        assert api_resp.status_code == 403
        assert api_resp.json()["detail"] == "Email address not verified"
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


def test_verify_email_flow_success():
    db = SessionLocal()
    unique_email = f"verify_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    token = create_email_verification_token(user.id, user.email)

    try:
        # GET renders confirmation page safely without mutating state (scanner defense)
        resp_get = client.get(f"/verify-email?token={token}")
        assert resp_get.status_code == 200
        assert "Confirm Verification" in resp_get.text
        assert "veditor_session" not in resp_get.cookies
        db.refresh(user)
        assert user.is_verified is False

        # POST performs atomic verification and establishes session
        resp = client.post(
            "/verify-email", data={"token": token}, follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/studio?verified=1"
        assert "veditor_session" in resp.cookies

        # Verify DB updated
        db.refresh(user)
        assert user.is_verified is True
        assert user.verified_at is not None

        # Re-using the same verification link is rejected (single-use token defense)
        resp_reuse_get = client.get(
            f"/verify-email?token={token}", follow_redirects=False
        )
        assert resp_reuse_get.status_code == 400
        assert "already been verified" in resp_reuse_get.text

        resp_reuse_post = client.post(
            "/verify-email", data={"token": token}, follow_redirects=False
        )
        assert resp_reuse_post.status_code == 400
        assert "already been verified" in resp_reuse_post.text
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


def test_verify_email_deactivated_user_fails():
    db = SessionLocal()
    unique_email = f"deactivated_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=False,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    token = create_email_verification_token(user.id, user.email)

    try:
        resp = client.get(f"/verify-email?token={token}", follow_redirects=False)
        assert resp.status_code == 400
        assert "deactivated" in resp.text
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


def test_verify_email_invalid_or_expired_token():
    client = TestClient(app)

    # Missing token
    resp_empty = client.get("/verify-email", follow_redirects=False)
    assert resp_empty.status_code == 400
    assert "Verification link is missing or invalid" in resp_empty.text

    # Corrupt token
    resp_invalid = client.get(
        "/verify-email?token=invalid.token.here", follow_redirects=False
    )
    assert resp_invalid.status_code == 400
    assert "Verification link is invalid or has expired" in resp_invalid.text


def test_verify_email_resend_endpoints():
    db = SessionLocal()
    unique_email = f"resend_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        # GET page renders cleanly
        resp_get = client.get(f"/verify-email/resend?email={unique_email}")
        assert resp_get.status_code == 200
        assert "Resend Verification" in resp_get.text

        # POST sends new email
        with patch("app.routes.auth.light_queue.enqueue") as mock_enqueue:
            resp_post = client.post(
                "/verify-email/resend",
                data={"email": unique_email},
            )
            assert resp_post.status_code == 200
            assert "a new verification link has been sent" in resp_post.text
            mock_enqueue.assert_called_once()

            # Successive request is rate-limited (HTTP 429)
            resp_rate_limited = client.post(
                "/verify-email/resend",
                data={"email": unique_email},
            )
            assert resp_rate_limited.status_code == 429
            assert "Please wait" in resp_rate_limited.text
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


def test_verify_email_concurrent_update_rejected():
    """Verify that when an update matches 0 rows (concurrency race), verification is rejected."""
    db = SessionLocal()
    unique_email = f"concurrent_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    token = create_email_verification_token(user.id, user.email)

    try:
        from sqlalchemy.orm import Session

        orig_execute = Session.execute

        def mock_execute(self, statement, *args, **kwargs):
            if getattr(statement, "is_update", False):
                fake_result = MagicMock()
                fake_result.rowcount = 0
                return fake_result
            return orig_execute(self, statement, *args, **kwargs)

        with patch("sqlalchemy.orm.Session.execute", new=mock_execute):
            resp = client.post(
                "/verify-email", data={"token": token}, follow_redirects=False
            )
            assert resp.status_code == 400
            assert "already been verified" in resp.text
            assert "veditor_session" not in resp.cookies
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()


def test_verify_email_resend_redis_failure_fails_closed():
    """Verify that when Redis is unreachable, resend fails closed with HTTP 503 instead of bypassing cooldown."""
    client = TestClient(app)
    with patch(
        "app.routes.auth.redis_conn.set", side_effect=ConnectionError("Redis down")
    ):
        resp = client.post(
            "/verify-email/resend",
            data={"email": "resend_redis_fail@example.com"},
        )
        assert resp.status_code == 503
        assert "temporarily unavailable" in resp.text


def test_verify_email_resend_enqueue_failure_clears_rate_key_and_returns_uniform_response():
    """Verify that when enqueuing a resend verification email fails, rate key is cleared and uniform response is returned."""
    db = SessionLocal()
    unique_email = f"resend_enq_fail_{uuid.uuid4().hex[:6]}@example.com"
    user = models.User(
        email=unique_email,
        hashed_password=hash_password("Password123!"),
        role="user",
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    client = TestClient(app)
    try:
        with (
            patch("app.routes.auth.redis_conn.set", return_value=True),
            patch("app.routes.auth.redis_conn.delete") as mock_delete,
            patch(
                "app.routes.auth.light_queue.enqueue",
                side_effect=RuntimeError("Queue unavailable"),
            ),
        ):
            resp = client.post(
                "/verify-email/resend",
                data={"email": unique_email},
            )
            # Uniform 200 response to prevent email enumeration
            assert resp.status_code == 200
            assert "a new verification link has been sent" in resp.text
            # But rate key was cleared so legitimate user can immediately retry
            mock_delete.assert_called_once_with(
                f"rate:resend_verification:{unique_email}"
            )
    finally:
        db.query(models.User).filter(models.User.id == user.id).delete()
        db.commit()
        db.close()
