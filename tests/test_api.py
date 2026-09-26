from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.auth import get_client
from app.db import engine, get_db
from app.main import app
from app.storage import get_storage_backend

client = TestClient(app)

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False)


@pytest.fixture
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    app.dependency_overrides[get_db] = lambda: session

    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        session.close()
        transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_talk(
    talk_id: int = 1,
    event_id: int = 1,
    status: str = "waiting_for_files",
    raw_duration_seconds: float | None = None,
    cut_start: float | None = None,
    cut_end: float | None = None,
) -> models.Talk:
    event = models.Event(id=event_id, name="Test Event")
    talk = models.Talk(
        id=talk_id,
        event_id=event_id,
        title="Test Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status=status,
        raw_duration_seconds=raw_duration_seconds,
        cut_start=cut_start,
        cut_end=cut_end,
    )
    talk.event = event
    return talk


def _setup_deps(mock_db, mock_storage=None, event_ids=(1,)):
    mock_client = models.Client(id=1, event_ids=list(event_ids))
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    if hasattr(mock_db, "query"):
        mock_db.query.return_value.filter.return_value.with_for_update.return_value = (
            mock_db.query.return_value.filter.return_value
        )
    if mock_storage is None:
        mock_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage


def _clear_deps():
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Health check (existing)
# ---------------------------------------------------------------------------


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
def test_raw_preview_gated_before_pending_bounds(blocked_status):
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status=blocked_status)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.get("/talks/1/raw-preview", headers={"X-API-Key": "valid"})
        assert resp.status_code == 403
    finally:
        _clear_deps()


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
def test_raw_preview_accessible_from_pending_bounds_onwards(allowed_status):
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status=allowed_status)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    mock_storage.url.return_value = "file:///data/1/raw/recording.mp4"
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.get("/talks/1/raw-preview", headers={"X-API-Key": "valid"})
        assert resp.status_code == 200
        assert resp.json()["url"] == "file:///data/1/raw/recording.mp4"
    finally:
        _clear_deps()


def test_raw_preview_no_file():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = []
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.get("/talks/1/raw-preview", headers={"X-API-Key": "valid"})
        assert resp.status_code == 404
    finally:
        _clear_deps()


# ---------------------------------------------------------------------------
# POST /talks/{id}/approve
# ---------------------------------------------------------------------------


def test_approve_transitions_to_pending_intro_outro():
    mock_db = MagicMock()
    talk = _mock_talk(status="pending_approval")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db)
    try:
        resp = client.post("/talks/1/approve", headers={"X-API-Key": "valid"})
        assert resp.status_code == 200
        assert talk.status == "pending_intro_outro"
    finally:
        _clear_deps()


def test_approve_reject_transitions_to_rejected():
    mock_db = MagicMock()
    talk = _mock_talk(status="pending_approval")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db)
    try:
        resp = client.post(
            "/talks/1/approve",
            json={"decision": "reject"},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 200
        assert talk.status == "rejected"
    finally:
        _clear_deps()


def test_approve_wrong_state():
    mock_db = MagicMock()
    talk = _mock_talk(status="pending_bounds")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db)
    try:
        resp = client.post("/talks/1/approve", headers={"X-API-Key": "valid"})
        assert resp.status_code == 409
    finally:
        _clear_deps()


def test_rejected_talk_has_no_raw_preview_access():
    """After rejection, raw-preview must remain gated (403)."""
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="rejected")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.get("/talks/1/raw-preview", headers={"X-API-Key": "valid"})
        assert resp.status_code == 403
    finally:
        _clear_deps()


# ---------------------------------------------------------------------------
# POST /talks/{id}/cut-bounds
# ---------------------------------------------------------------------------


def test_cut_bounds_happy_path():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)

    with patch("app.routes.talks.light_queue") as mock_queue:
        try:
            resp = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid"},
            )
            assert resp.status_code == 202
            assert talk.status == "cutting"
            assert talk.cut_start == 10.0
            assert talk.cut_end == 2700.0
            mock_queue.enqueue.assert_called_once()
            # Verify job_cut was enqueued (not arbitrary function)
            call_args = mock_queue.enqueue.call_args
            assert call_args[0][0].__name__ == "job_cut"
            # Verify talk_id and raw_key are passed, not scheduled times
            assert call_args[0][1] == 1  # talk_id
            assert call_args[0][2] == "1/raw/recording.mp4"  # raw_key
        finally:
            _clear_deps()


def test_cut_bounds_with_note():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)

    with patch("app.routes.talks.light_queue"):
        try:
            resp = client.post(
                "/talks/1/cut",
                json={
                    "cut_start": "00:00:10",
                    "cut_end": "00:45:00",
                    "note": "Trimmed initial silent intro",
                },
                headers={"X-API-Key": "valid"},
            )
            assert resp.status_code == 202
            assert talk.status == "cutting"
            # Verify Review model was added with the note
            review_calls = [
                call[0][0]
                for call in mock_db.add.call_args_list
                if isinstance(call[0][0], models.Review)
            ]
            assert len(review_calls) == 1
            assert review_calls[0].note == "Trimmed initial silent intro"
            assert review_calls[0].decision == "cut"
        finally:
            _clear_deps()


def test_cut_bounds_subsecond_precision():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)

    with patch("app.routes.talks.light_queue"):
        try:
            resp = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:10.500", "cut_end": "00:00:45.250"},
                headers={"X-API-Key": "valid"},
            )
            assert resp.status_code == 202
            assert talk.cut_start == 10.5
            assert talk.cut_end == 45.25
        finally:
            _clear_deps()


def test_cut_bounds_end_before_start():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:30:00", "cut_end": "00:10:00"},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 422
    finally:
        _clear_deps()


def test_cut_bounds_equal_start_end():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:10:00", "cut_end": "00:10:00"},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 422
    finally:
        _clear_deps()


def test_cut_bounds_exceeds_duration():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    # 60-second file
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=60.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:05", "cut_end": "00:05:00"},  # 300s > 60s
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 422
    finally:
        _clear_deps()


def test_cut_bounds_missing_raw_duration():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=None)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:05", "cut_end": "00:00:55"},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 422
        assert "no detected raw duration" in resp.json()["detail"]
    finally:
        _clear_deps()


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
def test_cut_bounds_invalid_time_formats(invalid_start, invalid_end):
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_bounds", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": invalid_start, "cut_end": invalid_end},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 422
    finally:
        _clear_deps()


def test_cut_bounds_wrong_state():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(status="pending_approval")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)
    try:
        resp = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:05", "cut_end": "00:00:55"},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 409
    finally:
        _clear_deps()


# ---------------------------------------------------------------------------
# Full happy path: recordings → approve → raw-preview → cut-bounds → cutting
# ---------------------------------------------------------------------------


def test_full_phase4_happy_path():
    """Drive a talk from pending_approval to cutting via approve → raw-preview → cut-bounds."""
    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = ["1/raw/recording.mp4"]
    mock_storage.url.return_value = "file:///data/1/raw/recording.mp4"

    talk = _mock_talk(status="pending_approval", raw_duration_seconds=3600.0)
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)

    try:
        # 1. Approve → pending_intro_outro
        resp = client.post("/talks/1/approve", headers={"X-API-Key": "valid"})
        assert resp.status_code == 200
        assert talk.status == "pending_intro_outro"

        # 1b. Handoff → pending_bounds
        resp = client.post(
            "/talks/1/handoff",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": "valid"},
        )
        assert resp.status_code == 200
        assert talk.status == "pending_bounds"

        # 2. Raw preview accessible
        resp = client.get("/talks/1/raw-preview", headers={"X-API-Key": "valid"})
        assert resp.status_code == 200
        assert "url" in resp.json()

        # 3. Submit cut bounds → cutting
        with patch("app.routes.talks.light_queue") as mock_queue:
            resp = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:10", "cut_end": "01:00:00"},
                headers={"X-API-Key": "valid"},
            )
            assert resp.status_code == 202
            assert talk.status == "cutting"
            # cut_start/cut_end on the talk row — not scheduled start/end
            assert talk.cut_start == 10.0
            assert talk.cut_end == 3600.0
            # Verify job_cut was enqueued with the right arguments
            mock_queue.enqueue.assert_called_once()
    finally:
        _clear_deps()


def test_abort_talk_success():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(
        talk_id=1,
        status="cutting",
        raw_duration_seconds=3600.0,
        cut_start=10.0,
        cut_end=1800.0,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)

    mock_job = MagicMock()
    mock_job.args = (1, "1/raw/test.mp4")
    mock_light = MagicMock()
    mock_light.job_ids = ["job_123"]
    mock_light.fetch_job.return_value = mock_job
    mock_heavy = MagicMock()
    mock_heavy.job_ids = []

    with (
        patch("app.routes.talks.light_queue", mock_light),
        patch("app.routes.talks.heavy_queue", mock_heavy),
    ):
        try:
            resp = client.post("/talks/1/abort", headers={"X-API-Key": "valid"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["id"] == 1
            assert data["status"] == "waiting_for_files"
            assert data["raw_duration_seconds"] is None
            assert data["cut_start"] is None
            assert data["cut_end"] is None

            # Talk state mutated
            assert talk.status == "waiting_for_files"
            assert talk.raw_duration_seconds is None
            assert talk.cut_start is None
            assert talk.cut_end is None

            # Storage cleaned up
            mock_storage.delete.assert_called_once_with("1")

            # RQ job cancelled and deleted
            mock_job.cancel.assert_called_once()
            mock_job.delete.assert_called_once()

            # DB jobs and reviews cleared
            assert mock_db.query.return_value.filter.return_value.delete.call_count >= 2
            assert mock_db.commit.called
        finally:
            _clear_deps()


def test_abort_talk_cancels_jobs_in_registries():
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(
        talk_id=1,
        status="cutting",
        raw_duration_seconds=3600.0,
        cut_start=10.0,
        cut_end=1800.0,
    )
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, mock_storage)

    mock_started_job = MagicMock()
    mock_started_job.id = "job_running"
    mock_started_job.args = (1, "1/raw/test.mp4")

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
            resp = client.post("/talks/1/abort", headers={"X-API-Key": "valid"})
            assert resp.status_code == 200
            mock_send_stop.assert_called_once_with(mock_light.connection, "job_running")
            mock_started_job.cancel.assert_called_once()
            mock_started_job.delete.assert_called_once()
        finally:
            _clear_deps()


def test_abort_talk_not_found():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    _setup_deps(mock_db)
    try:
        resp = client.post("/talks/999/abort", headers={"X-API-Key": "valid"})
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Talk not found"
    finally:
        _clear_deps()


def test_abort_talk_unauthorized_event():
    mock_db = MagicMock()
    talk = _mock_talk(talk_id=1, event_id=2, status="cutting")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db, event_ids=[1])
    try:
        resp = client.post("/talks/1/abort", headers={"X-API-Key": "valid"})
        assert resp.status_code == 404
    finally:
        _clear_deps()


def test_abort_talk_from_any_state():
    """Verify abort works seamlessly from terminal states (e.g. broken, rejected) as well."""
    for initial_status in (
        "broken",
        "rejected",
        "done",
        "waiting_for_files",
        "detecting",
    ):
        mock_db = MagicMock()
        mock_storage = MagicMock()
        talk = _mock_talk(talk_id=1, status=initial_status)
        mock_db.query.return_value.filter.return_value.first.return_value = talk
        _setup_deps(mock_db, mock_storage)

        with (
            patch("app.routes.talks.light_queue") as mock_l,
            patch("app.routes.talks.heavy_queue") as mock_h,
        ):
            mock_l.job_ids = []
            mock_h.job_ids = []
            try:
                resp = client.post("/talks/1/abort", headers={"X-API-Key": "valid"})
                assert resp.status_code == 200
                assert talk.status == "waiting_for_files"
                mock_storage.delete.assert_called_once_with("1")
            finally:
                _clear_deps()


# ---------------------------------------------------------------------------
# Lifecycle & Orchestration Tests
# ---------------------------------------------------------------------------


def test_lifecycle_full_ingest_to_done():
    """Mock-driven orchestration test covering the complete talk processing lifecycle.

    Drives a talk from ingest -> detecting -> pending_approval -> approve ->
    pending_bounds -> cutting -> generating_previews -> preview ->
    pending_intro_outro -> assembling (intro -> concat) -> transcoding (loudness -> transcode) ->
    uploading (publish) -> done.
    Asserts database mutations, stage transitions, and job record persistence at each step.
    """
    from pathlib import Path

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

    talk = _mock_talk(talk_id=1, status="waiting_for_files")
    jobs_dict: dict[int, models.Job] = {}
    next_job_id = [1]

    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_storage.exists.return_value = True
    mock_storage.get.return_value = Path("/tmp/mock_raw.mp4")
    mock_storage.url.return_value = "https://example.com/preview.mp4"

    def query_side_effect(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
            q.filter.return_value.all.return_value = [talk]
        elif model == models.Job:

            def filter_side_effect(*criteria):
                fq = MagicMock()
                matching = list(jobs_dict.values())
                for c in criteria:
                    if (
                        hasattr(c, "left")
                        and hasattr(c.left, "name")
                        and hasattr(c, "right")
                        and hasattr(c.right, "value")
                    ):
                        field = c.left.name
                        val = c.right.value
                        matching = [
                            j for j in matching if getattr(j, field, None) == val
                        ]
                fq.first.side_effect = lambda: matching[0] if matching else None
                fq.order_by.return_value.all.side_effect = lambda: matching
                fq.all.side_effect = lambda: matching
                return fq

            q.filter.side_effect = filter_side_effect
            q.filter.return_value.first.side_effect = lambda: (
                list(jobs_dict.values())[-1] if jobs_dict else None
            )
            q.filter.return_value.order_by.return_value.all.side_effect = lambda: list(
                jobs_dict.values()
            )
            q.filter.return_value.all.side_effect = lambda: list(jobs_dict.values())
        return q

    def add_side_effect(obj):
        if isinstance(obj, models.Job):
            if obj.id is None:
                obj.id = next_job_id[0]
                next_job_id[0] += 1
            obj.talk = talk
            jobs_dict[obj.id] = obj
        elif isinstance(obj, models.Review):
            if obj.id is None:
                obj.id = 1
            if obj.created_at is None:
                obj.created_at = datetime.now(UTC)

    mock_db.query.side_effect = query_side_effect
    mock_db.add.side_effect = add_side_effect
    mock_db.get.side_effect = lambda model, oid: (
        talk if model == models.Talk and oid == 1 else jobs_dict.get(oid)
    )
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, mock_storage, event_ids=[1])

    with (
        patch("app.routes.talks.light_queue") as mock_light_queue,
        patch("app.routes.talks.heavy_queue") as mock_heavy_queue,
        patch("app.tasks.light_queue", new_callable=lambda: mock_light_queue),
        patch("app.tasks.heavy_queue", new_callable=lambda: mock_heavy_queue),
        patch(
            "app.routes.talks.stage_recording",
            return_value="1/raw/mock_source.mp4",
        ),
        patch("app.tasks.SessionLocal", return_value=mock_db),
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

        try:
            # 1. Ingest recording -> transitions talk to 'detecting' and enqueues job_detect
            resp1 = client.post(
                "/talks/1/recordings",
                json={"source_path": "/tmp/mock_source.mp4"},
                headers={"X-API-Key": "valid"},
            )
            assert resp1.status_code == 202
            assert talk.status == "detecting"
            mock_light_queue.enqueue.assert_called_once()
            queued_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_task == job_detect

            # 2. Execute job_detect -> transitions talk to 'pending_approval'
            job_detect(1, "1/raw/mock_source.mp4")
            assert talk.status == "pending_approval"
            assert talk.raw_duration_seconds == 3600.0
            assert any(
                j.kind == "detect" and j.status == "done" for j in jobs_dict.values()
            )

            # 3. Approve talk -> transitions talk to 'pending_intro_outro'
            resp2 = client.post(
                "/talks/1/approve",
                json={"decision": "approve"},
                headers={"X-API-Key": "valid"},
            )
            assert resp2.status_code == 200
            assert talk.status == "pending_intro_outro"

            # 3b. Handoff to speaker -> transitions talk to 'pending_bounds'
            resp_handoff = client.post(
                "/talks/1/handoff",
                json={
                    "include_intro": True,
                    "intro_source": "generated",
                    "include_outro": False,
                },
                headers={"X-API-Key": "valid"},
            )
            assert resp_handoff.status_code == 200
            assert talk.status == "pending_bounds"

            # 4. Submit cut bounds -> transitions talk to 'cutting' and enqueues job_cut
            mock_light_queue.reset_mock()
            resp3 = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:50:00"},
                headers={"X-API-Key": "valid"},
            )
            assert resp3.status_code == 202
            assert talk.status == "cutting"
            assert talk.cut_start == 10.0
            assert talk.cut_end == 3000.0
            mock_light_queue.enqueue.assert_called_once()
            queued_cut_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_cut_task == job_cut

            # 5. Execute job_cut -> transitions talk to 'generating_previews' and enqueues job_preview
            mock_light_queue.reset_mock()
            job_cut(1, "1/raw/mock_source.mp4")
            assert talk.status == "generating_previews"
            assert any(
                j.kind == "cut" and j.status == "done" for j in jobs_dict.values()
            )
            mock_light_queue.enqueue.assert_called_once()
            queued_preview_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_preview_task == job_preview

            # 6. Execute job_preview -> transitions talk to 'preview'
            job_preview(1, "1/cut/cut.mp4")
            assert talk.status == "preview"
            assert any(
                j.kind == "preview" and j.status == "done" for j in jobs_dict.values()
            )

            # 7. Review talk -> transitions talk to 'assembling' and enqueues job_intro
            mock_light_queue.reset_mock()
            resp4 = client.post(
                "/talks/1/review",
                json={"decision": "approve", "note": "Looks great!"},
                headers={"X-API-Key": "valid"},
            )
            assert resp4.status_code == 200
            assert talk.status == "assembling"
            mock_light_queue.enqueue.assert_called_once()
            queued_intro_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_intro_task == job_intro

            # 8. Execute job_intro -> dispatches assembly to enqueue job_concat
            mock_light_queue.reset_mock()
            job_intro(1, "1/cut/cut.mp4", "1/intro/intro.mp4")
            assert any(
                j.kind == "intro" and j.status == "done" for j in jobs_dict.values()
            )
            mock_light_queue.enqueue.assert_called_once()
            queued_concat_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_concat_task == job_concat

            # 10. Execute job_concat -> enqueues job_loudness
            mock_light_queue.reset_mock()
            job_concat(
                1,
                cut_key="1/cut/cut.mp4",
                intro_key="1/intro/intro.mp4",
                outro_key=None,
                concat_key="1/assemble/assemble.mp4",
            )
            assert any(
                j.kind == "concat" and j.status == "done" for j in jobs_dict.values()
            )
            mock_light_queue.enqueue.assert_called_once()
            queued_loudness_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_loudness_task == job_loudness

            # 11. Execute job_loudness -> transitions talk to 'transcoding' and enqueues job_transcode on heavy_queue
            mock_light_queue.reset_mock()
            mock_heavy_queue.reset_mock()
            job_loudness(1, "1/assemble/assemble.mp4", "1/assemble/assemble_loud.mp4")
            assert talk.status == "transcoding"
            assert any(
                j.kind == "loudness" and j.status == "done" for j in jobs_dict.values()
            )
            mock_heavy_queue.enqueue.assert_called_once()
            queued_transcode_task, *_ = mock_heavy_queue.enqueue.call_args[0]
            assert queued_transcode_task == job_transcode

            # 12. Execute job_transcode -> transitions talk to 'uploading' and enqueues job_publish on light_queue
            mock_light_queue.reset_mock()
            mock_heavy_queue.reset_mock()
            job_transcode(1, "1/assemble/assemble_loud.mp4", "1/final/final.mp4")
            assert talk.status == "uploading"
            assert any(
                j.kind == "transcode" and j.status == "done" for j in jobs_dict.values()
            )
            mock_light_queue.enqueue.assert_called_once()
            queued_publish_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_publish_task == job_publish

            # 13. Execute job_publish -> transitions talk to terminal 'done'
            mock_light_queue.reset_mock()
            job_publish(1, "1/final/final.mp4")
            assert talk.status == "done"
            assert any(
                j.kind == "publish" and j.status == "done" for j in jobs_dict.values()
            )

            # 14. Final GET /talks/1 verify
            get_resp = client.get("/talks/1", headers={"X-API-Key": "valid"})
            assert get_resp.status_code == 200
            talk_payload = get_resp.json()
            assert talk_payload["status"] == "done"
            assert talk_payload["cut_start"] == 10.0
            assert talk_payload["cut_end"] == 3000.0
            assert talk_payload["raw_duration_seconds"] == 3600.0
            assert len(jobs_dict) == 8
            assert {j.kind for j in jobs_dict.values()} == {
                "detect",
                "cut",
                "preview",
                "intro",
                "concat",
                "loudness",
                "transcode",
                "publish",
            }
        finally:
            _clear_deps()


def test_cross_tenant_event_scoping_isolation(db_session):
    """Assert cross-tenant scoping isolation across all talk and job endpoints against a real database.

    Verifies that a client authorized only for event_id=2 receives 404 Not Found (or 403 Forbidden)
    and cannot inspect or mutate talks/jobs belonging to event_id=1.
    """
    event1 = models.Event(name="Tenant A Event")
    event2 = models.Event(name="Tenant B Event")
    db_session.add_all([event1, event2])
    db_session.commit()

    talk = models.Talk(
        event_id=event1.id,
        title="Tenant A Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    job = models.Job(talk_id=talk.id, kind="detect", status="done")
    db_session.add(job)
    db_session.commit()

    # Authenticated client scoped strictly to event2 (Tenant B)
    client_b = models.Client(id=2, event_ids=[event2.id])
    app.dependency_overrides[get_client] = lambda: client_b

    try:
        # 1. GET /talks/{talk.id} -> 404
        r1 = client.get(f"/talks/{talk.id}", headers={"X-API-Key": "client_b"})
        assert r1.status_code == 404
        assert r1.json()["detail"] == "Talk not found"

        # 2. GET /jobs/{job.id} -> 404
        r2 = client.get(f"/jobs/{job.id}", headers={"X-API-Key": "client_b"})
        assert r2.status_code == 404
        assert r2.json()["detail"] == "Job not found"

        # 3. POST /talks/{talk.id}/recordings -> 404
        r3 = client.post(
            f"/talks/{talk.id}/recordings",
            json={"source_path": "/tmp/test.mp4"},
            headers={"X-API-Key": "client_b"},
        )
        assert r3.status_code == 404
        assert r3.json()["detail"] == "Talk not found"

        # 4. POST /talks/{talk.id}/approve -> 404
        r4 = client.post(
            f"/talks/{talk.id}/approve",
            json={"decision": "approve"},
            headers={"X-API-Key": "client_b"},
        )
        assert r4.status_code == 404
        assert r4.json()["detail"] == "Talk not found"

        # 5. POST /talks/{talk.id}/cut -> 404
        r5 = client.post(
            f"/talks/{talk.id}/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:20:00"},
            headers={"X-API-Key": "client_b"},
        )
        assert r5.status_code == 404
        assert r5.json()["detail"] == "Talk not found"

        # 6. POST /talks/{talk.id}/abort -> 404
        r6 = client.post(
            f"/talks/{talk.id}/abort",
            headers={"X-API-Key": "client_b"},
        )
        assert r6.status_code == 404
        assert r6.json()["detail"] == "Talk not found"

        # 7. GET /talks/{talk.id}/raw-preview -> 404
        r7 = client.get(
            f"/talks/{talk.id}/raw-preview",
            headers={"X-API-Key": "client_b"},
        )
        assert r7.status_code == 404
        assert r7.json()["detail"] == "Talk not found"

        # 8. POST /talks/{talk.id}/review -> 403 (unauthorized event)
        r8 = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "approve"},
            headers={"X-API-Key": "client_b"},
        )
        assert r8.status_code == 403

        # Ensure talk state was not modified
        db_session.refresh(talk)
        assert talk.status == "waiting_for_files"
    finally:
        app.dependency_overrides.pop(get_client, None)


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


def test_e2e_heavy_light_queue_isolation():
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


def test_e2e_heavy_job_in_flight_does_not_block_light_job():
    """Concurrency test proving an active, in-flight heavy job does not block a light job."""
    import multiprocessing
    import time
    from uuid import uuid4

    import redis
    from rq import Queue, SimpleWorker

    from app.config import settings

    conn = redis.from_url(settings.redis_url)
    iso_heavy_q = Queue(f"test_heavy_{uuid4().hex}", connection=conn)
    iso_light_q = Queue(f"test_light_{uuid4().hex}", connection=conn)

    try:
        heavy_job = iso_heavy_q.enqueue(_e2e_dummy_heavy_task, 1.0)
        light_job = iso_light_q.enqueue(_e2e_dummy_light_task, 99)

        ctx = multiprocessing.get_context("spawn")
        heavy_proc = ctx.Process(
            target=_run_named_simple_worker,
            args=(settings.redis_url, iso_heavy_q.name),
        )
        heavy_proc.start()

        # Wait briefly until the heavy job is picked up or heavy process is running
        for _ in range(20):
            time.sleep(0.05)
            heavy_job.refresh()
            if heavy_job.get_status() == "started" or heavy_proc.is_alive():
                break

        # Execute light worker on main process while heavy job is running
        light_worker = SimpleWorker([iso_light_q], connection=conn)
        light_worker.work(burst=True)

        light_job.refresh()
        heavy_job.refresh()

        # Assert light job finished while heavy worker process is still active / job in flight
        assert light_job.get_status() in ("finished", "done")
        assert light_job.return_value() == 100
        assert heavy_proc.is_alive(), (
            "Light job must complete while heavy job is still in flight"
        )

        heavy_proc.join(timeout=5.0)
        heavy_job.refresh()
        assert not heavy_proc.is_alive()
        assert heavy_job.get_status() in ("finished", "done")
        assert heavy_job.return_value() == "heavy_complete"
    finally:
        iso_heavy_q.empty()
        iso_light_q.empty()


def test_e2e_rq_driven_lifecycle_ingest_to_preview():
    """Drive a talk through the RQ-backed processing lifecycle from ingest to preview.

    Validates that real RQ light queue workers process queued jobs synchronously at each step,
    mutating talk status and persisting job records in the database.
    """
    from pathlib import Path
    from uuid import uuid4

    import redis
    from rq import Queue, SimpleWorker

    from app.config import settings

    conn = redis.from_url(settings.redis_url)
    test_light_q = Queue(f"test_light_{uuid4().hex}", connection=conn)
    test_heavy_q = Queue(f"test_heavy_{uuid4().hex}", connection=conn)

    talk = _mock_talk(talk_id=1, status="waiting_for_files")
    jobs_dict: dict[int, models.Job] = {}
    next_job_id = [1]

    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_storage.exists.return_value = True
    mock_storage.get.return_value = Path("/tmp/mock_raw.mp4")
    mock_storage.url.return_value = "https://example.com/preview.mp4"
    mock_storage.list_keys.return_value = ["1/raw/mock_source.mp4"]

    def query_side_effect(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
            q.filter.return_value.all.return_value = [talk]
        elif model == models.Job:

            def filter_side_effect(*criteria):
                fq = MagicMock()
                matching = list(jobs_dict.values())
                for c in criteria:
                    if (
                        hasattr(c, "left")
                        and hasattr(c.left, "name")
                        and hasattr(c, "right")
                        and hasattr(c.right, "value")
                    ):
                        field = c.left.name
                        val = c.right.value
                        matching = [
                            j for j in matching if getattr(j, field, None) == val
                        ]
                fq.first.side_effect = lambda: matching[0] if matching else None
                fq.order_by.return_value.all.side_effect = lambda: matching
                fq.all.side_effect = lambda: matching
                return fq

            q.filter.side_effect = filter_side_effect
            q.filter.return_value.first.side_effect = lambda: (
                list(jobs_dict.values())[-1] if jobs_dict else None
            )
            q.filter.return_value.order_by.return_value.all.side_effect = lambda: list(
                jobs_dict.values()
            )
            q.filter.return_value.all.side_effect = lambda: list(jobs_dict.values())
        return q

    def add_side_effect(obj):
        if isinstance(obj, models.Job):
            if obj.id is None:
                obj.id = next_job_id[0]
                next_job_id[0] += 1
            obj.talk = talk
            jobs_dict[obj.id] = obj

    mock_db.query.side_effect = query_side_effect
    mock_db.add.side_effect = add_side_effect
    mock_db.get.side_effect = lambda model, oid: (
        talk if model == models.Talk and oid == 1 else jobs_dict.get(oid)
    )
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, mock_storage, event_ids=[1])

    with (
        patch("app.routes.talks.light_queue", test_light_q),
        patch("app.routes.talks.heavy_queue", test_heavy_q),
        patch("app.tasks.light_queue", test_light_q),
        patch("app.tasks.heavy_queue", test_heavy_q),
        patch(
            "app.routes.talks.stage_recording",
            return_value="1/raw/mock_source.mp4",
        ),
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect") as mock_detect,
        patch("app.tasks.cut"),
        patch("app.tasks.generate_preview"),
    ):
        mock_detect.return_value = MagicMock(
            passed=True, actual_duration_seconds=3600.0
        )

        try:
            # 1. Ingest recording -> talk transitions to 'detecting', job_detect enqueued to test_light_q
            resp1 = client.post(
                "/talks/1/recordings",
                json={"source_path": "/tmp/mock_source.mp4"},
                headers={"X-API-Key": "valid"},
            )
            assert resp1.status_code == 202
            assert talk.status == "detecting"
            assert test_light_q.count == 1

            # 2. Run RQ SimpleWorker to process job_detect -> transitions talk to 'pending_approval'
            SimpleWorker([test_light_q], connection=conn).work(burst=True)
            assert test_light_q.count == 0
            assert talk.status == "pending_approval"
            assert talk.raw_duration_seconds == 3600.0
            assert any(
                j.kind == "detect" and j.status == "done" for j in jobs_dict.values()
            )

            # 3. Approve talk -> transitions to 'pending_intro_outro'
            resp2 = client.post(
                "/talks/1/approve",
                json={"decision": "approve"},
                headers={"X-API-Key": "valid"},
            )
            assert resp2.status_code == 200
            assert talk.status == "pending_intro_outro"

            # 3b. Handoff to speaker -> transitions to 'pending_bounds'
            resp_handoff = client.post(
                "/talks/1/handoff",
                json={"include_intro": False, "include_outro": False},
                headers={"X-API-Key": "valid"},
            )
            assert resp_handoff.status_code == 200
            assert talk.status == "pending_bounds"

            # 4. Submit cut bounds -> transitions to 'cutting', job_cut enqueued to test_light_q
            resp3 = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:10", "cut_end": "00:50:00"},
                headers={"X-API-Key": "valid"},
            )
            assert resp3.status_code == 202
            assert talk.status == "cutting"
            assert talk.cut_start == 10.0
            assert talk.cut_end == 3000.0
            assert test_light_q.count == 1

            # 5. Run RQ SimpleWorker to execute job_cut and cascading job_preview -> transitions talk to 'preview'
            SimpleWorker([test_light_q], connection=conn).work(burst=True)
            assert test_light_q.count == 0
            assert talk.status == "preview"
            assert any(
                j.kind == "cut" and j.status == "done" for j in jobs_dict.values()
            )
            assert any(
                j.kind == "preview" and j.status == "done" for j in jobs_dict.values()
            )

            # 6. Final GET /talks/1 assertion
            get_resp = client.get("/talks/1", headers={"X-API-Key": "valid"})
            assert get_resp.status_code == 200
            payload = get_resp.json()
            assert payload["status"] == "preview"
            assert payload["cut_start"] == 10.0
            assert payload["cut_end"] == 3000.0
            assert payload["raw_duration_seconds"] == 3600.0
            assert len(jobs_dict) == 3
            assert {j.kind for j in jobs_dict.values()} == {"detect", "cut", "preview"}
        finally:
            _clear_deps()
            test_light_q.empty()
            test_heavy_q.empty()


# ---------------------------------------------------------------------------
# Phase 5 Integration Tests (Review Flow, Disk Guard & Progress Tracking)
# ---------------------------------------------------------------------------


def test_lifecycle_disk_guard_insufficient_storage():
    """Verify that insufficient disk storage guards against recording ingestion."""
    from app.ingest import InsufficientStorageError

    talk = _mock_talk(talk_id=1, status="waiting_for_files")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_storage = MagicMock()
    mock_storage.free_bytes.return_value = 100

    _setup_deps(mock_db, mock_storage, event_ids=[1])

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
                "/talks/1/recordings",
                json={"source_path": "/tmp/mock_source.mp4"},
                headers={"X-API-Key": "valid"},
            )
            assert resp.status_code == 507
            assert "Insufficient storage" in resp.json()["detail"]
            assert talk.status == "waiting_for_files"
            mock_light_q.enqueue.assert_not_called()
        finally:
            _clear_deps()


def test_lifecycle_full_approve_review_flow_to_done():
    """Drive a talk from preview through review approve, assembly, and publishing to done.

    Validates review submission, pending_intro_outro transition, assembly orchestration,
    and confirms the final published artifact is retrievable in storage.
    """
    from pathlib import Path

    from app.tasks import (
        job_concat,
        job_loudness,
        job_publish,
        job_transcode,
    )

    talk = _mock_talk(
        talk_id=1,
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
        raw_duration_seconds=3600.0,
    )
    jobs_dict: dict[int, models.Job] = {}
    reviews_list: list[models.Review] = []
    next_job_id = [1]
    storage_data: dict[str, str] = {
        "1/raw/mock_source.mp4": "raw_video_bytes",
        "1/cut/cut.mp4": "cut_video_bytes",
        "1/preview/preview.mp4": "preview_video_bytes",
    }

    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_storage.exists.side_effect = lambda k: k in storage_data
    mock_storage.get.side_effect = lambda k: Path(f"/tmp/{k}")
    mock_storage.url.side_effect = lambda k: f"https://cdn.example.com/{k}"

    def put_mock(key, source):
        storage_data[key] = str(source)

    mock_storage.put.side_effect = put_mock
    mock_storage.list_keys.side_effect = lambda prefix="": [
        k for k in storage_data if k.startswith(prefix)
    ]

    def query_mock(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
            q.filter.return_value.all.return_value = [talk]
        elif model == models.Job:
            q.filter.return_value.all.side_effect = lambda: list(jobs_dict.values())
            q.filter.return_value.first.side_effect = lambda: (
                list(jobs_dict.values())[-1] if jobs_dict else None
            )
        elif model == models.Review:
            q.filter.return_value.all.side_effect = lambda: reviews_list
        return q

    def add_mock(obj):
        if isinstance(obj, models.Job):
            if obj.id is None:
                obj.id = next_job_id[0]
                next_job_id[0] += 1
            obj.talk = talk
            jobs_dict[obj.id] = obj
        elif isinstance(obj, models.Review):
            if obj.id is None:
                obj.id = len(reviews_list) + 1
            if obj.created_at is None:
                obj.created_at = datetime.now(UTC)
            reviews_list.append(obj)

    mock_db.query.side_effect = query_mock
    mock_db.get.side_effect = lambda model, oid: (
        talk
        if getattr(model, "__name__", "") == "Talk" and oid == talk.id
        else jobs_dict.get(oid)
    )
    mock_db.add.side_effect = add_mock
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, mock_storage, event_ids=[1])

    with (
        patch("app.routes.talks.light_queue") as mock_lq,
        patch("app.routes.talks.heavy_queue") as mock_hq,
        patch("app.tasks.light_queue", new_callable=lambda: mock_lq),
        patch("app.tasks.heavy_queue", new_callable=lambda: mock_hq),
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat"),
        patch("app.tasks.normalize"),
        patch("app.tasks.transcode"),
        patch("app.tasks.publish") as mock_publish_fn,
    ):
        mock_publish_fn.side_effect = lambda *args, **kwargs: storage_data.update(
            {"1/final/final.mp4": "published_video"}
        )

        try:
            # 1. Speaker approves preview -> transitions to 'assembling' and enqueues job_concat
            review_resp = client.post(
                "/talks/1/review",
                json={"decision": "approve", "note": "Approved by speaker"},
                headers={"X-API-Key": "valid"},
            )
            assert review_resp.status_code == 200
            assert talk.status == "assembling"
            assert len(reviews_list) == 1
            assert reviews_list[0].decision == "approve"
            assert reviews_list[0].note == "Approved by speaker"
            mock_lq.enqueue.assert_called_once()
            queued_concat_task, *_ = mock_lq.enqueue.call_args[0]
            assert queued_concat_task == job_concat

            # 3. Execute job_concat -> enqueues job_loudness
            mock_lq.reset_mock()
            job_concat(
                1,
                cut_key="1/cut/cut.mp4",
                intro_key=None,
                outro_key=None,
                concat_key="1/assemble/assemble.mp4",
            )
            mock_lq.enqueue.assert_called_once()
            queued_loudness, *_ = mock_lq.enqueue.call_args[0]
            assert queued_loudness == job_loudness

            # 4. Execute job_loudness -> transitions to 'transcoding', enqueues job_transcode on heavy_queue
            mock_lq.reset_mock()
            mock_hq.reset_mock()
            job_loudness(1, "1/assemble/assemble.mp4", "1/assemble/assemble_loud.mp4")
            assert talk.status == "transcoding"
            mock_hq.enqueue.assert_called_once()
            queued_transcode, *_ = mock_hq.enqueue.call_args[0]
            assert queued_transcode == job_transcode

            # 5. Execute job_transcode -> transitions to 'uploading', enqueues job_publish on light_queue
            mock_lq.reset_mock()
            mock_hq.reset_mock()
            job_transcode(1, "1/assemble/assemble_loud.mp4", "1/final/final.mp4")
            assert talk.status == "uploading"
            mock_lq.enqueue.assert_called_once()
            queued_publish, *_ = mock_lq.enqueue.call_args[0]
            assert queued_publish == job_publish

            # 6. Execute job_publish -> transitions talk to 'done' and verifies final storage artifact
            job_publish(1, "1/final/final.mp4")
            assert talk.status == "done"
            assert mock_storage.exists("1/final/final.mp4")

            # 7. Final GET /talks/1 verification
            get_resp = client.get("/talks/1", headers={"X-API-Key": "valid"})
            assert get_resp.status_code == 200
            data = get_resp.json()
            assert data["status"] == "done"
        finally:
            _clear_deps()


def test_lifecycle_needs_work_review_loop():
    """Drive consecutive needs_work cycles, verifying return to preview without stale jobs."""
    from pathlib import Path

    from app.tasks import job_cut, job_preview

    talk = _mock_talk(
        talk_id=1,
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
        raw_duration_seconds=3600.0,
    )
    jobs_dict: dict[int, models.Job] = {}
    reviews_list: list[models.Review] = []
    next_job_id = [1]

    mock_db = MagicMock()
    mock_storage = MagicMock()
    mock_storage.exists.return_value = True
    mock_storage.get.return_value = Path("/tmp/mock_raw.mp4")
    mock_storage.list_keys.return_value = ["1/raw/mock_source.mp4"]
    mock_storage.url.return_value = "https://example.com/preview.mp4"

    def query_mock(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
            q.filter.return_value.all.return_value = [talk]
        elif model == models.Job:
            q.filter.return_value.all.side_effect = lambda: list(jobs_dict.values())
            q.filter.return_value.first.side_effect = lambda: (
                list(jobs_dict.values())[-1] if jobs_dict else None
            )
        elif model == models.Review:
            q.filter.return_value.all.side_effect = lambda: reviews_list
        return q

    def add_mock(obj):
        if isinstance(obj, models.Job):
            if obj.id is None:
                obj.id = next_job_id[0]
                next_job_id[0] += 1
            obj.talk = talk
            jobs_dict[obj.id] = obj
        elif isinstance(obj, models.Review):
            if obj.id is None:
                obj.id = len(reviews_list) + 1
            if obj.created_at is None:
                obj.created_at = datetime.now(UTC)
            reviews_list.append(obj)

    mock_db.query.side_effect = query_mock
    mock_db.get.side_effect = lambda model, oid: (
        talk
        if getattr(model, "__name__", "") == "Talk" and oid == talk.id
        else jobs_dict.get(oid)
    )
    mock_db.add.side_effect = add_mock
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, mock_storage, event_ids=[1])

    with (
        patch("app.tasks.SessionLocal", return_value=mock_db),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.cut"),
        patch("app.tasks.generate_preview"),
        patch("app.routes.talks.light_queue") as mock_lq,
        patch("app.tasks.light_queue", new_callable=lambda: mock_lq),
    ):
        try:
            # === CYCLE 1: needs_work -> pending_bounds -> cutting -> preview ===
            # Speaker requests rework
            r1 = client.post(
                "/talks/1/review",
                json={"decision": "needs_work", "note": "Cut head too close"},
                headers={"X-API-Key": "valid"},
            )
            assert r1.status_code == 200
            assert talk.status == "pending_bounds"

            # Operator resubmits cut bounds
            cut1 = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:15", "cut_end": "00:45:00"},
                headers={"X-API-Key": "valid"},
            )
            assert cut1.status_code == 202
            assert talk.status == "cutting"
            assert talk.cut_start == 15.0
            assert talk.cut_end == 2700.0

            # Execute cycle 1 pipeline jobs
            job_cut(1, "1/raw/mock_source.mp4")
            assert talk.status == "generating_previews"
            job_preview(1, "1/cut/cut.mp4")
            assert talk.status == "preview"

            # === CYCLE 2: needs_work -> pending_bounds -> cutting -> preview ===
            # Speaker requests second rework
            r2 = client.post(
                "/talks/1/review",
                json={"decision": "needs_work", "note": "Need 5s more at the end"},
                headers={"X-API-Key": "valid"},
            )
            assert r2.status_code == 200
            assert talk.status == "pending_bounds"

            # Operator resubmits revised cut bounds
            cut2 = client.post(
                "/talks/1/cut",
                json={"cut_start": "00:00:15", "cut_end": "00:46:00"},
                headers={"X-API-Key": "valid"},
            )
            assert cut2.status_code == 202
            assert talk.status == "cutting"
            assert talk.cut_start == 15.0
            assert talk.cut_end == 2760.0

            # Execute cycle 2 pipeline jobs
            job_cut(1, "1/raw/mock_source.mp4")
            assert talk.status == "generating_previews"
            job_preview(1, "1/cut/cut.mp4")
            assert talk.status == "preview"

            # Verify review count and notes
            assert len(reviews_list) == 2
            assert [r.decision for r in reviews_list] == ["needs_work", "needs_work"]
            assert reviews_list[0].note == "Cut head too close"
            assert reviews_list[1].note == "Need 5s more at the end"
        finally:
            _clear_deps()


def test_lifecycle_initial_approval_reject_flow():
    """Verify initial approve reject transitions talk from pending_approval to terminal rejected."""
    mock_db = MagicMock()
    talk = _mock_talk(talk_id=1, status="pending_approval")

    mock_db.query.return_value.filter.return_value.first.return_value = talk
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, event_ids=[1])

    try:
        r1 = client.post(
            "/talks/1/approve",
            json={"decision": "reject"},
            headers={"X-API-Key": "valid"},
        )
        assert r1.status_code == 200
        assert talk.status == "rejected"
    finally:
        _clear_deps()


def test_lifecycle_speaker_review_reject_and_storage_cleanup():
    """Verify speaker review reject clears cut bounds, resets to pending_bounds, and deletes staging storage."""
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(
        talk_id=1,
        status="preview",
        cut_start=10.0,
        cut_end=1800.0,
    )
    reviews: list[models.Review] = []

    def query_mock(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
        elif model == models.Review:
            q.filter.return_value.all.side_effect = lambda: reviews
        return q

    def add_mock(obj):
        if isinstance(obj, models.Review):
            if obj.id is None:
                obj.id = len(reviews) + 1
            if obj.created_at is None:
                obj.created_at = datetime.now(UTC)
            reviews.append(obj)

    mock_db.query.side_effect = query_mock
    mock_db.add.side_effect = add_mock
    mock_db.__enter__.return_value = mock_db
    mock_db.__exit__.return_value = None

    _setup_deps(mock_db, mock_storage, event_ids=[1])

    try:
        r = client.post(
            "/talks/1/review",
            json={"decision": "reject", "note": "Recording unsuitable"},
            headers={"X-API-Key": "valid"},
        )
        assert r.status_code == 200
        assert talk.status == "pending_bounds"
        assert talk.cut_start is None
        assert talk.cut_end is None
        assert len(reviews) == 1
        assert reviews[0].decision == "reject"
        assert reviews[0].note == "Recording unsuitable"
        mock_storage.delete.assert_any_call("1/cut")
        mock_storage.delete.assert_any_call("1/preview")
    finally:
        _clear_deps()


def test_job_progress_tracking_transcode_polling():
    """Verify Job.progress_pct updates across sequential polls with timing metadata."""
    from datetime import timedelta

    now = datetime.now(UTC)
    talk = _mock_talk(talk_id=1, status="transcoding")
    job = models.Job(
        id=42,
        talk_id=1,
        kind="transcode",
        status="running",
        progress_pct=25.0,
        started_at=now - timedelta(seconds=10),
        updated_at=now,
    )
    job.talk = talk

    mock_db = MagicMock()

    def query_mock(model):
        q = MagicMock()
        if model == models.Job:
            q.filter.return_value.first.return_value = job
            q.filter.return_value.order_by.return_value.all.return_value = [job]
            q.filter.return_value.all.return_value = [job]
        elif model == models.Talk:
            q.filter.return_value.first.return_value = talk
        return q

    mock_db.query.side_effect = query_mock
    _setup_deps(mock_db, event_ids=[1])

    try:
        # Poll 1: initial progress at 25%
        p1 = client.get("/jobs/42", headers={"X-API-Key": "valid"})
        assert p1.status_code == 200
        data1 = p1.json()
        assert data1["progress_pct"] == 25.0
        assert data1["status"] == "running"

        # Advance job progress to 75%
        job.progress_pct = 75.0
        job.updated_at = datetime.now(UTC)

        # Poll 2: updated progress at 75%
        p2 = client.get("/jobs/42", headers={"X-API-Key": "valid"})
        assert p2.status_code == 200
        data2 = p2.json()
        assert data2["progress_pct"] == 75.0
        assert data2["progress_pct"] > data1["progress_pct"]

        # Finalize job to done at 100%
        job.progress_pct = 100.0
        job.status = "done"

        p3 = client.get("/jobs/42", headers={"X-API-Key": "valid"})
        assert p3.status_code == 200
        data3 = p3.json()
        assert data3["progress_pct"] == 100.0
        assert data3["status"] == "done"
    finally:
        _clear_deps()
