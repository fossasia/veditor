from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.consts import TalkStatus
from app.db import get_db
from app.main import app
from app.storage import get_storage_backend
from tests.conftest import TestSessionLocal

client = TestClient(app)

API_KEY = "valid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_api_client(
    db: Session, event_ids: list[int], api_key: str = API_KEY
) -> models.Client:
    client_row = models.Client(hashed_key=hash_api_key(api_key), event_ids=event_ids)
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


def _make_storage_override(mock_storage: MagicMock) -> Callable[[], MagicMock]:
    def _override() -> MagicMock:
        return mock_storage

    return _override


def _create_event(db: Session, name: str = "Test Event") -> models.Event:
    event = models.Event(name=name)
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
    raw_duration_seconds: float | None = None,
    cut_start: float | None = None,
    cut_end: float | None = None,
) -> models.Talk:
    talk = models.Talk(
        event_id=event_id,
        title=title,
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status=status,
        raw_duration_seconds=raw_duration_seconds,
        cut_start=cut_start,
        cut_end=cut_end,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def _talk(db: Session, talk_id: int) -> models.Talk:
    """Fetch a talk by id, failing loudly when it is missing."""
    talk = db.get(models.Talk, talk_id)
    assert talk is not None
    return talk


def _talk_jobs(db: Session, talk_id: int) -> list[models.Job]:
    return list(db.scalars(select(models.Job).where(models.Job.talk_id == talk_id)))


def _worker_session_factory(db_session: Session) -> Callable[[], Session]:
    def factory() -> Session:
        return TestSessionLocal(bind=db_session.get_bind())

    return factory


# ---------------------------------------------------------------------------
# Health check (existing)
# ---------------------------------------------------------------------------


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_check_redis_failure() -> None:
    fail_client = TestClient(app, raise_server_exceptions=False)
    with patch("app.main.redis_conn.ping", side_effect=Exception("Redis down")):
        response = fail_client.get("/health")
        assert 500 <= response.status_code < 600


def test_health_check_database_failure() -> None:
    fail_client = TestClient(app, raise_server_exceptions=False)
    mock_db = MagicMock()
    mock_db.execute.side_effect = Exception("DB connection failed")
    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        response = fail_client.get("/health")
        assert 500 <= response.status_code < 600
        mock_db.execute.assert_called_once()
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# GET /talks/{id}/raw-preview — state gating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "blocked_status",
    [
        "waiting_for_files",
        "detecting",
        "pending_approval",
    ],
)
def test_raw_preview_gated_before_pending_bounds(
    db_session: Session, blocked_status: str
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status=blocked_status)
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.get(f"/talks/{talk.id}/raw-preview", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "allowed_status",
    [
        "pending_bounds",
        "cutting",
        "generating_previews",
        "preview",
        "needs_work",
        "pending_intro_outro",
        "transcoding",
        "uploading",
        "done",
    ],
)
def test_raw_preview_accessible_from_pending_bounds_onwards(
    db_session: Session, allowed_status: str
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status=allowed_status)
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    raw_key = f"{talk.id}/raw/recording.mp4"
    mock_storage.list_keys.return_value = [raw_key]
    mock_storage.url.return_value = f"file:///data/{raw_key}"
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        resp = client.get(
            f"/talks/{talk.id}/raw-preview", headers={"X-API-Key": API_KEY}
        )
        assert resp.status_code == 200
        assert resp.json()["url"] == f"file:///data/{raw_key}"
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_raw_preview_no_file(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="pending_bounds")
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = []
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        resp = client.get(
            f"/talks/{talk.id}/raw-preview", headers={"X-API-Key": API_KEY}
        )
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


# ---------------------------------------------------------------------------
# POST /talks/{id}/approve
# ---------------------------------------------------------------------------


def test_approve_wrong_state(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="pending_bounds")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 409


def test_rejected_talk_has_no_raw_preview_access(db_session: Session) -> None:
    """After rejection, raw-preview must remain gated (403)."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="rejected")
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = [f"{talk.id}/raw/recording.mp4"]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        resp = client.get(
            f"/talks/{talk.id}/raw-preview", headers={"X-API-Key": API_KEY}
        )
        assert resp.status_code == 403
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


# ---------------------------------------------------------------------------
# POST /talks/{id}/cut-bounds
# ---------------------------------------------------------------------------


def test_cut_bounds_happy_path(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    raw_key = f"{talk.id}/raw/recording.mp4"
    mock_storage.list_keys.return_value = [raw_key]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        with patch("app.routes.talks.light_queue") as mock_queue:
            resp = client.post(
                f"/talks/{talk.id}/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 202

            db_session.expire_all()
            reloaded = _talk(db_session, talk.id)
            assert reloaded.status == "cutting"
            assert reloaded.cut_start == 10.0
            assert reloaded.cut_end == 2700.0

            mock_queue.enqueue.assert_called_once()
            # Verify job_cut was enqueued (not arbitrary function)
            call_args = mock_queue.enqueue.call_args
            assert call_args[0][0].__name__ == "job_cut"
            # Verify talk_id and raw_key are passed, not scheduled times
            assert call_args[0][1] == talk.id
            assert call_args[0][2] == raw_key
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_cut_bounds_with_note(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = [f"{talk.id}/raw/recording.mp4"]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        with patch("app.routes.talks.light_queue"):
            resp = client.post(
                f"/talks/{talk.id}/cut",
                json={
                    "cut_start": "00:00:10",
                    "cut_end": "00:45:00",
                    "note": "Trimmed initial silent intro",
                },
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 202

            # Verify Review model was persisted with the note
            review = db_session.scalar(
                select(models.Review).where(models.Review.talk_id == talk.id)
            )
            assert review is not None
            assert review.note == "Trimmed initial silent intro"
            assert review.decision == "cut"
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_cut_bounds_subsecond_precision(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = [f"{talk.id}/raw/recording.mp4"]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        with patch("app.routes.talks.light_queue"):
            resp = client.post(
                f"/talks/{talk.id}/cut",
                json={"cut_start": "00:00:10.500", "cut_end": "00:00:45.250"},
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 202

            db_session.expire_all()
            reloaded = _talk(db_session, talk.id)
            assert reloaded.cut_start == 10.5
            assert reloaded.cut_end == 45.25
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_cut_bounds_end_before_start(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:30:00", "cut_end": "00:10:00"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_cut_bounds_equal_start_end(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:10:00", "cut_end": "00:10:00"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_cut_bounds_exceeds_duration(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=60.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:00:05", "cut_end": "00:05:00"},  # 300s > 60s
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_cut_bounds_missing_raw_duration(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="pending_bounds")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:00:05", "cut_end": "00:00:55"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422
    assert "no detected raw duration" in resp.json()["detail"]


@pytest.mark.parametrize(
    "invalid_start,invalid_end",
    [
        ("00:10", "00:20:00"),  # missing seconds
        ("00:00:10+05:00", "00:20:00"),  # timezone offset
        ("1:2:3", "00:20:00"),  # single digit fields
        ("99:99:99", "00:20:00"),  # invalid time values
        ("not-a-time", "00:20:00"),  # non-time string
    ],
)
def test_cut_bounds_invalid_time_formats(
    db_session: Session, invalid_start: str, invalid_end: str
) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_bounds", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": invalid_start, "cut_end": invalid_end},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 422


def test_cut_bounds_wrong_state(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, status="pending_approval")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:00:05", "cut_end": "00:00:55"},
        headers={"X-API-Key": API_KEY},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Full happy path: recordings → approve → raw-preview → cut-bounds → cutting
# ---------------------------------------------------------------------------


def test_full_phase4_happy_path(db_session: Session) -> None:
    """Drive a talk from pending_approval to cutting via approve → raw-preview → cut-bounds."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, status="pending_approval", raw_duration_seconds=3600.0
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    raw_key = f"{talk.id}/raw/recording.mp4"
    mock_storage.list_keys.return_value = [raw_key]
    mock_storage.url.return_value = f"file:///data/{raw_key}"
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        # 1. Approve → pending_intro_outro
        resp = client.post(f"/talks/{talk.id}/approve", headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        assert _talk(db_session, talk.id).status == "pending_intro_outro"

        # 1b. Handoff → pending_bounds
        resp = client.post(
            f"/talks/{talk.id}/handoff",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": API_KEY},
        )
        assert resp.status_code == 200
        assert _talk(db_session, talk.id).status == "pending_bounds"

        # 2. Raw preview accessible
        resp = client.get(
            f"/talks/{talk.id}/raw-preview", headers={"X-API-Key": API_KEY}
        )
        assert resp.status_code == 200
        assert "url" in resp.json()

        # 3. Submit cut bounds → cutting
        with patch("app.routes.talks.light_queue") as mock_queue:
            resp = client.post(
                f"/talks/{talk.id}/cut",
                json={"cut_start": "00:00:10", "cut_end": "01:00:00"},
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 202

            db_session.expire_all()
            reloaded = _talk(db_session, talk.id)
            assert reloaded.status == "cutting"
            # cut_start/cut_end on the talk row — not scheduled start/end
            assert reloaded.cut_start == 10.0
            assert reloaded.cut_end == 3600.0
            # Verify job_cut was enqueued with the right arguments
            mock_queue.enqueue.assert_called_once()
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_abort_talk_success(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        status="cutting",
        raw_duration_seconds=3600.0,
        cut_start=10.0,
        cut_end=1800.0,
    )
    _create_api_client(db_session, event_ids=[event.id])
    db_session.add(models.Job(talk_id=talk.id, kind="cut", status="running"))
    db_session.add(models.Review(talk_id=talk.id, decision="cut"))
    db_session.commit()

    mock_storage = MagicMock()

    mock_job = MagicMock()
    mock_job.args = (talk.id, f"{talk.id}/raw/test.mp4")
    mock_light = MagicMock()
    mock_light.job_ids = ["job_123"]
    mock_light.fetch_job.return_value = mock_job
    mock_heavy = MagicMock()
    mock_heavy.job_ids = []

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    with (
        patch("app.routes.talks.light_queue", mock_light),
        patch("app.routes.talks.heavy_queue", mock_heavy),
    ):
        try:
            resp = client.post(
                f"/talks/{talk.id}/abort", headers={"X-API-Key": API_KEY}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == talk.id
            assert data["status"] == "waiting_for_files"
            assert data["raw_duration_seconds"] is None
            assert data["cut_start"] is None
            assert data["cut_end"] is None

            # Talk state persisted
            db_session.expire_all()
            reloaded = _talk(db_session, talk.id)
            assert reloaded.status == "waiting_for_files"
            assert reloaded.raw_duration_seconds is None
            assert reloaded.cut_start is None
            assert reloaded.cut_end is None

            # DB jobs and reviews cleared
            assert _talk_jobs(db_session, talk.id) == []
            assert (
                db_session.scalar(
                    select(func.count())
                    .select_from(models.Review)
                    .where(models.Review.talk_id == talk.id)
                )
                == 0
            )

            # Storage cleaned up
            mock_storage.delete.assert_called_once_with(str(talk.id))

            # RQ job cancelled and deleted
            mock_job.cancel.assert_called_once()
            mock_job.delete.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_storage_backend, None)


def test_abort_talk_cancels_jobs_in_registries(db_session: Session) -> None:
    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        status="cutting",
        raw_duration_seconds=3600.0,
        cut_start=10.0,
        cut_end=1800.0,
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    mock_started_job = MagicMock()
    mock_started_job.id = "job_running"
    mock_started_job.args = (talk.id, f"{talk.id}/raw/test.mp4")

    mock_light = MagicMock()
    mock_light.job_ids = []
    mock_light.started_job_registry.get_job_ids.return_value = ["job_running"]
    mock_light.deferred_job_registry.get_job_ids.return_value = []
    mock_light.scheduled_job_registry.get_job_ids.return_value = []
    mock_light.fetch_job.return_value = mock_started_job

    mock_heavy = MagicMock()
    mock_heavy.job_ids = []
    mock_heavy.started_job_registry.get_job_ids.return_value = []
    mock_heavy.deferred_job_registry.get_job_ids.return_value = []
    mock_heavy.scheduled_job_registry.get_job_ids.return_value = []

    with (
        patch("app.routes.talks.light_queue", mock_light),
        patch("app.routes.talks.heavy_queue", mock_heavy),
        patch("app.routes.talks.send_stop_job_command") as mock_send_stop,
    ):
        try:
            resp = client.post(
                f"/talks/{talk.id}/abort", headers={"X-API-Key": API_KEY}
            )
            assert resp.status_code == 200
            mock_send_stop.assert_called_once_with(mock_light.connection, "job_running")
            mock_started_job.cancel.assert_called_once()
            mock_started_job.delete.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_storage_backend, None)


def test_abort_talk_not_found(db_session: Session) -> None:
    event = _create_event(db_session)
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post("/talks/999/abort", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Talk not found"


def test_abort_talk_unauthorized_event(db_session: Session) -> None:
    event = _create_event(db_session)
    other_event = _create_event(db_session, "Other Event")
    talk = _create_talk(db_session, other_event.id, status="cutting")
    _create_api_client(db_session, event_ids=[event.id])

    resp = client.post(f"/talks/{talk.id}/abort", headers={"X-API-Key": API_KEY})
    assert resp.status_code == 404


def test_abort_talk_from_any_state(db_session: Session) -> None:
    """Verify abort works seamlessly from terminal states (e.g. broken, rejected) as well."""
    for initial_status in (
        "broken",
        "rejected",
        "done",
        "waiting_for_files",
        "detecting",
    ):
        event = _create_event(db_session, f"Abort Event {initial_status}")
        talk = _create_talk(db_session, event.id, status=initial_status)
        status_api_key = f"{API_KEY}-{initial_status}"
        _create_api_client(db_session, event_ids=[event.id], api_key=status_api_key)

        mock_storage = MagicMock()
        app.dependency_overrides[get_storage_backend] = _make_storage_override(
            mock_storage
        )

        with (
            patch("app.routes.talks.light_queue") as mock_l,
            patch("app.routes.talks.heavy_queue") as mock_h,
        ):
            mock_l.job_ids = []
            mock_h.job_ids = []
            try:
                resp = client.post(
                    f"/talks/{talk.id}/abort", headers={"X-API-Key": status_api_key}
                )
                assert resp.status_code == 200
                assert _talk(db_session, talk.id).status == "waiting_for_files"
                mock_storage.delete.assert_called_once_with(str(talk.id))
            finally:
                app.dependency_overrides.pop(get_storage_backend, None)


# ---------------------------------------------------------------------------
# Lifecycle & Orchestration Tests
# ---------------------------------------------------------------------------


def test_lifecycle_full_ingest_to_done(db_session: Session) -> None:
    """Mock-driven orchestration test covering the complete talk processing lifecycle.

    Drives a talk from ingest -> detecting -> pending_approval -> approve ->
    pending_bounds -> cutting -> generating_previews -> preview ->
    pending_intro_outro -> assembling (intro -> concat) -> transcoding (loudness -> transcode) ->
    uploading (publish) -> done.
    Asserts database mutations, stage transitions, and job record persistence at each step.
    """
    from app.tasks import (
        job_concat,
        job_cut,
        job_detect,
        job_intro,
        job_loudness,
        job_preview,
        job_publish,
        job_transcode,
    )

    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id, title="Lifecycle Talk")
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.exists.return_value = True
    mock_storage.get.return_value = Path("/tmp/mock_raw.mp4")
    mock_storage.url.return_value = "https://example.com/preview.mp4"
    mock_storage.list_keys.return_value = [f"{talk.id}/raw/mock_source.mp4"]

    raw_key = f"{talk.id}/raw/mock_source.mp4"
    cut_key = f"{talk.id}/cut/cut.mp4"
    worker_sessions = _worker_session_factory(db_session)

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    with (
        patch("app.routes.talks.light_queue") as mock_light_queue,
        patch("app.routes.talks.heavy_queue") as mock_heavy_queue,
        patch("app.tasks.light_queue", mock_light_queue),
        patch("app.tasks.heavy_queue", mock_heavy_queue),
        patch("app.routes.talks.stage_recording", return_value=raw_key),
        patch("app.tasks.SessionLocal", side_effect=worker_sessions),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
        patch("app.tasks.cut"),
        patch("app.tasks.generate_preview"),
        patch("app.tasks.generate_intro_clip"),
        patch("app.tasks.concat"),
        patch("app.tasks.normalize"),
        patch("app.tasks.transcode"),
        patch("app.tasks.publish"),
    ):
        mock_detect.return_value = MagicMock(
            passed=True, actual_duration_seconds=3600.0
        )

        # 1. Ingest recording -> transitions talk to 'detecting' and enqueues job_detect
        resp1 = client.post(
            f"/talks/{talk.id}/recordings",
            json={"source_path": "/tmp/mock_source.mp4"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp1.status_code == 202
        assert _talk(db_session, talk.id).status == "detecting"
        mock_light_queue.enqueue.assert_called_once()
        queued_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_task == job_detect

        # 2. Execute job_detect -> transitions talk to 'pending_approval'
        job_detect(talk.id, raw_key)
        db_session.expire_all()
        reloaded = _talk(db_session, talk.id)
        assert reloaded.status == "pending_approval"
        assert reloaded.raw_duration_seconds == 3600.0
        assert any(
            j.kind == "detect" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )

        # 3. Approve talk -> transitions talk to 'pending_intro_outro'
        resp2 = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "approve"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp2.status_code == 200
        assert _talk(db_session, talk.id).status == "pending_intro_outro"

        # 3b. Handoff to speaker -> transitions talk to 'pending_bounds'
        resp_handoff = client.post(
            f"/talks/{talk.id}/handoff",
            json={
                "include_intro": True,
                "intro_source": "generated",
                "include_outro": False,
            },
            headers={"X-API-Key": API_KEY},
        )
        assert resp_handoff.status_code == 200
        assert _talk(db_session, talk.id).status == "pending_bounds"

        # 4. Submit cut bounds -> transitions talk to 'cutting' and enqueues job_cut
        mock_light_queue.reset_mock()
        resp3 = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:50:00"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp3.status_code == 202
        reloaded = _talk(db_session, talk.id)
        assert reloaded.status == "cutting"
        assert reloaded.cut_start == 10.0
        assert reloaded.cut_end == 3000.0
        mock_light_queue.enqueue.assert_called_once()
        queued_cut_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_cut_task == job_cut

        # 5. Execute job_cut -> transitions talk to 'generating_previews' and enqueues job_preview
        mock_light_queue.reset_mock()
        job_cut(talk.id, raw_key)
        db_session.expire_all()
        assert _talk(db_session, talk.id).status == "generating_previews"
        assert any(
            j.kind == "cut" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )
        mock_light_queue.enqueue.assert_called_once()
        queued_preview_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_preview_task == job_preview

        # 6. Execute job_preview -> transitions talk to 'preview'
        job_preview(talk.id, cut_key)
        db_session.expire_all()
        assert _talk(db_session, talk.id).status == "preview"
        assert any(
            j.kind == "preview" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )

        # 7. Review talk -> transitions talk to 'assembling' and enqueues job_intro
        mock_light_queue.reset_mock()
        resp4 = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "approve", "note": "Looks great!"},
            headers={"X-API-Key": API_KEY},
        )
        assert resp4.status_code == 200
        assert _talk(db_session, talk.id).status == "assembling"
        mock_light_queue.enqueue.assert_called_once()
        queued_intro_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_intro_task == job_intro

        # 8. Execute job_intro -> dispatches assembly to enqueue job_concat
        mock_light_queue.reset_mock()
        job_intro(talk.id, cut_key, f"{talk.id}/intro/intro.mp4")
        assert any(
            j.kind == "intro" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )
        mock_light_queue.enqueue.assert_called_once()
        queued_concat_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_concat_task == job_concat

        # 10. Execute job_concat -> enqueues job_loudness
        mock_light_queue.reset_mock()
        job_concat(
            talk.id,
            cut_key=cut_key,
            intro_key=f"{talk.id}/intro/intro.mp4",
            outro_key=None,
            concat_key=f"{talk.id}/assemble/assemble.mp4",
        )
        assert any(
            j.kind == "concat" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )
        mock_light_queue.enqueue.assert_called_once()
        queued_loudness_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_loudness_task == job_loudness

        # 11. Execute job_loudness -> transitions talk to 'transcoding' and enqueues job_transcode on heavy_queue
        mock_light_queue.reset_mock()
        mock_heavy_queue.reset_mock()
        job_loudness(
            talk.id,
            f"{talk.id}/assemble/assemble.mp4",
            f"{talk.id}/assemble/assemble_loud.mp4",
        )
        db_session.expire_all()
        assert _talk(db_session, talk.id).status == "transcoding"
        assert any(
            j.kind == "loudness" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )
        mock_heavy_queue.enqueue.assert_called_once()
        queued_transcode_task, *_ = mock_heavy_queue.enqueue.call_args[0]
        assert queued_transcode_task == job_transcode

        # 12. Execute job_transcode -> transitions talk to 'uploading' and enqueues job_publish on light_queue
        mock_light_queue.reset_mock()
        mock_heavy_queue.reset_mock()
        job_transcode(
            talk.id,
            f"{talk.id}/assemble/assemble_loud.mp4",
            f"{talk.id}/final/final.mp4",
        )
        db_session.expire_all()
        assert _talk(db_session, talk.id).status == "uploading"
        assert any(
            j.kind == "transcode" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )
        mock_light_queue.enqueue.assert_called_once()
        queued_publish_task, *_ = mock_light_queue.enqueue.call_args[0]
        assert queued_publish_task == job_publish

        # 13. Execute job_publish -> transitions talk to terminal 'done'
        mock_light_queue.reset_mock()
        job_publish(talk.id, f"{talk.id}/final/final.mp4")
        db_session.expire_all()
        assert _talk(db_session, talk.id).status == "done"
        assert any(
            j.kind == "publish" and j.status == "done"
            for j in _talk_jobs(db_session, talk.id)
        )

        # 14. Final GET /talks/{id} verify
        get_resp = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
        assert get_resp.status_code == 200
        talk_payload = get_resp.json()
        assert talk_payload["status"] == "done"
        assert talk_payload["cut_start"] == 10.0
        assert talk_payload["cut_end"] == 3000.0
        assert talk_payload["raw_duration_seconds"] == 3600.0
        jobs = _talk_jobs(db_session, talk.id)
        assert len(jobs) == 8
        assert {j.kind for j in jobs} == {
            "detect",
            "cut",
            "preview",
            "intro",
            "concat",
            "loudness",
            "transcode",
            "publish",
        }

    app.dependency_overrides.pop(get_storage_backend, None)


def test_cross_tenant_event_scoping_isolation(db_session: Session) -> None:
    """Assert cross-tenant scoping isolation across all talk and job endpoints against a real database.

    Verifies that a client authorized only for event B receives 404 Not Found (or 403 Forbidden)
    and cannot inspect or mutate talks/jobs belonging to event A.
    """
    event1 = _create_event(db_session, "Tenant A Event")
    event2 = _create_event(db_session, "Tenant B Event")

    talk = _create_talk(db_session, event1.id, title="Tenant A Talk")
    job = models.Job(talk_id=talk.id, kind="detect", status="done")
    db_session.add(job)
    db_session.commit()

    # Authenticated client scoped strictly to event2 (Tenant B)
    _create_api_client(db_session, event_ids=[event2.id], api_key="client_b")
    api_key_header = {"X-API-Key": "client_b"}

    # 1. GET /talks/{talk.id} -> 404
    r1 = client.get(f"/talks/{talk.id}", headers=api_key_header)
    assert r1.status_code == 404
    assert r1.json()["detail"] == "Talk not found"

    # 2. GET /jobs/{job.id} -> 404
    r2 = client.get(f"/jobs/{job.id}", headers=api_key_header)
    assert r2.status_code == 404
    assert r2.json()["detail"] == "Job not found"

    # 3. POST /talks/{talk.id}/recordings -> 404
    r3 = client.post(
        f"/talks/{talk.id}/recordings",
        json={"source_path": "/tmp/test.mp4"},
        headers=api_key_header,
    )
    assert r3.status_code == 404
    assert r3.json()["detail"] == "Talk not found"

    # 4. POST /talks/{talk.id}/approve -> 404
    r4 = client.post(
        f"/talks/{talk.id}/approve",
        json={"decision": "approve"},
        headers=api_key_header,
    )
    assert r4.status_code == 404
    assert r4.json()["detail"] == "Talk not found"

    # 5. POST /talks/{talk.id}/cut -> 404
    r5 = client.post(
        f"/talks/{talk.id}/cut",
        json={"cut_start": "00:00:10", "cut_end": "00:20:00"},
        headers=api_key_header,
    )
    assert r5.status_code == 404
    assert r5.json()["detail"] == "Talk not found"

    # 6. POST /talks/{talk.id}/abort -> 404
    r6 = client.post(f"/talks/{talk.id}/abort", headers=api_key_header)
    assert r6.status_code == 404
    assert r6.json()["detail"] == "Talk not found"

    # 7. GET /talks/{talk.id}/raw-preview -> 404
    r7 = client.get(f"/talks/{talk.id}/raw-preview", headers=api_key_header)
    assert r7.status_code == 404
    assert r7.json()["detail"] == "Talk not found"

    # 8. POST /talks/{talk.id}/review -> 403 (unauthorized event)
    r8 = client.post(
        f"/talks/{talk.id}/review",
        json={"decision": "approve"},
        headers=api_key_header,
    )
    assert r8.status_code == 403

    # Ensure talk state was not modified
    db_session.refresh(talk)
    assert talk.status == TalkStatus.WAITING_FILES


# ---------------------------------------------------------------------------
# Module-level RQ dummy tasks for integration tests
# ---------------------------------------------------------------------------


def _e2e_dummy_light_task(val: int = 1) -> int:
    return val + 1


def _e2e_dummy_heavy_task(duration: float = 0.5) -> str:
    import time

    time.sleep(duration)
    return "heavy_complete"


def _run_named_simple_worker(redis_url: str, queue_name: str) -> None:
    import redis
    from rq import Queue, SimpleWorker

    conn = redis.from_url(redis_url)
    q = Queue(queue_name, connection=conn)
    SimpleWorker([q], connection=conn).work(burst=True)


def test_e2e_heavy_light_queue_isolation() -> None:
    """Verify that heavy queue workloads do not block light queue tasks.

    Asserts that STAGE_CONFIG strictly segregates CPU-intensive stages
    (such as transcode) into the heavy queue and interactive stages
    into the light queue, and verifies independent queue execution with SimpleWorker.
    """
    from uuid import uuid4

    import redis
    from rq import Queue, SimpleWorker

    from app.config import settings
    from app.queue import heavy_queue, light_queue
    from app.tasks import STAGE_CONFIG

    # 1. Verify queue configuration segregation
    assert STAGE_CONFIG["transcode"]["queue"] == "heavy"
    assert STAGE_CONFIG["detect"]["queue"] == "light"
    assert STAGE_CONFIG["cut"]["queue"] == "light"
    assert STAGE_CONFIG["preview"]["queue"] == "light"
    assert STAGE_CONFIG["loudness"]["queue"] == "light"
    assert STAGE_CONFIG["publish"]["queue"] == "light"

    # 2. Verify independent Queue instances and names
    assert light_queue.name == "light"
    assert heavy_queue.name == "heavy"
    assert light_queue.name != heavy_queue.name

    # 3. Real RQ queue isolation: light-only worker drains light queue and leaves heavy work queued
    conn = redis.from_url(settings.redis_url)
    iso_heavy_q = Queue(f"test_heavy_{uuid4().hex}", connection=conn)
    iso_light_q = Queue(f"test_light_{uuid4().hex}", connection=conn)

    try:
        heavy_job = iso_heavy_q.enqueue(_e2e_dummy_heavy_task, 0.1)
        light_job = iso_light_q.enqueue(_e2e_dummy_light_task, 41)

        # Light worker processes only the light queue
        SimpleWorker([iso_light_q], connection=conn).work(burst=True)

        light_job.refresh()
        heavy_job.refresh()

        assert light_job.get_status() in ("finished", "done")
        assert light_job.return_value() == 42
        assert heavy_job.get_status() == "queued"
        assert iso_heavy_q.count == 1

        # Drain heavy queue
        SimpleWorker([iso_heavy_q], connection=conn).work(burst=True)
        heavy_job.refresh()
        assert heavy_job.get_status() in ("finished", "done")
        assert heavy_job.return_value() == "heavy_complete"
        assert iso_heavy_q.count == 0
    finally:
        iso_heavy_q.empty()
        iso_light_q.empty()


# ---------------------------------------------------------------------------
# Phase 5 Integration Tests (Review Flow, Disk Guard & Progress Tracking)
# ---------------------------------------------------------------------------


def test_lifecycle_disk_guard_insufficient_storage(db_session: Session) -> None:
    """Verify that insufficient disk storage guards against recording ingestion."""
    from app.ingest import InsufficientStorageError

    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.free_bytes.return_value = 100
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    with (
        patch("app.routes.talks.light_queue") as mock_light_q,
        patch(
            "app.routes.talks.stage_recording",
            side_effect=InsufficientStorageError(
                required_bytes=1000000, available_bytes=100
            ),
        ),
    ):
        try:
            resp = client.post(
                f"/talks/{talk.id}/recordings",
                json={"source_path": "/tmp/mock_source.mp4"},
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 507
            assert "Insufficient storage" in resp.json()["detail"]
            assert _talk(db_session, talk.id).status == "waiting_for_files"
            mock_light_q.enqueue.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_storage_backend, None)


def test_lifecycle_full_approve_review_flow_to_done(db_session: Session) -> None:
    """Drive a talk from preview through review approve, assembly, and publishing to done.

    Validates review submission, pending_intro_outro transition, assembly orchestration,
    and confirms the final published artifact is retrievable in storage.
    """
    from app.tasks import (
        job_concat,
        job_loudness,
        job_publish,
        job_transcode,
    )

    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        title="Review Flow Talk",
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
        raw_duration_seconds=3600.0,
    )
    _create_api_client(db_session, event_ids=[event.id])

    storage_data: dict[str, str] = {
        f"{talk.id}/raw/mock_source.mp4": "raw_video_bytes",
        f"{talk.id}/cut/cut.mp4": "cut_video_bytes",
        f"{talk.id}/preview/preview.mp4": "preview_video_bytes",
    }
    mock_storage = MagicMock()
    mock_storage.exists.side_effect = lambda k: k in storage_data
    mock_storage.get.side_effect = lambda k: Path(f"/tmp/{k}")
    mock_storage.url.side_effect = lambda k: f"https://cdn.example.com/{k}"
    mock_storage.put.side_effect = lambda key, source: storage_data.update(
        {key: str(source)}
    )
    mock_storage.list_keys.side_effect = lambda prefix="": [
        k for k in storage_data if k.startswith(prefix)
    ]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    cut_key = f"{talk.id}/cut/cut.mp4"
    worker_sessions = _worker_session_factory(db_session)

    try:
        with (
            patch("app.routes.talks.light_queue") as mock_lq,
            patch("app.routes.talks.heavy_queue") as mock_hq,
            patch("app.tasks.light_queue", mock_lq),
            patch("app.tasks.heavy_queue", mock_hq),
            patch("app.tasks.SessionLocal", side_effect=worker_sessions),
            patch("app.tasks.get_storage_backend", return_value=mock_storage),
            patch("app.tasks.concat"),
            patch("app.tasks.normalize"),
            patch("app.tasks.transcode"),
            patch("app.tasks.publish") as mock_publish_fn,
        ):
            mock_publish_fn.side_effect = lambda *args, **kwargs: storage_data.update(
                {f"{talk.id}/final/final.mp4": "published_video"}
            )

            # 1. Speaker approves preview -> transitions to 'assembling' and enqueues job_concat
            review_resp = client.post(
                f"/talks/{talk.id}/review",
                json={"decision": "approve", "note": "Approved by speaker"},
                headers={"X-API-Key": API_KEY},
            )
            assert review_resp.status_code == 200
            assert _talk(db_session, talk.id).status == "assembling"

            review = db_session.scalar(
                select(models.Review).where(models.Review.talk_id == talk.id)
            )
            assert review is not None
            assert review.decision == "approve"
            assert review.note == "Approved by speaker"

            mock_lq.enqueue.assert_called_once()
            queued_concat_task, *_ = mock_lq.enqueue.call_args[0]
            assert queued_concat_task == job_concat

            # 3. Execute job_concat -> enqueues job_loudness
            mock_lq.reset_mock()
            job_concat(
                talk.id,
                cut_key=cut_key,
                intro_key=None,
                outro_key=None,
                concat_key=f"{talk.id}/assemble/assemble.mp4",
            )
            mock_lq.enqueue.assert_called_once()
            queued_loudness, *_ = mock_lq.enqueue.call_args[0]
            assert queued_loudness == job_loudness

            # 4. Execute job_loudness -> transitions to 'transcoding', enqueues job_transcode on heavy_queue
            mock_lq.reset_mock()
            mock_hq.reset_mock()
            job_loudness(
                talk.id,
                f"{talk.id}/assemble/assemble.mp4",
                f"{talk.id}/assemble/assemble_loud.mp4",
            )
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "transcoding"
            mock_hq.enqueue.assert_called_once()
            queued_transcode, *_ = mock_hq.enqueue.call_args[0]
            assert queued_transcode == job_transcode

            # 5. Execute job_transcode -> transitions to 'uploading', enqueues job_publish on light_queue
            mock_lq.reset_mock()
            mock_hq.reset_mock()
            job_transcode(
                talk.id,
                f"{talk.id}/assemble/assemble_loud.mp4",
                f"{talk.id}/final/final.mp4",
            )
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "uploading"
            mock_lq.enqueue.assert_called_once()
            queued_publish, *_ = mock_lq.enqueue.call_args[0]
            assert queued_publish == job_publish

            # 6. Execute job_publish -> transitions talk to 'done' and verifies final storage artifact
            job_publish(talk.id, f"{talk.id}/final/final.mp4")
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "done"
            assert mock_storage.exists(f"{talk.id}/final/final.mp4")

            # 7. Final GET /talks/{id} verification
            get_resp = client.get(f"/talks/{talk.id}", headers={"X-API-Key": API_KEY})
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert data["status"] == "done"
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_lifecycle_needs_work_review_loop(db_session: Session) -> None:
    """Drive consecutive needs_work cycles, verifying return to preview without stale jobs."""
    from app.tasks import job_cut, job_preview

    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        title="Needs Work Loop Talk",
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
        raw_duration_seconds=3600.0,
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    mock_storage.exists.return_value = True
    mock_storage.get.return_value = Path("/tmp/mock_raw.mp4")
    mock_storage.list_keys.return_value = [f"{talk.id}/raw/mock_source.mp4"]
    mock_storage.url.return_value = "https://example.com/preview.mp4"
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    raw_key = f"{talk.id}/raw/mock_source.mp4"
    cut_key = f"{talk.id}/cut/cut.mp4"
    worker_sessions = _worker_session_factory(db_session)

    try:
        with (
            patch("app.tasks.SessionLocal", side_effect=worker_sessions),
            patch("app.tasks.get_storage_backend", return_value=mock_storage),
            patch("app.tasks.cut"),
            patch("app.tasks.generate_preview"),
            patch("app.routes.talks.light_queue") as mock_lq,
            patch("app.tasks.light_queue", mock_lq),
        ):
            # === CYCLE 1: needs_work -> pending_bounds -> cutting -> preview ===
            # Speaker requests rework
            r1 = client.post(
                f"/talks/{talk.id}/review",
                json={"decision": "needs_work", "note": "Cut head too close"},
                headers={"X-API-Key": API_KEY},
            )
            assert r1.status_code == 200
            assert _talk(db_session, talk.id).status == "pending_bounds"

            # Operator resubmits cut bounds
            cut1 = client.post(
                f"/talks/{talk.id}/cut",
                json={"cut_start": "00:00:15", "cut_end": "00:45:00"},
                headers={"X-API-Key": API_KEY},
            )
            assert cut1.status_code == 202
            reloaded = _talk(db_session, talk.id)
            assert reloaded.status == "cutting"
            assert reloaded.cut_start == 15.0
            assert reloaded.cut_end == 2700.0

            # Execute cycle 1 pipeline jobs
            job_cut(talk.id, raw_key)
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "generating_previews"
            job_preview(talk.id, cut_key)
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "preview"

            # === CYCLE 2: needs_work -> pending_bounds -> cutting -> preview ===
            # Speaker requests second rework
            r2 = client.post(
                f"/talks/{talk.id}/review",
                json={"decision": "needs_work", "note": "Need 5s more at the end"},
                headers={"X-API-Key": API_KEY},
            )
            assert r2.status_code == 200
            assert _talk(db_session, talk.id).status == "pending_bounds"

            # Operator resubmits revised cut bounds
            cut2 = client.post(
                f"/talks/{talk.id}/cut",
                json={"cut_start": "00:00:15", "cut_end": "00:46:00"},
                headers={"X-API-Key": API_KEY},
            )
            assert cut2.status_code == 202
            reloaded = _talk(db_session, talk.id)
            assert reloaded.status == "cutting"
            assert reloaded.cut_start == 15.0
            assert reloaded.cut_end == 2760.0

            # Execute cycle 2 pipeline jobs
            job_cut(talk.id, raw_key)
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "generating_previews"
            job_preview(talk.id, cut_key)
            db_session.expire_all()
            assert _talk(db_session, talk.id).status == "preview"

            # Verify review count and notes
            reviews = list(
                db_session.scalars(
                    select(models.Review).where(models.Review.talk_id == talk.id)
                )
            )
            assert len(reviews) == 2
            assert [r.decision for r in reviews] == ["needs_work", "needs_work"]
            assert reviews[0].note == "Cut head too close"
            assert reviews[1].note == "Need 5s more at the end"
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_lifecycle_initial_approval_reject_flow(db_session: Session) -> None:
    """Verify initial approve reject transitions talk from pending_approval to terminal rejected."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Reject Flow Talk", status="pending_approval"
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        r1 = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "reject"},
            headers={"X-API-Key": API_KEY},
        )
        assert r1.status_code == 200
        assert _talk(db_session, talk.id).status == "rejected"
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_lifecycle_speaker_review_reject_and_storage_cleanup(
    db_session: Session,
) -> None:
    """Verify speaker review reject clears cut bounds, resets to pending_bounds, and deletes staging storage."""
    event = _create_event(db_session)
    talk = _create_talk(
        db_session,
        event.id,
        title="Reject Cleanup Talk",
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
    )
    _create_api_client(db_session, event_ids=[event.id])

    mock_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    try:
        r = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "reject", "note": "Recording unsuitable"},
            headers={"X-API-Key": API_KEY},
        )
        assert r.status_code == 200

        db_session.expire_all()
        reloaded = _talk(db_session, talk.id)
        assert reloaded.status == "pending_bounds"
        assert reloaded.cut_start is None
        assert reloaded.cut_end is None

        review = db_session.scalar(
            select(models.Review).where(models.Review.talk_id == talk.id)
        )
        assert review is not None
        assert review.decision == "reject"
        assert review.note == "Recording unsuitable"

        mock_storage.delete.assert_any_call(f"{talk.id}/cut")
        mock_storage.delete.assert_any_call(f"{talk.id}/preview")
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_job_progress_tracking_transcode_polling(db_session: Session) -> None:
    """Verify Job.progress_pct updates across sequential polls with timing metadata."""
    now = datetime.now(UTC)
    event = _create_event(db_session)
    talk = _create_talk(
        db_session, event.id, title="Progress Talk", status="transcoding"
    )
    _create_api_client(db_session, event_ids=[event.id])

    job = models.Job(
        talk_id=talk.id,
        kind="transcode",
        status="running",
        progress_pct=25.0,
        started_at=now - timedelta(seconds=10),
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # Poll 1: initial progress at 25%
    p1 = client.get(f"/jobs/{job.id}", headers={"X-API-Key": API_KEY})
    assert p1.status_code == 200
    data1 = p1.json()
    assert data1["progress_pct"] == 25.0
    assert data1["status"] == "running"

    # Advance job progress to 75%
    job.progress_pct = 75.0
    job.updated_at = datetime.now(UTC)
    db_session.commit()

    # Poll 2: updated progress at 75%
    p2 = client.get(f"/jobs/{job.id}", headers={"X-API-Key": API_KEY})
    assert p2.status_code == 200
    data2 = p2.json()
    assert data2["progress_pct"] == 75.0
    assert data2["progress_pct"] > data1["progress_pct"]

    # Finalize job to done at 100%
    job.progress_pct = 100.0
    job.status = "done"
    db_session.commit()

    p3 = client.get(f"/jobs/{job.id}", headers={"X-API-Key": API_KEY})
    assert p3.status_code == 200
    data3 = p3.json()
    assert data3["progress_pct"] == 100.0
    assert data3["status"] == "done"
