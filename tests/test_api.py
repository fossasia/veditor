from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import get_client
from app.db import get_db
from app.main import app
from app.storage import get_storage_backend

client = TestClient(app)


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
    return models.Talk(
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


def _setup_deps(mock_db, mock_storage=None, event_ids=(1,)):
    mock_client = models.Client(id=1, event_ids=list(event_ids))
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db
    if mock_storage is not None:
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


def test_approve_transitions_to_pending_bounds():
    mock_db = MagicMock()
    talk = _mock_talk(status="pending_approval")
    mock_db.query.return_value.filter.return_value.first.return_value = talk
    _setup_deps(mock_db)
    try:
        resp = client.post("/talks/1/approve", headers={"X-API-Key": "valid"})
        assert resp.status_code == 200
        assert talk.status == "pending_bounds"
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
        # 1. Approve → pending_bounds
        resp = client.post("/talks/1/approve", headers={"X-API-Key": "valid"})
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
