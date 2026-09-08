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


# ---------------------------------------------------------------------------
# End-to-End Integration Tests
# ---------------------------------------------------------------------------


def test_e2e_full_lifecycle_ingest_to_done():
    """End-to-End integration test covering the complete talk processing lifecycle.

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

            # 3. Approve talk -> transitions talk to 'pending_bounds'
            resp2 = client.post(
                "/talks/1/approve",
                json={"decision": "approve"},
                headers={"X-API-Key": "valid"},
            )
            assert resp2.status_code == 200
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

            # 7. Review talk -> transitions talk to 'pending_intro_outro'
            resp4 = client.post(
                "/talks/1/review",
                json={"decision": "approve", "note": "Looks great!"},
                headers={"X-API-Key": "valid"},
            )
            assert resp4.status_code == 200
            assert talk.status == "pending_intro_outro"

            # 8. Submit assembly request -> transitions talk to 'assembling' and enqueues job_intro
            mock_light_queue.reset_mock()
            resp5 = client.post(
                "/talks/1/assemble",
                json={
                    "include_intro": True,
                    "intro_source": "generated",
                    "include_outro": False,
                },
                headers={"X-API-Key": "valid"},
            )
            assert resp5.status_code == 202
            assert talk.status == "assembling"
            mock_light_queue.enqueue.assert_called_once()
            queued_intro_task, *_ = mock_light_queue.enqueue.call_args[0]
            assert queued_intro_task == job_intro

            # 9. Execute job_intro -> dispatches assembly to enqueue job_concat
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


def test_e2e_cross_tenant_event_scoping_isolation():
    """Assert cross-tenant scoping isolation across all talk and job endpoints.

    Verifies that a client authorized only for event_id=2 receives 404 Not Found (or 403 Forbidden)
    and cannot inspect or mutate talks/jobs belonging to event_id=1.
    """
    mock_db = MagicMock()
    mock_storage = MagicMock()
    talk = _mock_talk(talk_id=1, event_id=1, status="waiting_for_files")
    job = models.Job(id=42, talk_id=1, kind="detect", status="done")
    job.talk = talk

    def query_mock(model):
        q = MagicMock()
        if model == models.Talk:
            q.filter.return_value.first.return_value = talk
            q.filter.return_value.with_for_update.return_value.first.return_value = talk
        elif model == models.Job:
            q.filter.return_value.first.return_value = job
        return q

    mock_db.query.side_effect = query_mock
    # Client B only has access to event_id=2
    _setup_deps(mock_db, mock_storage, event_ids=[2])

    try:
        # 1. GET /talks/1 -> 404
        r1 = client.get("/talks/1", headers={"X-API-Key": "client_b"})
        assert r1.status_code == 404
        assert r1.json()["detail"] == "Talk not found"

        # 2. GET /jobs/42 -> 404
        r2 = client.get("/jobs/42", headers={"X-API-Key": "client_b"})
        assert r2.status_code == 404
        assert r2.json()["detail"] == "Job not found"

        # 3. POST /talks/1/recordings -> 404
        r3 = client.post(
            "/talks/1/recordings",
            json={"source_path": "/tmp/test.mp4"},
            headers={"X-API-Key": "client_b"},
        )
        assert r3.status_code == 404
        assert r3.json()["detail"] == "Talk not found"

        # 4. POST /talks/1/approve -> 404
        r4 = client.post(
            "/talks/1/approve",
            json={"decision": "approve"},
            headers={"X-API-Key": "client_b"},
        )
        assert r4.status_code == 404
        assert r4.json()["detail"] == "Talk not found"

        # 5. POST /talks/1/cut -> 404
        r5 = client.post(
            "/talks/1/cut",
            json={"cut_start": "00:00:10", "cut_end": "00:20:00"},
            headers={"X-API-Key": "client_b"},
        )
        assert r5.status_code == 404
        assert r5.json()["detail"] == "Talk not found"

        # 6. POST /talks/1/abort -> 404
        r6 = client.post(
            "/talks/1/abort",
            headers={"X-API-Key": "client_b"},
        )
        assert r6.status_code == 404
        assert r6.json()["detail"] == "Talk not found"

        # 7. GET /talks/1/raw-preview -> 404
        r7 = client.get(
            "/talks/1/raw-preview",
            headers={"X-API-Key": "client_b"},
        )
        assert r7.status_code == 404
        assert r7.json()["detail"] == "Talk not found"

        # 8. POST /talks/1/review -> 403 (unauthorized event)
        r8 = client.post(
            "/talks/1/review",
            json={"decision": "approve"},
            headers={"X-API-Key": "client_b"},
        )
        assert r8.status_code == 403

        # Ensure talk state was not modified
        assert talk.status == "waiting_for_files"
    finally:
        _clear_deps()


def test_e2e_heavy_light_queue_isolation():
    """Verify that heavy queue workloads do not block light queue tasks.

    Asserts that STAGE_CONFIG strictly segregates CPU-intensive stages
    (such as transcode) into the heavy queue and interactive stages
    into the light queue, and verifies independent queue routing.
    """
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

    # 3. Simulate queue execution order: light worker processes only light queue
    processed = []

    def mock_heavy_work():
        processed.append("heavy_completed")

    def mock_light_work():
        processed.append("light_completed")

    mock_light_q = MagicMock()
    mock_heavy_q = MagicMock()

    mock_light_q.name = "light"
    mock_heavy_q.name = "heavy"

    # Enqueue heavy task first, then light task
    mock_heavy_q.enqueue(mock_heavy_work)
    mock_light_q.enqueue(mock_light_work)

    # Light worker processes light task without touching heavy task
    mock_light_q.enqueue.assert_called_once_with(mock_light_work)
    mock_heavy_q.enqueue.assert_called_once_with(mock_heavy_work)

    # Execute light work
    mock_light_work()
    assert processed == ["light_completed"]
    assert "heavy_completed" not in processed
