import hashlib
import hmac
import json
import urllib.error
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import get_client
from app.cli import create_client
from app.db import get_db
from app.main import app
from app.storage import get_storage_backend
from app.tasks import job_cut, job_deliver_webhook
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


# --- 4. Trigger on Talk Cut Bounds (Active Cutting) Integration Tests ---


def test_submit_cut_bounds_triggers_webhook_when_configured():
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

            # Verify both job_cut and webhook deliveries were enqueued
            assert mock_enqueue.call_count == 2
            cut_call = mock_enqueue.call_args_list[0]
            assert cut_call[0][0] == job_cut
            assert cut_call[0][1] == 10
            assert cut_call[0][2] == "10/raw/recording.mp4"

            webhook_call = mock_enqueue.call_args_list[1]
            assert webhook_call[0][0] == job_deliver_webhook
            assert webhook_call[0][1] == "https://subscriber.example/hook"
            assert webhook_call[0][2] == "sub-secret-key"

            payload = webhook_call[0][3]
            assert payload["talk_id"] == 10
            assert payload["event_id"] == 1
            assert "timestamp" in payload
            # Acceptance criteria: ONLY talk_id, event_id, and timestamp
            assert set(payload.keys()) == {"talk_id", "event_id", "timestamp"}
    finally:
        app.dependency_overrides.clear()


def test_approve_talk_does_not_trigger_webhook():
    """Approving a talk moves it to pending_bounds; it must not trigger the webhook."""
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
        with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
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


def test_submit_cut_bounds_silent_when_no_webhook_url():
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
        status="pending_bounds",
        raw_duration_seconds=3600.0,
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
    fake_storage.put("11/raw/recording.mp4", b"raw video bytes")
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
            resp = client.post(
                "/talks/11/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 202
            assert resp.json()["status"] == "cutting"
            # Only job_cut should be enqueued, no webhook
            mock_enqueue.assert_called_once()
            assert mock_enqueue.call_args[0][0] == job_cut
    finally:
        app.dependency_overrides.clear()


def test_reject_talk_does_not_trigger_webhook():
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
        with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
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


def test_submit_cut_bounds_webhook_failure_decoupled():
    mock_db = MagicMock()
    mock_client = models.Client(
        id=1,
        event_ids=[1],
        webhook_url="https://subscriber.example/hook",
        webhook_secret="sub-secret-key",
    )
    mock_talk = models.Talk(
        id=13,
        event_id=1,
        title="Talk With Webhook Queue Outage",
        room="Room D",
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
    fake_storage.put("13/raw/recording.mp4", b"raw video bytes")
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    def enqueue_side_effect(fn, *args, **kwargs):
        if fn == job_deliver_webhook:
            raise RuntimeError("Webhook queue broker unreachable")
        return MagicMock()

    try:
        with patch(
            "app.routes.talks.light_queue.enqueue",
            side_effect=enqueue_side_effect,
        ):
            resp = client.post(
                "/talks/13/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid_key"},
            )
            # Must succeed despite webhook enqueue failure
            assert resp.status_code == 202
            assert resp.json()["status"] == "cutting"
            assert mock_talk.status == "cutting"
            assert mock_db.commit.called
    finally:
        app.dependency_overrides.clear()


def test_submit_cut_bounds_multi_client_isolation():
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
        raw_duration_seconds=3600.0,
    )

    def mock_query(model):
        q = MagicMock()
        if model is models.Talk:
            q.filter.return_value.first.return_value = mock_talk
            q.filter.return_value.with_for_update.return_value = q.filter.return_value
        elif model is models.Client:
            q.filter.return_value.all.return_value = [mock_client_1, mock_client_2]
            q.filter.return_value.first.return_value = mock_client_1
        return q

    mock_db.query = mock_query

    fake_storage = FakeStorageBackend()
    fake_storage.put("14/raw/recording.mp4", b"raw video bytes")
    app.dependency_overrides[get_client] = lambda: mock_client_1
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    calls_made = []

    def enqueue_side_effect(fn, *args, **kwargs):
        calls_made.append((fn, args))
        if fn == job_deliver_webhook and args[0] == "https://subscriber-1.example/hook":
            raise RuntimeError("Failure for client 1")
        return MagicMock()

    try:
        with patch(
            "app.routes.talks.light_queue.enqueue",
            side_effect=enqueue_side_effect,
        ):
            resp = client.post(
                "/talks/14/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid_key"},
            )
            assert resp.status_code == 202
            assert resp.json()["status"] == "cutting"
            assert mock_talk.status == "cutting"

            # Verify job_cut + client_1 attempt + client_2 attempt (all 3 called)
            assert len(calls_made) == 3
            assert calls_made[0][0] == job_cut
            assert calls_made[1][0] == job_deliver_webhook
            assert calls_made[1][1][0] == "https://subscriber-1.example/hook"
            assert calls_made[2][0] == job_deliver_webhook
            assert calls_made[2][1][0] == "https://subscriber-2.example/hook"
    finally:
        app.dependency_overrides.clear()
