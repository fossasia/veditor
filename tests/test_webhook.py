import hashlib
import hmac
import json
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models, schemas
from app.auth import get_client
from app.cli import create_client
from app.config import settings
from app.db import get_db
from app.main import app
from app.review_handlers import handle_needs_work
from app.storage import get_storage_backend
from app.tasks import job_cut, job_deliver_webhook, job_preview, job_publish
from app.webhook import dispatch_talk_webhook, get_candidate_clients
from tests.conftest import FakeStorageBackend

client = TestClient(app)


# --- 1. Registration & API Lifecycle Tests ---


def test_register_webhook_unauthorized():
    resp = client.post(
        "/client/webhook",
        json={"url": "https://example.com/webhook"},
    )
    assert resp.status_code == 401


def test_register_webhook_invalid_url():
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        resp = client.post(
            "/client/webhook",
            json={"url": "not-a-valid-url"},
            headers={"X-API-Key": "valid_key"},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_register_webhook_custom_secret():
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db = MagicMock()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        resp = client.post(
            "/client/webhook",
            json={
                "url": "https://example.com/webhook",
                "secret": "my-shared-secret-12345",
            },
            headers={"X-API-Key": "valid_key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["url"] == "https://example.com/webhook"
        assert data["secret"] == "my-shared-secret-12345"

        assert mock_client.webhook_url == "https://example.com/webhook"
        assert mock_client.webhook_secret == "my-shared-secret-12345"
        assert mock_db.commit.called
    finally:
        app.dependency_overrides.clear()


def test_register_webhook_auto_generated_secret():
    mock_client = models.Client(id=1, event_ids=[1])
    mock_db = MagicMock()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        resp = client.post(
            "/client/webhook",
            json={"url": "https://example.com/webhook"},
            headers={"X-API-Key": "valid_key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["url"] == "https://example.com/webhook"
        assert len(data["secret"]) >= 32
        assert mock_client.webhook_secret == data["secret"]
        assert mock_db.commit.called
    finally:
        app.dependency_overrides.clear()


def test_get_and_delete_webhook():
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://example.com/webhook",
        webhook_secret="supersecret",
    )
    mock_db = MagicMock()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        # GET webhook info
        resp = client.get("/client/webhook", headers={"X-API-Key": "valid_key"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://example.com/webhook"
        assert data["has_secret"] is True

        # DELETE webhook
        resp = client.delete("/client/webhook", headers={"X-API-Key": "valid_key"})
        assert resp.status_code == 204
        assert mock_client.webhook_url is None
        assert mock_client.webhook_secret is None
        assert mock_db.commit.called
    finally:
        app.dependency_overrides.clear()


# --- 2. CLI Tests ---


def test_cli_create_client_with_webhook():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.first.return_value = (
        models.Event(id=5, name="Event 5")
    )

    create_client(
        mock_session,
        event_name=None,
        event_id=5,
        webhook_url="https://example.com/hook",
        webhook_secret="cli-secret",
    )

    added_client = mock_session.add.call_args[0][0]
    assert isinstance(added_client, models.Client)
    assert added_client.webhook_url == "https://example.com/hook"
    assert added_client.webhook_secret == "cli-secret"
    assert mock_session.commit.called


def test_cli_create_client_webhook_secret_without_url_does_not_persist_event():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            mock_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url=None,
            webhook_secret="secret_without_url",
        )
    assert excinfo.value.code == 1
    assert mock_session.add.call_count == 0
    assert not mock_session.commit.called


def test_cli_create_client_webhook_secret_too_long_does_not_persist_event():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            mock_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url="https://example.com/hook",
            webhook_secret="a" * 256,
        )
    assert excinfo.value.code == 1
    assert mock_session.add.call_count == 0
    assert not mock_session.commit.called


def test_cli_create_client_invalid_webhook_url_does_not_persist_event():
    mock_session = MagicMock()
    with pytest.raises(SystemExit) as excinfo:
        create_client(
            mock_session,
            event_name="Should Not Be Created",
            event_id=None,
            webhook_url="ftp://invalid.com/hook",
            webhook_secret="valid_secret",
        )
    assert excinfo.value.code == 1
    assert mock_session.add.call_count == 0
    assert not mock_session.commit.called


# --- 3. Delivery, HMAC Signing & Retry Unit Tests ---


def test_register_webhook_secret_too_long():
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client

    try:
        resp = client.post(
            "/client/webhook",
            json={
                "url": "https://example.com/webhook",
                "secret": "s" * 256,
            },
            headers={"X-API-Key": "valid_key"},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_cli_secret_without_url_fails():
    mock_session = MagicMock()
    with pytest.raises(SystemExit):
        create_client(
            mock_session,
            event_name=None,
            event_id=5,
            webhook_url=None,
            webhook_secret="cli-secret",
        )


def test_job_deliver_webhook_missing_args():
    assert job_deliver_webhook("", "secret", {}) is False
    assert job_deliver_webhook("https://example.com", "", {}) is False


def test_job_deliver_webhook_hmac_signing_and_success():
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


def test_job_deliver_webhook_retry_success_on_second_attempt():
    payload = {"talk_id": 1, "event_id": 1, "timestamp": "2026-09-13T03:00:00Z"}
    secret = "retry-secret"
    url = "https://example.com/failing-then-succeeding"

    mock_success_resp = MagicMock()
    mock_success_resp.status = 200
    mock_success_resp.__enter__.return_value = mock_success_resp

    error_resp = urllib.error.URLError("Connection refused")

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


def test_job_deliver_webhook_fails_quietly_after_two_attempts():
    payload = {"talk_id": 1, "event_id": 1, "timestamp": "2026-09-13T03:00:00Z"}
    secret = "fail-secret"
    url = "https://example.com/permanently-failing"

    error_resp = urllib.error.URLError("Network unreachable")

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


def test_submit_cut_bounds_does_not_prematurely_trigger_webhook():
    """Submitting cut bounds must only enqueue job_cut and NOT dispatch a premature webhook."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        external_id="TALK_10_EXT",
        title="Keynote Talk",
        room="Main Hall",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
            q.filter.return_value.with_for_update.return_value = q.filter.return_value
        elif model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    fake_storage.put("10/raw/recording.mp4", b"raw video bytes")
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/10/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 202
            assert resp.json()["status"] == "cutting"
            assert mock_talk.status == "cutting"
            assert mock_talk.cut_start == 10.0
            assert mock_talk.cut_end == 2700.0
            assert mock_db.commit.called

            # ONLY job_cut must be enqueued - no premature webhook while cutting/generating
            assert mock_enqueue.call_count == 1
            cut_call = mock_enqueue.call_args_list[0]
            assert cut_call[0][0] == job_cut
            assert cut_call[0][1] == 10
            assert cut_call[0][2] == "10/raw/recording.mp4"
    finally:
        app.dependency_overrides.clear()


def test_approve_talk_does_not_dispatch_webhook():
    """Approving a talk transitions it to pending_intro_outro and does not dispatch a webhook."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        external_id="TALK_10_EXT",
        title="Keynote Talk",
        room="Main Hall",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
            q.filter.return_value.with_for_update.return_value = q.filter.return_value
        elif model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/10/approve",
                json={"decision": "approve"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "pending_intro_outro"
            assert mock_talk.status == "pending_intro_outro"
            mock_enqueue.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_intro_outro_handoff_triggers_bounds_pending_webhook():
    """Handoff from pending_intro_outro to pending_bounds dispatches talk.bounds_pending."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        external_id="TALK_10_EXT",
        title="Keynote Talk",
        room="Main Hall",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_intro_outro",
    )

    mock_event = models.Event(id=1, name="Test Event")

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
            q.filter.return_value.with_for_update.return_value = q.filter.return_value
        elif model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        elif model is models.Event:
            q.filter.return_value.first.return_value = mock_event
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/10/handoff",
                json={"include_intro": False, "include_outro": False},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "pending_bounds"
            assert mock_talk.status == "pending_bounds"

            mock_enqueue.assert_called_once()
            call_args = mock_enqueue.call_args[0]
            assert call_args[0] == job_deliver_webhook
            assert call_args[1] == "https://subscriber.example/hook"
            assert call_args[2] == "sub-secret-key"

            payload = call_args[3]
            assert payload["event"] == "talk.bounds_pending"
            assert payload["talk_id"] == 10
            assert payload["event_id"] == 1
            assert payload["external_id"] == "TALK_10_EXT"
            assert "timestamp" in payload
    finally:
        app.dependency_overrides.clear()


def test_approve_talk_silent_when_no_webhook_url():
    """Approving talk succeeds without error even if client has no webhook configured."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url=None,
        webhook_secret=None,
    )
    mock_talk = models.Talk(
        id=11,
        event_id=1,
        title="Another Talk",
        room="Room B",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
            q.filter.return_value.with_for_update.return_value = q.filter.return_value
        elif model is models.Client:
            q.filter.return_value.all.return_value = []
            q.filter.return_value.first.return_value = None
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/11/approve",
                json={"decision": "approve"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "pending_intro_outro"
            mock_enqueue.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_reject_talk_does_not_trigger_webhook():
    """Rejecting a talk transitions to rejected and does not dispatch webhook."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=12,
        event_id=1,
        title="Rejected Talk",
        room="Room C",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )

    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk
    mock_db.query.return_value.filter.return_value.with_for_update.return_value = (
        mock_db.query.return_value.filter.return_value
    )

    fake_storage = FakeStorageBackend()
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/12/approve",
                json={"decision": "reject"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "rejected"
            assert mock_talk.status == "rejected"
            mock_enqueue.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_review_needs_work_triggers_bounds_pending_webhook():
    """When a reviewer marks needs_work, talk moves to pending_bounds and dispatches talk.bounds_pending."""
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=15,
        event_id=1,
        external_id="TALK_15_EXT",
        title="Talk Needs Work",
        room="Room F",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
        elif model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        return q

    mock_db.query = mock_query

    def fake_flush():
        for call in mock_db.add.call_args_list:
            obj = call[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

    mock_db.flush.side_effect = fake_flush

    with patch("app.webhook.light_queue.enqueue") as mock_enqueue:
        payload = schemas.ReviewRequest(
            decision=schemas.ReviewDecision.needs_work,
            note="Audio needs trimming at beginning",
        )
        response = handle_needs_work(mock_talk, payload, mock_db)
        assert response.talk.status == "pending_bounds"
        assert mock_talk.status == "pending_bounds"

        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        payload = call_args[3]
        assert payload["event"] == "talk.bounds_pending"
        assert payload["talk_id"] == 15
        assert payload["external_id"] == "TALK_15_EXT"


def test_job_preview_dispatches_preview_ready_webhook():
    """Completing job_preview transitions talk to preview and dispatches talk.preview_ready."""
    mock_db = MagicMock()
    mock_db.__enter__.return_value = mock_db
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=20,
        event_id=1,
        external_id="TALK_20_EXT",
        title="Preview Ready Talk",
        room="Room G",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="generating_previews",
    )
    mock_job = models.Job(
        id=101,
        talk_id=20,
        kind="preview",
        status="running",
    )

    mock_db.get.side_effect = lambda model, obj_id: (
        mock_talk if model is models.Talk else mock_job
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    fake_storage.put("20/cut/cut.mp4", b"cut video bytes")

    def fake_generate_preview(src, dst, preset=None):
        from pathlib import Path

        Path(dst).write_bytes(b"preview video bytes")

    with (
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.generate_preview", side_effect=fake_generate_preview),
        patch("app.tasks._cache_waveform"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
    ):
        job_preview(20, "20/cut/cut.mp4")

        assert mock_talk.status == "preview"
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        assert call_args[1] == "https://subscriber.example/hook"
        assert call_args[2] == "sub-secret-key"

        payload = call_args[3]
        assert payload["event"] == "talk.preview_ready"
        assert payload["talk_id"] == 20
        assert payload["event_id"] == 1
        assert payload["external_id"] == "TALK_20_EXT"
        assert "timestamp" in payload


def test_job_publish_dispatches_talk_published_webhook():
    """Completing job_publish transitions talk to done and dispatches talk.published with video_url and duration."""
    mock_db = MagicMock()
    mock_db.__enter__.return_value = mock_db
    mock_client = models.Client(
        id=1,
        event_ids=[10],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=42,
        event_id=10,
        external_id="TALK_ABC123",
        title="Published Keynote",
        room="Auditorium",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="uploading",
        cut_start=10.0,
        cut_end=1834.5,
    )
    mock_job = models.Job(
        id=202,
        talk_id=42,
        kind="publish",
        status="running",
    )

    mock_db.get.side_effect = lambda model, obj_id: (
        mock_talk if model is models.Talk else mock_job
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Client:
            q.filter.return_value.all.return_value = [mock_client]
            q.filter.return_value.first.return_value = mock_client
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    fake_storage.put("42/transcode/master.mp4", b"final master video bytes")

    with (
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.publish"),
        patch("app.tasks.cleanup_intermediates"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
    ):
        job_publish(42, "42/transcode/master.mp4")

        assert mock_talk.status == "done"
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0]
        assert call_args[0] == job_deliver_webhook
        assert call_args[1] == "https://subscriber.example/hook"
        assert call_args[2] == "sub-secret-key"

        payload = call_args[3]
        assert payload["event"] == "talk.published"
        assert payload["talk_id"] == 42
        assert payload["event_id"] == 10
        assert payload["external_id"] == "TALK_ABC123"
        assert payload["duration_seconds"] == 1824.5
        assert payload["video_url"] == "/studio/media/42/final/master.mp4"
        assert "timestamp" in payload


def test_job_publish_uses_custom_base_url():
    """When settings.base_url is configured, video_url is prefixed accordingly."""
    mock_db = MagicMock()
    mock_db.__enter__.return_value = mock_db
    mock_client = models.Client(
        id=1,
        event_ids=[10],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=42,
        event_id=10,
        external_id="TALK_ABC123",
        title="Published Keynote",
        room="Auditorium",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="uploading",
        raw_duration_seconds=500.0,
    )
    mock_job = models.Job(id=203, talk_id=42, kind="publish", status="running")

    mock_db.get.side_effect = lambda model, obj_id: (
        mock_talk if model is models.Talk else mock_job
    )
    mock_db.query.return_value.filter.return_value.all.return_value = [mock_client]
    mock_db.query.return_value.filter.return_value.first.return_value = mock_client

    fake_storage = FakeStorageBackend()
    fake_storage.put("42/transcode/master.mp4", b"final master video bytes")

    with (
        patch.object(settings, "base_url", "https://veditor.example.org"),
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.publish"),
        patch("app.tasks.cleanup_intermediates"),
        patch("app.webhook.light_queue.enqueue") as mock_enqueue,
    ):
        job_publish(42, "42/transcode/master.mp4")

        assert mock_talk.status == "done"
        mock_enqueue.assert_called_once()
        payload = mock_enqueue.call_args[0][3]
        assert (
            payload["video_url"]
            == "https://veditor.example.org/studio/media/42/final/master.mp4"
        )
        assert payload["duration_seconds"] == 500.0


def test_candidate_clients_platform_and_event_matching():
    """Verify get_candidate_clients correctly resolves event-specific and platform clients."""
    mock_db = MagicMock()
    client_event = models.Client(
        id=1,
        event_ids=[10],
        is_platform=False,
        webhook_url="https://event.example/hook",
        webhook_secret="secret1",
    )
    client_platform = models.Client(
        id=2,
        event_ids=[],
        is_platform=True,
        webhook_url="https://platform.example/hook",
        webhook_secret="secret2",
    )
    client_unrelated = models.Client(
        id=3,
        event_ids=[99],
        is_platform=False,
        webhook_url="https://unrelated.example/hook",
        webhook_secret="secret3",
    )
    client_no_secret = models.Client(
        id=4,
        event_ids=[10],
        is_platform=False,
        webhook_url="https://nosecret.example/hook",
        webhook_secret=None,
    )

    mock_db.query.return_value.filter.return_value.all.return_value = [
        client_event,
        client_platform,
        client_unrelated,
        client_no_secret,
    ]
    mock_db.get_bind.return_value = None

    candidates = get_candidate_clients(mock_db, event_id=10)
    candidate_ids = {c.id for c in candidates}
    assert 1 in candidate_ids
    assert 2 in candidate_ids
    assert 3 not in candidate_ids
    assert 4 not in candidate_ids  # Excluded because no secret

    # Passing explicit client_id for unrelated event client should not include it
    mock_db.query.return_value.filter.return_value.first.return_value = client_unrelated
    candidates_with_unrelated = get_candidate_clients(mock_db, event_id=10, client_id=3)
    assert 3 not in {c.id for c in candidates_with_unrelated}


def test_multi_client_isolation_on_webhook_dispatch():
    """Verify that an enqueue failure on one client does not abort webhooks for other clients."""
    mock_db = MagicMock()
    mock_client_1 = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber-1.example/hook",
        webhook_secret="secret-1",
    )
    mock_client_2 = models.Client(
        id=2,
        event_ids=[1],
        webhook_url="https://subscriber-2.example/hook",
        webhook_secret="secret-2",
    )
    mock_talk = models.Talk(
        id=14,
        event_id=1,
        title="Multi Client Talk",
        room="Room E",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Client:
            q.filter.return_value.all.return_value = [mock_client_1, mock_client_2]
            q.filter.return_value.first.return_value = mock_client_1
        return q

    mock_db.query = mock_query

    calls_made = []

    def enqueue_side_effect(fn, *args, **kwargs):
        calls_made.append((fn, args))
        if fn == job_deliver_webhook and args[0] == "https://subscriber-1.example/hook":
            raise RuntimeError("Failure for client 1")
        return MagicMock()

    with patch(
        "app.webhook.light_queue.enqueue",
        side_effect=enqueue_side_effect,
    ):
        dispatch_talk_webhook("talk.bounds_pending", mock_talk, mock_db)

        # Verify client_1 attempt + client_2 attempt (both attempted despite client 1 failing)
        assert len(calls_made) == 2
        assert calls_made[0][1][0] == "https://subscriber-1.example/hook"
        assert calls_made[1][1][0] == "https://subscriber-2.example/hook"
