import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest
from fastapi.testclient import TestClient
from pytest import LogCaptureFixture
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import hash_api_key
from app.cli import create_client
from app.config import settings
from app.consts import TalkStatus
from app.main import app
from app.review_handlers import handle_needs_work
from app.routes.events import _get_event_client
from app.storage import get_storage_backend
from app.tasks import (
    _redact_url,
    job_cut,
    job_deliver_webhook,
    job_preview,
    job_publish,
)
from app.webhook import dispatch_talk_webhook, get_candidate_clients
from tests.conftest import FakeStorageBackend, TestSessionLocal

client = TestClient(app)

API_KEY = "valid_key"


def _create_api_client(
    db: Session, event_ids: list[int], api_key: str = API_KEY, **extra: Any
) -> models.Client:
    client_row = models.Client(
        hashed_key=hash_api_key(api_key), event_ids=event_ids, **extra
    )
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


def _create_event(db: Session, name: str = "Webhook Test Event") -> models.Event:
    event = models.Event(name=name)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db: Session,
    event_id: int,
    *,
    title: str = "Webhook Test Talk",
    status: str = "waiting_for_files",
    **extra: Any,
) -> models.Talk:
    talk = models.Talk(
        event_id=event_id,
        title=title,
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        status=status,
        **extra,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def _worker_session_factory(db_session: Session) -> Callable[[], Session]:
    """Return a factory of job-worker sessions bound to the test connection."""

    def factory() -> Session:
        return TestSessionLocal(bind=db_session.get_bind())

    return factory


# --- 1. Registration & API Lifecycle Tests ---


def test_register_webhook_unauthorized() -> None:
    resp = client.post(
        "/client/webhook",
        json={"url": "https://example.com/webhook"},
    )
    assert resp.status_code == 401


def test_register_webhook_invalid_url(db_session: Session) -> None:
    _create_api_client(db_session, event_ids=[1])

    resp = client.post(
        "/client/webhook",
        json={"url": "not-a-valid-url"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_register_webhook_custom_secret(db_session: Session) -> None:
    client_row = _create_api_client(db_session, event_ids=[1])

    resp = client.post(
        "/client/webhook",
        json={
            "url": "https://example.com/webhook",
            "secret": "my-shared-secret-12345",
        },
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "registered"
    assert data["url"] == "https://example.com/webhook"
    assert data["secret"] == "my-shared-secret-12345"

    db_session.refresh(client_row)
    assert client_row.webhook_url == "https://example.com/webhook"
    assert client_row.webhook_secret == "my-shared-secret-12345"


def test_register_webhook_auto_generated_secret(db_session: Session) -> None:
    client_row = _create_api_client(db_session, event_ids=[1])

    resp = client.post(
        "/client/webhook",
        json={"url": "https://example.com/webhook"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "registered"
    assert data["url"] == "https://example.com/webhook"
    assert len(data["secret"]) >= 32

    db_session.refresh(client_row)
    assert client_row.webhook_secret == data["secret"]


def test_get_and_delete_webhook(db_session: Session) -> None:
    client_row = _create_api_client(
        db_session,
        event_ids=[1],
        webhook_url="https://example.com/webhook",
        webhook_secret="supersecret",
    )

    # GET webhook info
    resp = client.get("/client/webhook", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"] == "https://example.com/webhook"
    assert data["has_secret"] is True

    # DELETE webhook
    resp = client.delete("/client/webhook", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 204

    db_session.refresh(client_row)
    assert client_row.webhook_url is None
    assert client_row.webhook_secret is None


# --- 2. CLI Tests ---


def test_cli_create_client_with_webhook(db_session: Session) -> None:
    event = _create_event(db_session, name="Event 5")

    create_client(
        db_session,
        event_name=None,
        event_id=event.id,
        webhook_url="https://example.com/hook",
        webhook_secret="cli-secret",
    )

    added_client = db_session.scalar(
        select(models.Client).where(
            models.Client.webhook_url == "https://example.com/hook"
        )
    )
    assert isinstance(added_client, models.Client)
    assert added_client.webhook_url == "https://example.com/hook"
    assert added_client.webhook_secret == "cli-secret"
    assert added_client.event_ids == [event.id]


def test_cli_create_client_webhook_secret_without_url_does_not_persist_event(
    db_session: Session,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            db_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url=None,
            webhook_secret="secret_without_url",
        )
    assert excinfo.value.code == 1
    assert not db_session.new


def test_cli_create_client_webhook_secret_too_long_does_not_persist_event(
    db_session: Session,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            db_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url="https://example.com/hook",
            webhook_secret="a" * 256,
        )
    assert excinfo.value.code == 1
    assert not db_session.new


def test_cli_create_client_invalid_webhook_url_does_not_persist_event(
    db_session: Session,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            db_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url="ftp://invalid.com/hook",
            webhook_secret="valid_secret",
        )
    assert excinfo.value.code == 1
    assert not db_session.new


# --- 3. Delivery, HMAC Signing & Retry Unit Tests ---


def test_register_webhook_secret_too_long(db_session: Session) -> None:
    _create_api_client(db_session, event_ids=[1])

    resp = client.post(
        "/client/webhook",
        json={
            "url": "https://example.com/webhook",
            "secret": "s" * 256,
        },
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_cli_secret_without_url_fails(db_session: Session) -> None:
    with pytest.raises(SystemExit):
        create_client(
            db_session,
            event_name=None,
            event_id=5,
            webhook_url=None,
            webhook_secret="cli-secret",
        )
    assert not db_session.new


def test_job_deliver_webhook_missing_args() -> None:
    assert job_deliver_webhook("", "secret", {}) is False
    assert job_deliver_webhook("https://example.com", "", {}) is False


def test_job_deliver_webhook_hmac_signing_and_success() -> None:
    payload = {"talk_id": 42, "event_id": 7, "timestamp": "2026-09-13T03:00:00Z"}
    secret = "test-secret-key"
    url = "https://example.com/incoming-webhook"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("app.tasks._webhook_opener.open", return_value=mock_resp) as mock_open:
        result = job_deliver_webhook(url, secret, payload)

        assert result is True
        assert mock_open.call_count == 1

        req = mock_open.call_args[0][0]
        assert req.get_full_url() == url
        assert req.get_method() == "POST"

        # Verify Content-Type and Signature headers
        assert req.headers["Content-type"] == "application/json"
        sig_header = req.headers["X-veditor-signature"]
        assert sig_header.startswith("sha256=")
        expected_sig = hmac.new(
            secret.encode("utf-8"), req.data, hashlib.sha256
        ).hexdigest()
        assert sig_header == f"sha256={expected_sig}"

        # Verify transmitted body matches canonical JSON
        sent_body = json.loads(req.data.decode("utf-8"))
        assert sent_body == payload


def test_job_deliver_webhook_logs_redacted_url(
    caplog: LogCaptureFixture,
) -> None:
    url = "https://user:pass@example.com:8443/hook?token=abc#frag"

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with (
        caplog.at_level("INFO", logger="app.tasks"),
        patch("app.tasks._webhook_opener.open", return_value=mock_resp) as mock_open,
    ):
        assert job_deliver_webhook(url, "secret", {"talk_id": 1}) is True

    # The request still goes to the full URL; only the log line is redacted.
    assert mock_open.call_args[0][0].get_full_url() == url
    assert "https://example.com:8443" in caplog.text
    for secret_part in ("user", "pass", "/hook", "token=abc", "frag"):
        assert secret_part not in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://[::1]:8443/hook?t=1", "https://[::1]:8443"),
        ("https://u:p@[2001:db8::1]/hook", "https://[2001:db8::1]"),
        # Slack and Discord put the token in the path.
        ("https://hooks.slack.com/services/T0/B0/secret", "https://hooks.slack.com"),
        ("https://example.com\\secret/hook", "<invalid url>"),
        # Characters that cannot appear in a host would let the log line be
        # forged or carry other data.
        ("https://example.com%0asecret/hook", "<invalid url>"),
        ("https://exa mple.com/hook", "<invalid url>"),
        ("https://example.com\nINFO forged/hook", "<invalid url>"),
        (None, "<invalid url>"),
        (b"https://example.com/hook", "<invalid url>"),
        (123, "<invalid url>"),
    ],
)
def test_redact_url_ipv6_and_non_strings(url: str, expected: str) -> None:
    assert _redact_url(url) == expected


def test_job_deliver_webhook_failure_log_omits_url(
    caplog: LogCaptureFixture,
) -> None:
    url = "https://example.com/hook?token=abc"
    # http.client puts the whole request target into some error messages.
    error = ValueError(f"URL can't contain control characters. {url!r}")

    with (
        caplog.at_level("INFO", logger="app.tasks"),
        patch("app.tasks._webhook_opener.open", side_effect=error),
        patch("time.sleep"),
    ):
        assert job_deliver_webhook(url, "secret", {"talk_id": 1}) is False

    assert "ValueError" in caplog.text
    assert "token=abc" not in caplog.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            HTTPError(
                "https://example.com/hook?token=abc", 503, "busy", Message(), None
            ),
            "HTTPError (HTTP 503)",
        ),
        (
            URLError(ConnectionRefusedError("refused")),
            "URLError (ConnectionRefusedError)",
        ),
    ],
)
def test_job_deliver_webhook_failure_log_includes_safe_details(
    caplog: LogCaptureFixture, error: BaseException, expected: str
) -> None:
    with (
        caplog.at_level("INFO", logger="app.tasks"),
        patch("app.tasks._webhook_opener.open", side_effect=error),
        patch("time.sleep"),
    ):
        assert (
            job_deliver_webhook("https://example.com/hook?token=abc", "s", {}) is False
        )

    assert expected in caplog.text
    assert "token=abc" not in caplog.text


def test_job_deliver_webhook_retry_success_on_second_attempt() -> None:
    payload = {"talk_id": 1, "event_id": 1, "timestamp": "2026-09-13T03:00:00Z"}
    secret = "retry-secret"
    url = "https://example.com/failing-then-succeeding"

    mock_success_resp = MagicMock()
    mock_success_resp.status = 200
    mock_success_resp.__enter__.return_value = mock_success_resp

    error_resp = URLError("Connection refused")

    with (
        patch(
            "app.tasks._webhook_opener.open",
            side_effect=[error_resp, mock_success_resp],
        ) as mock_open,
        patch("time.sleep") as mock_sleep,
    ):
        result = job_deliver_webhook(url, secret, payload)

        assert result is True
        assert mock_open.call_count == 2
        mock_sleep.assert_called_once_with(1)


def test_job_deliver_webhook_fails_quietly_after_two_attempts() -> None:
    payload = {"talk_id": 1, "event_id": 1, "timestamp": "2026-09-13T03:00:00Z"}
    secret = "fail-secret"
    url = "https://example.com/permanently-failing"

    error_resp = URLError("Network unreachable")

    with (
        patch("app.tasks._webhook_opener.open", side_effect=error_resp) as mock_open,
        patch("time.sleep") as mock_sleep,
    ):
        # Must return False and not raise an unhandled exception
        result = job_deliver_webhook(url, secret, payload)

        assert result is False
        assert mock_open.call_count == 2
        mock_sleep.assert_called_once_with(1)


# --- 4. Webhook Lifecycle Realignment Tests (Issue #274) ---


def test_submit_cut_bounds_does_not_prematurely_trigger_webhook(
    db_session: Session,
) -> None:
    """Submitting cut bounds must only enqueue job_cut and NOT dispatch a premature webhook."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Keynote Talk",
        external_id="TALK_10_EXT",
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )

    fake_storage = FakeStorageBackend()
    fake_storage.put(f"{talk.id}/raw/recording.mp4", b"raw video bytes")
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "cutting"

        # ONLY job_cut must be enqueued - no premature webhook while cutting/generating
        assert mock_enqueue.call_count == 1
        cut_call = mock_enqueue.call_args_list[0]
        assert cut_call[0][0] == job_cut
        assert cut_call[0][1] == talk.id
        assert cut_call[0][2] == f"{talk.id}/raw/recording.mp4"

    db_session.refresh(talk)
    assert talk.status == TalkStatus.CUTTING
    assert talk.cut_start == 10.0
    assert talk.cut_end == 2700.0


def test_approve_talk_does_not_dispatch_webhook(db_session: Session) -> None:
    """Approving a talk transitions it to pending_intro_outro and does not dispatch a webhook."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Keynote Talk",
        external_id="TALK_10_EXT",
        status="pending_approval",
    )

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "approve"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_intro_outro"
        mock_enqueue.assert_not_called()

    db_session.refresh(talk)
    assert talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_handoff_triggers_bounds_pending_webhook(
    db_session: Session,
) -> None:
    """Handoff from pending_intro_outro to pending_bounds dispatches talk.bounds_pending."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Keynote Talk",
        external_id="TALK_10_EXT",
        status="pending_intro_outro",
    )

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_bounds"

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        assert call_args[1] == "https://subscriber.example/hook"
        assert call_args[2] == "sub-secret-key"

        payload = call_args[3]
        assert payload["event"] == "talk.bounds_pending"
        assert payload["talk_id"] == talk.id
        assert payload["event_id"] == event.id
        assert payload["external_id"] == "TALK_10_EXT"
        assert "timestamp" in payload


def test_approve_talk_silent_when_no_webhook_url(db_session: Session) -> None:
    """Approving talk succeeds without error even if client has no webhook configured."""
    event = _create_event(db_session)
    _create_api_client(db_session, event_ids=[event.id])
    talk = _create_talk(
        db_session,
        event.id,
        title="Another Talk",
        status="pending_approval",
    )

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "approve"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_intro_outro"
        mock_enqueue.assert_not_called()


def test_reject_talk_does_not_trigger_webhook(db_session: Session) -> None:
    """Rejecting a talk transitions to rejected and does not dispatch webhook."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Rejected Talk",
        status="pending_approval",
    )

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "reject"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        mock_enqueue.assert_not_called()

    db_session.refresh(talk)
    assert talk.status == TalkStatus.REJECTED


def test_review_needs_work_triggers_bounds_pending_webhook(db_session: Session) -> None:
    """When a reviewer marks needs_work, talk moves to pending_bounds and dispatches talk.bounds_pending."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Talk Needs Work",
        external_id="TALK_15_EXT",
        status="preview",
    )

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        payload = schemas.ReviewRequest(
            decision=schemas.ReviewDecision.needs_work,
            note="Audio needs trimming at beginning",
        )
        response = handle_needs_work(talk, payload, db_session)
        assert response.talk.status == TalkStatus.PENDING_BOUNDS
        assert talk.status == TalkStatus.PENDING_BOUNDS

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        dispatched_payload = call_args[3]
        assert dispatched_payload["event"] == "talk.bounds_pending"
        assert dispatched_payload["talk_id"] == talk.id
        assert dispatched_payload["external_id"] == "TALK_15_EXT"


def test_job_preview_dispatches_preview_ready_webhook(db_session: Session) -> None:
    """Completing job_preview transitions talk to preview and dispatches talk.preview_ready."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Preview Ready Talk",
        external_id="TALK_20_EXT",
        status="generating_previews",
    )

    fake_storage = FakeStorageBackend()
    fake_storage.put(f"{talk.id}/cut/cut.mp4", b"cut video bytes")

    def fake_generate_preview(
        src: Path | str, dst: Path | str, preset: str | None = None
    ) -> None:
        Path(dst).write_bytes(b"preview video bytes")

    with (
        patch("app.tasks.SessionLocal", _worker_session_factory(db_session)),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.generate_preview", side_effect=fake_generate_preview),
        patch("app.tasks._cache_waveform"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
    ):
        job_preview(talk.id, f"{talk.id}/cut/cut.mp4")

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        assert call_args[1] == "https://subscriber.example/hook"
        assert call_args[2] == "sub-secret-key"

        payload = call_args[3]
        assert payload["event"] == "talk.preview_ready"
        assert payload["talk_id"] == talk.id
        assert payload["event_id"] == event.id
        assert payload["external_id"] == "TALK_20_EXT"
        assert "timestamp" in payload

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PREVIEW


def test_job_publish_dispatches_talk_published_webhook(db_session: Session) -> None:
    """Completing job_publish transitions talk to done and dispatches talk.published with video_url and duration."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Published Keynote",
        external_id="TALK_ABC123",
        status="uploading",
        cut_start=10.0,
        cut_end=1834.5,
    )

    fake_storage = FakeStorageBackend()
    fake_storage.put(f"{talk.id}/transcode/master.mp4", b"final master video bytes")

    with (
        patch("app.tasks.SessionLocal", _worker_session_factory(db_session)),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.publish"),
        patch("app.tasks.cleanup_intermediates"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
        patch.object(settings, "base_url", ""),
    ):
        job_publish(talk.id, f"{talk.id}/transcode/master.mp4")

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        assert call_args[1] == "https://subscriber.example/hook"
        assert call_args[2] == "sub-secret-key"

        payload = call_args[3]
        assert payload["event"] == "talk.published"
        assert payload["talk_id"] == talk.id
        assert payload["event_id"] == event.id
        assert payload["external_id"] == "TALK_ABC123"
        assert payload["duration_seconds"] == 1824.5
        assert payload["video_url"] == f"/studio/media/{talk.id}/final/master.mp4"
        assert "timestamp" in payload

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.DONE


def test_job_publish_uses_custom_base_url(db_session: Session) -> None:
    """When settings.base_url is configured, video_url is prefixed accordingly."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Published Keynote",
        external_id="TALK_ABC123",
        status="uploading",
        raw_duration_seconds=500.0,
    )

    fake_storage = FakeStorageBackend()
    fake_storage.put(f"{talk.id}/transcode/master.mp4", b"final master video bytes")

    with (
        patch.object(settings, "base_url", "https://veditor.example.org"),
        patch("app.tasks.SessionLocal", _worker_session_factory(db_session)),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.publish"),
        patch("app.tasks.cleanup_intermediates"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
    ):
        job_publish(talk.id, f"{talk.id}/transcode/master.mp4")

        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args[0][3]
        assert (
            payload["video_url"] == "https://veditor.example.org/studio/media/"
            f"{talk.id}/final/master.mp4"
        )
        assert payload["duration_seconds"] == 500.0


def test_candidate_clients_platform_and_event_matching(db_session: Session) -> None:
    """Verify get_candidate_clients correctly resolves event-specific and platform clients."""
    client_event = _create_api_client(
        db_session,
        event_ids=[10],
        api_key="key-event",
        webhook_url="https://event.example/hook",
        webhook_secret="secret1",
    )
    client_platform = _create_api_client(
        db_session,
        event_ids=[],
        api_key="key-platform",
        is_platform=True,
        webhook_url="https://platform.example/hook",
        webhook_secret="secret2",
    )
    _create_api_client(
        db_session,
        event_ids=[99],
        api_key="key-unrelated",
        webhook_url="https://unrelated.example/hook",
        webhook_secret="secret3",
    )
    _create_api_client(
        db_session,
        event_ids=[10],
        api_key="key-nosecret",
        webhook_url="https://nosecret.example/hook",
        webhook_secret=None,
    )

    candidates = get_candidate_clients(db_session, event_id=10)
    candidate_ids = {c.id for c in candidates}
    assert client_event.id in candidate_ids
    assert client_platform.id in candidate_ids
    assert len(candidate_ids) == 2  # unrelated and secret-less clients excluded

    # Passing explicit client_id for unrelated event client should not include it
    client_unrelated = db_session.scalar(
        select(models.Client).where(
            models.Client.webhook_url == "https://unrelated.example/hook"
        )
    )
    assert client_unrelated is not None
    candidates_with_unrelated = get_candidate_clients(
        db_session, event_id=10, client_id=client_unrelated.id
    )
    assert client_unrelated.id not in {c.id for c in candidates_with_unrelated}


def test_multi_client_isolation_on_webhook_dispatch(db_session: Session) -> None:
    """Verify that an enqueue failure on one client does not abort webhooks for other clients."""
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        api_key="key-1",
        webhook_url="https://subscriber-1.example/hook",
        webhook_secret="secret-1",
    )
    _create_api_client(
        db_session,
        event_ids=[event.id],
        api_key="key-2",
        webhook_url="https://subscriber-2.example/hook",
        webhook_secret="secret-2",
    )
    talk = _create_talk(
        db_session,
        event.id,
        title="Multi Client Talk",
        status="pending_bounds",
    )

    calls_made: list[tuple[Any, tuple[Any, ...]]] = []

    def enqueue_side_effect(fn: Any, *args: Any, **kwargs: Any) -> MagicMock:
        calls_made.append((fn, args))
        if fn == job_deliver_webhook and args[0] == "https://subscriber-1.example/hook":
            raise RuntimeError("Failure for client 1")
        return MagicMock()

    with patch(
        "app.webhook.light_queue.enqueue",
        side_effect=enqueue_side_effect,
    ):
        dispatch_talk_webhook("talk.bounds_pending", talk, db_session)

        # Verify client_1 attempt + client_2 attempt (both attempted despite client 1 failing)
        assert len(calls_made) == 2
        attempted_urls = {args[0] for _, args in calls_made}
        assert attempted_urls == {
            "https://subscriber-1.example/hook",
            "https://subscriber-2.example/hook",
        }


def test_get_event_client_prioritizes_configured_webhook(db_session: Session) -> None:
    event = _create_event(db_session)
    _create_api_client(
        db_session,
        event_ids=[event.id],
        api_key="key-no-webhook",
    )
    client_with_webhook = _create_api_client(
        db_session,
        event_ids=[event.id],
        api_key="key-with-webhook",
        webhook_url="https://example.com/webhook",
        webhook_secret="sec",
    )

    chosen = _get_event_client(event.id, db_session)
    assert chosen is not None
    assert chosen.id == client_with_webhook.id
