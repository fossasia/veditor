from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models
from app.auth import CurrentUser, get_client, get_current_user, hash_api_key
from app.consts import TalkStatus
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

from .conftest import FakeStorageBackend, TestSessionLocal

client = TestClient(app)

API_KEY = "valid_key"


def _create_api_client(
    db: Session, event_ids: list[int], api_key: str = API_KEY
) -> models.Client:
    client_row = models.Client(hashed_key=hash_api_key(api_key), event_ids=event_ids)
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


def _create_event(
    db: Session, name: str = "Test Event", source: str | None = None
) -> models.Event:
    event = models.Event(name=name, source=source)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db: Session,
    event_id: int,
    *,
    title: str = "Test Talk",
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


def test_post_talks_unauthorized() -> None:
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


def test_post_talks_forbidden_event(db_session: Session) -> None:
    _create_api_client(db_session, event_ids=[2])

    response = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Introduction to FastAPI",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Client is not authorized to access this event"


def test_post_talks_create_success(db_session: Session) -> None:
    event = _create_event(db_session, "Keynote Event")
    _create_api_client(db_session, event_ids=[event.id])

    start_time = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    end_time = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)

    response = client.post(
        "/talks",
        json={
            "event_id": event.id,
            "title": "Keynote Address",
            "room": "Main Stage",
            "start": start_time.isoformat(),
            "end": end_time.isoformat(),
        },
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["event_id"] == event.id
    assert data["title"] == "Keynote Address"
    assert data["room"] == "Main Stage"
    assert data["status"] == "waiting_for_files"
    assert data["preview_urls"] == []

    created = db_session.scalar(
        select(models.Talk).where(models.Talk.title == "Keynote Address")
    )
    assert created is not None
    assert created.room == "Main Stage"


def test_post_talks_upsert_update_success(db_session: Session) -> None:
    event = _create_event(db_session, "Keynote Event")
    _create_api_client(db_session, event_ids=[event.id])

    start_time = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    initial_end = datetime(2026, 9, 1, 10, 45, tzinfo=UTC)
    updated_end = datetime(2026, 9, 1, 11, 0, tzinfo=UTC)

    existing_talk = _create_talk(
        db_session,
        event.id,
        title="Keynote Address",
        status="cutting",
    )
    existing_talk.end = initial_end
    db_session.commit()

    response = client.post(
        "/talks",
        json={
            "event_id": event.id,
            "title": "Keynote Address",
            "room": "Updated Room",
            "start": start_time.isoformat(),
            "end": updated_end.isoformat(),
        },
        headers={"X-API-Key": API_KEY},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == existing_talk.id
    assert data["room"] == "Updated Room"
    assert data["status"] == "cutting"

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, existing_talk.id)
    assert reloaded is not None
    assert reloaded.room == "Updated Room"
    assert reloaded.end == updated_end
    assert reloaded.status == "cutting"


def test_post_talks_concurrent_race_handled(db_session: Session) -> None:
    """Verify that a race condition on insert (IntegrityError) recovers and performs upsert.

    The commit failure is simulated with a mocked session; the upsert recovery
    path itself is exercised through the real route logic.
    """
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

    try:
        talk_lookups = {"count": 0}

        def scalar_side_effect(
            stmt: Any, *args: Any, **kwargs: Any
        ) -> models.Talk | models.Event | None:
            entity = stmt.column_descriptions[0]["entity"]
            if entity is models.Talk:
                talk_lookups["count"] += 1
                return None if talk_lookups["count"] == 1 else existing_talk
            if entity is models.Event:
                return models.Event(id=1, name="Concurrent Event")
            return None

        mock_db.scalar.side_effect = scalar_side_effect
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
                "speaker_email": "updated@speaker.com",
            },
            headers={"X-API-Key": API_KEY},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 20
        assert data["room"] == "Updated Concurrent Room"
        assert existing_talk.room == "Updated Concurrent Room"
        assert existing_talk.end == updated_end
        assert existing_talk.speaker_email == "updated@speaker.com"
        assert mock_db.rollback.called
    finally:
        app.dependency_overrides.clear()


def test_get_talk_unauthorized() -> None:
    response = client.get("/talks/1")
    assert response.status_code == 401


def test_get_talk_not_found(db_session: Session) -> None:
    event = _create_event(db_session)
    _create_api_client(db_session, event_ids=[event.id])

    response = client.get("/talks/999", headers={"X-API-Key": API_KEY})
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_get_talk_unauthorized_event_returns_404(db_session: Session) -> None:
    """Talk in unowned event must return 404 (not 403) to avoid leaking existence."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Secret Talk")
    _create_api_client(db_session, event_ids=[event.id + 100])

    response = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_get_talk_authorized_without_previews(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Public Talk")
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == talk.id
    assert data["title"] == "Public Talk"
    assert data["preview_urls"] == []


def test_get_talk_authorized_with_previews(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Preview Ready Talk", status="preview"
    )
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    fake_storage.put(f"{talk.id}/preview/small_video.mp4", b"dummy_small")
    fake_storage.put(f"{talk.id}/preview/big_video.mp4", b"dummy_big")

    response = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == talk.id
    assert data["status"] == "preview"
    assert len(data["preview_urls"]) == 2
    assert f"memory://{talk.id}/preview/small_video.mp4" in data["preview_urls"]
    assert f"memory://{talk.id}/preview/big_video.mp4" in data["preview_urls"]


# --- Tests for POST /talks/{id}/recordings ---


def test_post_recording_unauthorized() -> None:
    response = client.post(
        "/talks/1/recordings",
        json={"source_path": "/some/path/video.mp4"},
    )
    assert response.status_code == 401


def test_post_recording_forbidden_event_returns_404(db_session: Session) -> None:
    """Accessing talk outside caller's event_ids returns 404 to avoid leaking existence."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Other Event Talk")
    _create_api_client(db_session, event_ids=[event.id + 100])

    response = client.post(
        f"/talks/{talk.id}/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_post_recording_talk_not_found(db_session: Session) -> None:
    event = _create_event(db_session)
    _create_api_client(db_session, event_ids=[event.id])

    response = client.post(
        "/talks/999/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_post_recording_invalid_state_conflict(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Processing Talk", status="cutting")
    _create_api_client(db_session, event_ids=[event.id])

    response = client.post(
        f"/talks/{talk.id}/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 409
    assert "Cannot ingest recording" in response.json()["detail"]


def test_post_recording_ingest_path_rejected(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch(
        "app.routes.talks.stage_recording",
        side_effect=IngestPathRejectedError("Invalid path"),
    ):
        response = client.post(
            f"/talks/{talk.id}/recordings",
            json={"source_path": "/invalid/path.mp4"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid path"


def test_post_recording_insufficient_storage_returns_507(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """When disk space is insufficient, returns 507 Insufficient Storage and avoids state advance/job creation."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

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
            f"/talks/{talk.id}/recordings",
            json={"source_path": "/valid/path.mp4"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 507
        assert "Insufficient storage" in response.json()["detail"]
        assert "3000" in response.json()["detail"]
        assert "1000" in response.json()["detail"]

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.WAITING_FILES
        mock_enqueue.assert_not_called()


def test_post_recording_success_enqueues_detect(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    raw_key = f"{talk.id}/raw/video.mp4"
    with (
        patch("app.routes.talks.stage_recording", return_value=raw_key) as mock_stage,
        patch("app.routes.talks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{talk.id}/recordings",
            json={"relative_key": "video.mp4"},
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 202
        data = response.json()
        assert data["id"] == talk.id
        assert data["status"] == "detecting"

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.DETECTING

        mock_stage.assert_called_once()
        mock_enqueue.assert_called_once_with(
            job_detect,
            talk.id,
            raw_key,
            None,
            is_room_recording=False,
            job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
        )


def test_post_recording_duplicate_ingest_rejected_with_409(db_session: Session) -> None:
    """Duplicate POST /recordings when talk is already in detecting state returns 409 Conflict."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="detecting")
    _create_api_client(db_session, event_ids=[event.id])

    response = client.post(
        f"/talks/{talk.id}/recordings",
        json={"source_path": "/some/path/video.mp4"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 409
    assert (
        "Cannot ingest recording for talk in status 'detecting'"
        in response.json()["detail"]
    )


# --- Tests for POST /talks/{id}/approve ---


def test_post_approve_unauthorized() -> None:
    response = client.post("/talks/1/approve")
    assert response.status_code == 401


def test_post_approve_forbidden_event_returns_404(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Other Talk", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id + 100])

    response = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_post_approve_invalid_state_conflict(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk in Waiting", status="waiting_for_files"
    )
    _create_api_client(db_session, event_ids=[event.id])

    response = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
    assert response.status_code == 409
    assert (
        "Cannot approve/reject talk in status 'waiting_for_files'"
        in response.json()["detail"]
    )


def test_post_approve_no_raw_recording_fails(db_session: Session) -> None:
    """Approve no longer checks for raw recording existence; it just transitions to pending_bounds."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk to Approve", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    response = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
    # Approve no longer requires a raw file — just transitions to pending_intro_outro
    assert response.status_code == 200
    assert response.json()["status"] == "pending_intro_outro"


def test_post_approve_success_enqueues_cut(db_session: Session) -> None:
    """Approve now transitions to pending_intro_outro; handoff transitions to pending_bounds; job_cut is enqueued later from /cut."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk to Approve", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        response = client.post(
            f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == talk.id
        assert data["status"] == "pending_intro_outro"

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO
        # Approve no longer enqueues job_cut immediately
        mock_enqueue.assert_not_called()


def test_post_approve_with_custom_raw_key(db_session: Session) -> None:
    """Approve body no longer accepts raw_key; extra fields are ignored by Pydantic (extra=ignore) or result in 422.
    Since ApproveRequest has no raw_key field, sending it just uses the default approve decision."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk to Approve", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    # Old raw_key field is ignored — body parsed as ApproveRequest(decision="approve")
    response = client.post(
        f"/talks/{talk.id}/approve",
        json={"decision": "approve"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "pending_intro_outro"


def test_post_approve_with_mismatched_talk_raw_key_rejected(
    db_session: Session,
) -> None:
    """Old raw_key validation is gone. Sending decision=reject now terminates the talk and cleans up intermediates."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk to Reject", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    response = client.post(
        f"/talks/{talk.id}/approve",
        json={"decision": "reject"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert mock_storage.delete.call_count == 5
    for stage in (
        f"{talk.id}/cut",
        f"{talk.id}/preview",
        f"{talk.id}/assemble",
        f"{talk.id}/intro",
        f"{talk.id}/outro",
    ):
        mock_storage.delete.assert_any_call(stage)


def test_post_approve_reject_storage_delete_resilient(db_session: Session) -> None:
    """Storage deletion failure on reject does not block transition into rejected."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Talk to Reject", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = RuntimeError("Storage connection failed")
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    response = client.post(
        f"/talks/{talk.id}/approve",
        json={"decision": "reject"},
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert mock_storage.delete.call_count == 5


# --- POST /talks/{id}/cut — time-format validation regression tests ---


def _create_pending_bounds_talk(db: Session, event_id: int) -> models.Talk:
    """Create a talk in pending_bounds state with a known raw_duration."""
    return _create_talk(
        db,
        event_id,
        title="Talk for Cut",
        status="pending_bounds",
        raw_duration_seconds=7200.0,  # 2-hour recording
    )


@pytest.mark.parametrize(
    "cut_start,cut_end",
    [
        ("24:00:00", "25:00:00"),  # HH == 24 — the reported silent-coercion bug
        ("25:00:00", "26:00:00"),  # HH > 24
        ("00:60:00", "01:00:00"),  # MM == 60
        ("00:00:60", "00:01:00"),  # SS == 60
        ("99:99:99", "99:99:99"),  # everything out of range
        ("00:00:00\n", "00:01:00"),  # trailing newline rejected
    ],
)
def test_submit_cut_bounds_out_of_range_returns_422(
    db_session: Session, cut_start: str, cut_end: str
) -> None:
    """Regression: out-of-range time strings must yield 422, not be silently coerced."""
    event = _create_event(db_session)
    talk = _create_pending_bounds_talk(db_session, event.id)
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": cut_start, "cut_end": cut_end},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_submit_cut_bounds_needs_work_accepted(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """POST /talks/{id}/cut succeeds when talk is in needs_work status."""
    event = _create_event(db_session)
    talk = _create_pending_bounds_talk(db_session, event.id)
    talk.status = TalkStatus.NEEDS_WORK
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    fake_storage.put(f"{talk.id}/raw/video.mp4", b"raw video")

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:01:10"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 202

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "cutting"
        mock_enqueue.assert_called_once()


# --- Full Path Test: recordings -> detect -> pending_approval -> approve -> cut -> preview -> preview halt ---


def test_patch_talk_unauthorized() -> None:
    resp = client.patch("/talks/1", json={"title": "New Title"})
    assert resp.status_code == 401


def test_patch_talk_not_found(db_session: Session) -> None:
    event = _create_event(db_session)
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        "/talks/9999",
        json={"title": "New Title"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 404


def test_patch_talk_updates_fields(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"title": "New Title", "room": "New Room"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, talk.id)
    assert reloaded is not None
    assert reloaded.title == "New Title"
    assert reloaded.room == "New Room"


def test_patch_talk_empty_title_rejected(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"title": "   "},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "cannot be empty" in resp.json()["detail"]


def test_patch_talk_end_before_start_rejected(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    end_time = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)  # 9:00 is before 10:00 start

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"end": end_time.isoformat()},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "after start time" in resp.json()["detail"]


def test_patch_talk_null_start_rejected(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"start": None},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "start time cannot be empty" in resp.json()["detail"]


def test_patch_talk_null_end_rejected(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"end": None},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "end time cannot be empty" in resp.json()["detail"]


def test_patch_talk_room_normalized_to_none(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"room": "   "},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 200

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.room is None


def test_patch_talk_naive_datetime_comparison_normalized(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Old Title")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.patch(
        f"/talks/{talk.id}",
        json={"end": "2026-09-01T09:00:00"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "after start time" in resp.json()["detail"]

    resp_valid = client.patch(
        f"/talks/{talk.id}",
        json={"end": "2026-09-01T12:00:00"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp_valid.status_code == 200

    db_session.expire_all()
    reloaded = db_session.get(models.Talk, talk.id)
    assert reloaded is not None
    assert reloaded.end == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_full_pipeline_flow_recordings_to_preview_halt(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
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

    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="E2E Pipeline Talk")
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    raw_key = f"{talk.id}/raw/session.mp4"
    fake_storage.put(raw_key, b"synthetic video bytes")
    worker_sessions = _worker_session_factory(db_session)

    # 1. Verify talk initial state
    resp = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    assert resp.json()["status"] == "waiting_for_files"

    # 2. POST /talks/{id}/recordings
    with (
        patch("app.routes.talks.stage_recording", return_value=raw_key),
        patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_detect,
    ):
        resp = client.post(
            f"/talks/{talk.id}/recordings",
            json={"relative_key": "session.mp4"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 202
        assert resp.json()["status"] == "detecting"

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.DETECTING
        mock_enqueue_detect.assert_called_once_with(
            job_detect,
            talk.id,
            raw_key,
            None,
            is_room_recording=False,
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
        patch("app.tasks.SessionLocal", side_effect=worker_sessions),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.detect", return_value=detect_result),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_after_detect,
    ):
        job_detect(talk.id, raw_key)

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_approval"
        assert reloaded.raw_duration_seconds == 1800.0
        mock_enqueue_after_detect.assert_not_called()  # Halts at pending_approval gate

    # 4. POST /talks/{id}/approve -> pending_intro_outro (no job enqueue)
    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_approve:
        resp = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO
        mock_enqueue_approve.assert_not_called()

    # 4b. POST /talks/{id}/handoff -> pending_bounds
    resp_handoff = client.post(
        f"/talks/{talk.id}/handoff",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": API_KEY},
    )
    assert resp_handoff.status_code == 200

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_BOUNDS

    # 5. POST /talks/{id}/cut -> cutting, enqueues job_cut
    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue_cut:
        resp = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:30:00"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 202

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "cutting"
        assert reloaded.cut_start == 10.0
        assert reloaded.cut_end == 1800.0
        mock_enqueue_cut.assert_called_once_with(
            job_cut,
            talk.id,
            raw_key,
            job_timeout=900,
        )

    # 6. Simulate job_cut running in worker
    def fake_cut(
        inp: Path | str, out: Path | str, start_s: float, end_s: float
    ) -> None:
        Path(out).write_bytes(b"cut media")

    with (
        patch("app.tasks.SessionLocal", side_effect=worker_sessions),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.cut", side_effect=fake_cut),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_preview,
    ):
        job_cut(talk.id, raw_key)

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.GENERATING_PREVIEWS
        mock_enqueue_preview.assert_called_once_with(
            job_preview,
            talk.id,
            f"{talk.id}/cut/cut.mp4",
            f"{talk.id}/preview/preview.mp4",
            job_timeout=1800,
        )

    # 7. Simulate job_preview running in worker
    def fake_preview(
        inp: Path | str, out: Path | str, preset: str, *args: Any, **kwargs: Any
    ) -> None:
        Path(out).write_bytes(b"preview media")

    with (
        patch("app.tasks.SessionLocal", side_effect=worker_sessions),
        patch("app.tasks.get_storage_backend", return_value=fake_storage),
        patch("app.tasks.generate_preview", side_effect=fake_preview),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue_after_preview,
    ):
        job_preview(talk.id, f"{talk.id}/cut/cut.mp4")

        db_session.expire_all()
        reloaded_talk = db_session.get(models.Talk, talk.id)
        assert reloaded_talk is not None
        assert reloaded_talk.status == TalkStatus.PREVIEW
        mock_enqueue_after_preview.assert_not_called()  # Halts at preview gate for human review

    # 10. Query GET /talks/{id} and verify status is 'preview' and preview URL exists
    resp = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "preview"
    assert data["preview_urls"] == [f"memory://{talk.id}/preview/preview.mp4"]


def test_post_recording_with_recording_start_enqueues_detect(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    from app.pipeline.detect import DetectResult

    event = _create_event(db_session)
    talk_start = datetime(2026, 9, 26, 10, 15, tzinfo=UTC)
    talk_end = datetime(2026, 9, 26, 10, 45, tzinfo=UTC)
    talk = _create_talk(db_session, event.id, title="Test Talk")
    talk.start = talk_start
    talk.end = talk_end
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    raw_key = f"{talk.id}/raw/video.mp4"
    with (
        patch("app.routes.talks.stage_recording", return_value=raw_key) as mock_stage,
        patch("app.routes.talks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{talk.id}/recordings",
            json={
                "relative_key": "video.mp4",
                "recording_start": "2026-09-26T10:00:00Z",
            },
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 202

        expected_dt = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
        mock_stage.assert_called_once()
        mock_enqueue.assert_called_once_with(
            job_detect,
            talk.id,
            raw_key,
            expected_dt,
            is_room_recording=True,
            job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
        )

        # Verify forwarded intent and resulting detection behavior for a room recording
        # longer than the scheduled talk (7200s room recording vs 1800s scheduled talk).
        enqueued_call = mock_enqueue.call_args
        target_func = enqueued_call.args[0]
        pos_args = enqueued_call.args[1:]
        kw_args = {k: v for k, v in enqueued_call.kwargs.items() if k != "job_timeout"}
        assert kw_args.get("is_room_recording") is True

        worker_sessions = _worker_session_factory(db_session)
        fake_storage.put(raw_key, b"fake_video_content")
        room_rec_duration = 7200.0  # 2 hours
        detect_result = DetectResult(
            passed=True,
            actual_duration_seconds=room_rec_duration,
            has_video=True,
            has_audio=True,
            reason=None,
        )

        with (
            patch("app.tasks.SessionLocal", side_effect=worker_sessions),
            patch("app.tasks.get_storage_backend", return_value=fake_storage),
            patch("app.tasks.detect", return_value=detect_result) as mock_detect_fn,
        ):
            target_func(*pos_args, **kw_args)

            mock_detect_fn.assert_called_once()
            detect_call_args, detect_call_kwargs = mock_detect_fn.call_args
            assert detect_call_args[0].key == raw_key
            assert detect_call_kwargs["scheduled_start"] == talk_start
            assert detect_call_kwargs["scheduled_end"] == talk_end
            assert detect_call_kwargs["tolerance_seconds"] == float("inf")
            assert detect_call_kwargs["is_room_recording"] is True
            # Ensure the talk is not marked broken and instead advances with pre-seeded cut bounds
            db_session.expire_all()
            reloaded = db_session.get(models.Talk, talk.id)
            assert reloaded is not None
            assert reloaded.status == "pending_approval"
            assert reloaded.raw_duration_seconds == room_rec_duration
            assert reloaded.cut_start == pytest.approx(
                900.0
            )  # 10:15 - 10:00 = 15m (900s)
            assert reloaded.cut_end == pytest.approx(
                2700.0
            )  # 10:45 - 10:00 = 45m (2700s)


def test_handoff_with_speaker_email_enqueues_email(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        title="Keynote",
        status="pending_intro_outro",
    )
    talk.speaker_email = None
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        response = client.post(
            f"/talks/{talk.id}/handoff",
            json={
                "include_intro": False,
                "include_outro": False,
                "speaker_email": "speaker@example.com",
            },
            headers={"X-API-Key": API_KEY},
        )
        assert response.status_code == 200

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.speaker_email == "speaker@example.com"

        from app.tasks import job_send_speaker_handoff_email

        enqueued_jobs = [c[0][0] for c in mock_enqueue.call_args_list]
        assert job_send_speaker_handoff_email in enqueued_jobs


def test_handoff_resend_cooldown_and_missing_speaker(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        title="Session",
        status="pending_bounds",
    )
    talk.speaker_email = None
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    # Talk in pending_bounds without speaker_email fails to resend
    resp = client.post(
        f"/talks/{talk.id}/handoff",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "speaker_email is required" in resp.json()["detail"]

    # Talk with speaker_email but within 60s cooldown fails with 429
    talk.speaker_email = "speaker@example.com"
    talk.handed_off_at = datetime.now(UTC) - timedelta(seconds=10)
    db_session.commit()

    resp = client.post(
        f"/talks/{talk.id}/handoff",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 429
    assert "please wait before resending" in resp.json()["detail"]

    # Talk with speaker_email past cooldown succeeds (200)
    talk.handed_off_at = datetime.now(UTC) - timedelta(seconds=70)
    db_session.commit()
    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        mock_enqueue.assert_called_once()


def test_handoff_eventyay_synced_talk_does_not_send_mail(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Handoff for an Eventyay-synced talk advances to pending_bounds without sending email."""
    event = _create_event(db_session, "Eventyay Conf", source="eventyay")
    talk = _create_talk(
        db_session,
        event.id,
        title="Eventyay Synced Keynote",
        status="pending_intro_outro",
    )
    talk.speaker_email = "speaker@example.com"
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={
                "include_intro": False,
                "include_outro": False,
                "speaker_email": "speaker@example.com",
            },
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.handed_off_at is None

        from app.tasks import job_send_speaker_handoff_email

        enqueued_jobs = [c[0][0] for c in mock_enqueue.call_args_list]
        assert job_send_speaker_handoff_email not in enqueued_jobs


def test_handoff_sso_session_does_not_send_mail(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """When an organizer acts via an SSO session, handoff email is not sent."""
    from app.security import create_sso_token

    event = _create_event(db_session, "SSO Event")
    talk = _create_talk(
        db_session,
        event.id,
        title="SSO Keynote",
        status="pending_intro_outro",
    )
    talk.speaker_email = "speaker@example.com"
    db_session.commit()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    token = create_sso_token(
        scope_type="event",
        scope_id=event.id,
        role="organizer",
        email="organizer@eventyay.com",
    )

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={"include_intro": False, "include_outro": False},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.handed_off_at is None

        from app.tasks import job_send_speaker_handoff_email

        enqueued_jobs = [c[0][0] for c in mock_enqueue.call_args_list]
        assert job_send_speaker_handoff_email not in enqueued_jobs


def test_handoff_eventyay_talk_resend_email_rejected(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Resending handoff email on an Eventyay-synced talk is rejected with 400."""
    event = _create_event(db_session, "Eventyay Conf", source="eventyay")
    talk = _create_talk(
        db_session,
        event.id,
        title="Eventyay Session",
        status="pending_bounds",
    )
    talk.speaker_email = "speaker@example.com"
    db_session.commit()
    _create_api_client(db_session, event_ids=[event.id])
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    resp = client.post(
        f"/talks/{talk.id}/handoff",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 400
    assert "Eventyay" in resp.json()["detail"]


def test_job_send_speaker_handoff_email_skips_eventyay_talk(
    db_session: Session,
) -> None:
    """job_send_speaker_handoff_email cleanly skips if talk event is synced from Eventyay."""
    from app.tasks import job_send_speaker_handoff_email

    event = _create_event(db_session, "Eventyay Conf", source="eventyay")
    talk = _create_talk(db_session, event.id, title="Talk Title")
    talk.speaker_email = "speaker@example.com"
    db_session.commit()

    worker_sessions = _worker_session_factory(db_session)

    with (
        patch("app.tasks.SessionLocal", side_effect=worker_sessions),
        patch("app.email.send_speaker_handoff_email") as mock_send,
    ):
        result = job_send_speaker_handoff_email(talk.id)
        assert result is True
        mock_send.assert_not_called()


def test_is_eventyay_synced_exact_matching() -> None:
    """Event.is_eventyay_synced must match managed sources ('eventyay', 'platform') exactly."""
    assert models.Event(source="eventyay").is_eventyay_synced is True
    assert models.Event(source="EVENTYAY ").is_eventyay_synced is True
    assert models.Event(source="platform").is_eventyay_synced is True
    assert models.Event(source="classroom_sso").is_eventyay_synced is False
    assert models.Event(source="other").is_eventyay_synced is False
    assert models.Event(source=None).is_eventyay_synced is False


def test_handoff_platform_user_on_regular_event_sends_email(
    db_session: Session, fake_storage: FakeStorageBackend
) -> None:
    """Platform API user handing off a talk in a regular event sends email."""
    organizer = models.User(
        email="platform-organizer@example.com",
        hashed_password="x",
        role="organizer",
        is_active=True,
    )
    db_session.add(organizer)
    db_session.commit()

    event = _create_event(db_session, "Regular Conf")
    talk = _create_talk(
        db_session,
        event.id,
        title="Regular Talk",
        status="pending_intro_outro",
    )
    talk.speaker_email = "speaker@example.com"
    db_session.commit()

    user = CurrentUser(
        user_id=organizer.id,
        role="organizer",
        source="api_key",
        is_platform=True,
        event_ids=[event.id],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={"include_intro": False, "include_outro": False},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        reloaded = db_session.get(models.Talk, talk.id)
        assert reloaded is not None
        assert reloaded.status == "pending_bounds"
        assert reloaded.handed_off_at is not None

        from app.tasks import job_send_speaker_handoff_email

        enqueued_jobs = [c[0][0] for c in mock_enqueue.call_args_list]
        assert job_send_speaker_handoff_email in enqueued_jobs
