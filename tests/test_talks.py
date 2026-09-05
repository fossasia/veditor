from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import models
from app.auth import get_client
from app.db import get_db
from app.ingest import IngestPathRejectedError, InsufficientStorageError
from app.main import app
from app.storage import get_storage_backend
from app.tasks import (
    STAGE_CONFIG,
    job_cut,
    job_detect,
    job_preview,
)
from tests.conftest import FakeStorageBackend

client = TestClient(app)


def test_post_talks_unauthorized():
    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Introduction to FastAPI",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 401


def test_post_talks_forbidden_event():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[2])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Introduction to FastAPI",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Client is not authorized to access this event"

    app.dependency_overrides.clear()


def test_post_talks_create_success():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    # Simulate talk does not exist
    mock_db.query.return_value.filter.return_value.first.return_value = None

    start_time = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    end_time = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)

    def fake_refresh(obj):
        obj.id = 42

    mock_db.refresh.side_effect = fake_refresh

    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Keynote Address",
            "room": "Main Stage",
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
        },
        headers={"X-API-Key": "valid_key"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 42
    assert data["event_id"] == 1
    assert data["title"] == "Keynote Address"
    assert data["room"] == "Main Stage"
    assert data["status"] == "waiting_for_files"
    assert data["preview_urls"] == []

    assert mock_db.add.called
    assert mock_db.commit.called

    app.dependency_overrides.clear()


def test_post_talks_upsert_update_success():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    start_time = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    initial_end = datetime(2026, 9, 1, 10, 45, tzinfo=UTC)
    updated_end = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)

    existing_talk = models.Talk(
        id=10,
        event_id=1,
        title="Keynote Address",
        room="Old Room",
        start=start_time,
        end=initial_end,
        status="cutting",
    )

    mock_db.query.return_value.filter.return_value.first.return_value = existing_talk

    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Keynote Address",
            "room": "Updated Room",
            "start": start_time.isoformat(),
            "end": updated_end.isoformat(),
        },
        headers={"X-API-Key": "valid_key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 10
    assert data["room"] == "Updated Room"
    assert data["status"] == "cutting"
    assert existing_talk.room == "Updated Room"
    assert existing_talk.end == updated_end
    assert existing_talk.status == "cutting"

    assert not mock_db.add.called
    assert mock_db.commit.called

    app.dependency_overrides.clear()


def test_post_talks_concurrent_race_handled():
    """Verify that a race condition on insert (IntegrityError) recovers and performs upsert."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    start_time = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    updated_end = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)

    existing_talk = models.Talk(
        id=20,
        event_id=1,
        title="Concurrent Talk",
        room="Original Room",
        start=start_time,
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="waiting_for_files",
    )

    mock_db.query.return_value.filter.return_value.first.side_effect = [
        None,
        existing_talk,
    ]

    mock_db.commit.side_effect = [
        IntegrityError("duplicate key", params=None, orig=Exception("uq")),
        None,
    ]

    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Concurrent Talk",
            "room": "Updated Concurrent Room",
            "start": start_time.isoformat(),
            "end": updated_end.isoformat(),
        },
        headers={"X-API-Key": "valid_key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 20
    assert data["room"] == "Updated Concurrent Room"
    assert existing_talk.room == "Updated Concurrent Room"
    assert existing_talk.end == updated_end
    assert mock_db.rollback.called

    app.dependency_overrides.clear()


def test_get_talk_unauthorized():
    response = client.get("/talks/1")
    assert response.status_code == 401


def test_get_talk_not_found():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = None

    response = client.get("/talks/999", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"

    app.dependency_overrides.clear()


def test_get_talk_unauthorized_event_returns_404():
    """Talk in unowned event must return 404 (not 403) to avoid leaking existence."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[2])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Secret Talk",
        room="Room X",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.get("/talks/1", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"

    app.dependency_overrides.clear()


def test_get_talk_authorized_without_previews():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    mock_talk = models.Talk(
        id=5,
        event_id=1,
        title="Public Talk",
        room="Auditorium",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.get("/talks/5", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 5
    assert data["title"] == "Public Talk"
    assert data["preview_urls"] == []

    app.dependency_overrides.clear()


def test_get_talk_authorized_with_previews():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()
    fake_storage.put("5/preview/small_video.mp4", b"dummy_small")
    fake_storage.put("5/preview/big_video.mp4", b"dummy_big")

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    mock_talk = models.Talk(
        id=5,
        event_id=1,
        title="Preview Ready Talk",
        room="Auditorium",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.get("/talks/5", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 5
    assert data["status"] == "preview"
    assert len(data["preview_urls"]) == 2
    assert "memory://5/preview/small_video.mp4" in data["preview_urls"]
    assert "memory://5/preview/big_video.mp4" in data["preview_urls"]

    app.dependency_overrides.clear()


# --- Tests for POST /talks/{id}/recordings ---


def test_post_recording_unauthorized():
    response = client.post(
        "/talks/1/recordings",
        json={"source_path": "/some/path/video.mp4"},
    )
    assert response.status_code == 401


def test_post_recording_forbidden_event_returns_404():
    """Accessing talk outside caller's event_ids returns 404 to avoid leaking existence."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[2])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Other Event Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"

    app.dependency_overrides.clear()


def test_post_recording_talk_not_found():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = None

    response = client.post(
        "/talks/999/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"

    app.dependency_overrides.clear()


def test_post_recording_invalid_state_conflict():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Processing Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="cutting",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert "Cannot ingest recording" in response.json()["detail"]

    app.dependency_overrides.clear()


def test_post_recording_ingest_path_rejected():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    with patch(
        "app.routes.talks.stage_recording",
        side_effect=IngestPathRejectedError("Invalid path"),
    ):
        response = client.post(
            "/talks/1/recordings",
            json={"source_path": "/invalid/path.mp4"},
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid path"

    app.dependency_overrides.clear()


def test_post_recording_insufficient_storage_returns_507():
    """When disk space is insufficient, returns 507 Insufficient Storage and avoids state advance/job creation."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    try:
        mock_talk = models.Talk(
            id=1,
            event_id=1,
            title="Test Talk",
            room="Room 1",
            start=datetime.now(UTC),
            end=datetime.now(UTC),
            status="waiting_for_files",
        )
        mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

        with (
            patch(
                "app.routes.talks.stage_recording",
                side_effect=InsufficientStorageError(
                    required_bytes=3000, available_bytes=1000
                ),
            ),
            patch("app.routes.talks.light_queue.enqueue") as mock_enqueue,
        ):
            response = client.post(
                "/talks/1/recordings",
                json={"source_path": "/valid/path.mp4"},
                headers={"X-API-Key": "valid_key"},
            )
            assert response.status_code == 507
            assert "Insufficient storage" in response.json()["detail"]
            assert "3000" in response.json()["detail"]
            assert "1000" in response.json()["detail"]
            assert mock_talk.status == "waiting_for_files"
            mock_enqueue.assert_not_called()
            mock_db.commit.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_post_recording_success_enqueues_detect():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    with (
        patch(
            "app.routes.talks.stage_recording", return_value="1/raw/video.mp4"
        ) as mock_stage,
        patch("app.routes.talks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/recordings",
            json={"relative_key": "video.mp4"},
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 202
        data = response.json()
        assert data["id"] == 1
        assert data["status"] == "detecting"
        assert mock_talk.status == "detecting"

        mock_stage.assert_called_once()
        mock_enqueue.assert_called_once_with(
            job_detect,
            1,
            "1/raw/video.mp4",
            job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
        )

    app.dependency_overrides.clear()


def test_post_recording_duplicate_ingest_rejected_with_409():
    """Duplicate POST /recordings when talk is already in detecting state returns 409 Conflict."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="detecting",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert (
        "Cannot ingest recording for talk in status 'detecting'"
        in response.json()["detail"]
    )

    app.dependency_overrides.clear()


# --- Tests for POST /talks/{id}/approve ---


def test_post_approve_unauthorized():
    response = client.post("/talks/1/approve")
    assert response.status_code == 401


def test_post_approve_forbidden_event_returns_404():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[2])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Other Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/approve",
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"

    app.dependency_overrides.clear()


def test_post_approve_invalid_state_conflict():
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Talk in Waiting",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/approve",
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert (
        "Cannot approve/reject talk in status 'waiting_for_files'"
        in response.json()["detail"]
    )

    app.dependency_overrides.clear()


def test_post_approve_no_raw_recording_fails():
    """Approve no longer checks for raw recording existence; it just transitions to pending_bounds."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Talk to Approve",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/approve",
        headers={"X-API-Key": "valid_key"},
    )
    # Approve no longer requires a raw file — just transitions to pending_bounds
    assert response.status_code == 200
    assert response.json()["status"] == "pending_bounds"

    app.dependency_overrides.clear()


def test_post_approve_success_enqueues_cut():
    """Approve now transitions to pending_bounds; job_cut is enqueued later from /cut."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Talk to Approve",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        response = client.post(
            "/talks/1/approve",
            headers={"X-API-Key": "valid_key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["status"] == "pending_bounds"
        assert mock_talk.status == "pending_bounds"
        assert mock_db.commit.called
        # Approve no longer enqueues job_cut immediately
        mock_enqueue.assert_not_called()

    app.dependency_overrides.clear()


def test_post_approve_with_custom_raw_key():
    """Approve body no longer accepts raw_key; extra fields are ignored by Pydantic (extra=ignore) or result in 422.
    Since ApproveRequest has no raw_key field, sending it just uses the default approve decision."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Talk to Approve",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    # Old raw_key field is ignored — body parsed as ApproveRequest(decision="approve")
    response = client.post(
        "/talks/1/approve",
        json={"decision": "approve"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending_bounds"

    app.dependency_overrides.clear()


def test_post_approve_with_mismatched_talk_raw_key_rejected():
    """Old raw_key validation is gone. Sending decision=reject now terminates the talk."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(
        id=1,
        event_id=1,
        title="Talk to Approve",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_approval",
    )
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/1/approve",
        json={"decision": "reject"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    app.dependency_overrides.clear()


# --- Full Path Test: recordings -> detect -> pending_approval -> approve -> cut -> preview -> preview halt ---


def test_full_pipeline_flow_recordings_to_preview_halt():
    """
    Simulates full pipeline flow from ingest to preview halt (Phase 4):
    1. Create talk -> 'waiting_for_files'
    2. POST /recordings -> stages raw file, advances to 'detecting', enqueues job_detect
    3. Execute job_detect -> advances talk to 'pending_approval', halts
    4. POST /approve -> advances talk to 'pending_bounds'
    5. POST /cut -> advances talk to 'cutting', enqueues job_cut
    6. Execute job_cut -> advances talk to 'generating_previews', enqueues job_preview
    7. Execute job_preview -> advances talk to 'preview', halts
    8. GET /talks/{id} -> returns 'preview' state and preview_urls
    """
    from app.pipeline.detect import DetectResult

    mock_client = models.Client(id=1, event_ids=[1])
    fake_storage = FakeStorageBackend()
    fake_storage.put("1/raw/session.mp4", b"synthetic video bytes")

    talk = models.Talk(
        id=1,
        event_id=1,
        title="E2E Pipeline Talk",
        room="Main Room",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="waiting_for_files",
        cut_start=None,
        cut_end=None,
        raw_duration_seconds=None,
    )
    jobs = {}

    mock_db_session = MagicMock()
    mock_db_session.query.return_value.filter.return_value.first.side_effect = lambda: (
        talk
    )
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()

    class WorkerDBContext:
        def __call__(self_inner):
            class Session:
                def __enter__(s):
                    mock_s = MagicMock()
                    mock_s.query.return_value.filter.return_value.first.return_value = (
                        talk
                    )
                    mock_s.get.side_effect = lambda model, obj_id: (
                        talk if model == models.Talk else jobs.get(obj_id)
                    )

                    def add_fn(obj):
                        if isinstance(obj, models.Job):
                            if obj.id is None:
                                obj.id = len(jobs) + 1
                            jobs[obj.id] = obj

                    mock_s.add.side_effect = add_fn
                    mock_s.commit = MagicMock()
                    mock_s.refresh = MagicMock()
                    return mock_s

                def __exit__(s, *args):
                    pass

            return Session()

    worker_db = WorkerDBContext()

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db_session
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    # 1. Verify talk initial state
    resp = client.get("/talks/1", headers={"X-API-Key": "key"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "waiting_for_files"

    # 2. POST /talks/1/recordings
    with (
        patch("app.routes.talks.stage_recording", return_value="1/raw/session.mp4"),
        patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_detect,
    ):
        resp = client.post(
            "/talks/1/recordings",
            json={"relative_key": "session.mp4"},
            headers={"X-API-Key": "key"},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "detecting"
        assert talk.status == "detecting"
        mock_enqueue_detect.assert_called_once_with(
            job_detect,
            1,
            "1/raw/session.mp4",
            job_timeout=300,
        )

    # 3. Simulate job_detect running in worker
    detect_result = DetectResult(
        passed=True,
        actual_duration_seconds=1800.0,
        has_video=True,
        has_audio=True,
        reason=None,
    )
    with (
        patch("app.tasks.SessionLocal", side_effect=worker_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.detect", return_value=detect_result),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_after_detect,
    ):
        job_detect(1, "1/raw/session.mp4")
        assert talk.status == "pending_approval"
        assert talk.raw_duration_seconds == 1800.0
        mock_enqueue_after_detect.assert_not_called()  # Halts at pending_approval gate

    # 4. POST /talks/1/approve -> pending_bounds (no job enqueue)
    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_approve:
        resp = client.post(
            "/talks/1/approve",
            headers={"X-API-Key": "key"},
        )
        assert resp.status_code == 200
        assert talk.status == "pending_bounds"
        mock_enqueue_approve.assert_not_called()

    # 5. POST /talks/1/cut -> cutting, enqueues job_cut
    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_cut:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:30:00"},
            headers={"X-API-Key": "key"},
        )
        assert resp.status_code == 202
        assert talk.status == "cutting"
        assert talk.cut_start == 10.0
        assert talk.cut_end == 1800.0
        mock_enqueue_cut.assert_called_once_with(
            job_cut,
            1,
            "1/raw/session.mp4",
            job_timeout=900,
        )

    # 6. Simulate job_cut running in worker
    def fake_cut(inp, out, start_s, end_s):
        Path(out).write_bytes(b"cut media")

    with (
        patch("app.tasks.SessionLocal", side_effect=worker_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_preview,
    ):
        job_cut(1, "1/raw/session.mp4")
        assert talk.status == "generating_previews"
        mock_enqueue_preview.assert_called_once_with(
            job_preview,
            1,
            "1/cut/cut.mp4",
            "1/preview/preview.mp4",
            job_timeout=1800,
        )

    # 7. Simulate job_preview running in worker
    def fake_preview(inp, out, preset):
        Path(out).write_bytes(b"preview media")

    with (
        patch("app.tasks.SessionLocal", side_effect=worker_db),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.generate_preview", side_effect=fake_preview),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_after_preview,
    ):
        job_preview(1, "1/cut/cut.mp4")
        assert talk.status == "preview"
        mock_enqueue_after_preview.assert_not_called()  # Halts at preview gate for human review

    # 10. Query GET /talks/1 and verify status is 'preview' and preview URL exists
    resp = client.get("/talks/1", headers={"X-API-Key": "key"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "preview"
    assert data["preview_urls"] == ["memory://1/preview/preview.mp4"]

    app.dependency_overrides.clear()
