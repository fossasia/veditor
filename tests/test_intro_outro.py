"""Tests for assembly configuration endpoint (POST /talks/{talk_id}/assemble) and assembly workflow."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import models
from app.auth import get_client
from app.config import Settings, settings
from app.db import get_db
from app.ingest import get_bumper_staging_dir
from app.main import app
from app.storage import get_storage_backend
from app.tasks import (
    STAGE_CONFIG,
    dispatch_assembly,
    job_concat,
    job_intro,
    job_loudness,
    job_outro,
    job_transcode,
)
from tests.conftest import FakeStorageBackend, generate_clip

client = TestClient(app)


class MockSessionContext:
    """Mock context manager for SessionLocal calls inside background tasks & dispatch."""

    def __init__(self, talk, jobs_dict=None):
        self.talk = talk
        self.jobs_dict = jobs_dict if jobs_dict is not None else {}
        self.next_job_id = 100

    def __call__(self):
        ctx = self
        session_mock = MagicMock()

        def get_mock(model, obj_id):
            if model == models.Talk and (ctx.talk and obj_id == ctx.talk.id):
                return ctx.talk
            if model == models.Job:
                return ctx.jobs_dict.get(obj_id)
            return None

        def add_mock(obj):
            if isinstance(obj, models.Job):
                if getattr(obj, "id", None) is None:
                    obj.id = ctx.next_job_id
                    ctx.next_job_id += 1
                ctx.jobs_dict[obj.id] = obj

        def refresh_mock(obj):
            if isinstance(obj, models.Job) and getattr(obj, "id", None) is None:
                obj.id = ctx.next_job_id
                ctx.next_job_id += 1
                ctx.jobs_dict[obj.id] = obj

        def query_mock(model):
            q = MagicMock()
            if model == models.Job:

                def filter_mock(*args):
                    sub_q = MagicMock()

                    def first_mock():
                        target_kind = None
                        for a in args:
                            val = getattr(getattr(a, "right", None), "value", None)
                            if val in ("intro", "outro"):
                                target_kind = val
                                break
                        for j in ctx.jobs_dict.values():
                            if (
                                j.talk_id == ctx.talk.id
                                and j.status == "done"
                                and (target_kind is None or j.kind == target_kind)
                            ):
                                return j
                        return None

                    sub_q.first.side_effect = first_mock
                    return sub_q

                q.filter.side_effect = filter_mock
            return q

        session_mock.get.side_effect = get_mock
        session_mock.add.side_effect = add_mock
        session_mock.refresh.side_effect = refresh_mock
        session_mock.query.side_effect = query_mock
        session_mock.__enter__.return_value = session_mock
        session_mock.__exit__.return_value = None
        return session_mock


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    """Ensure dependency overrides are cleared after each test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_db():
    db = MagicMock()
    mock_filter = db.query.return_value.filter.return_value
    mock_filter.with_for_update.return_value = mock_filter

    def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1

    db.refresh.side_effect = fake_refresh
    return db


@pytest.fixture
def fake_storage():
    storage = FakeStorageBackend()
    storage.put("1/cut/cut.mp4", b"fake cut video bytes")
    return storage


@pytest.fixture
def pending_talk():
    return models.Talk(
        id=1,
        event_id=1,
        title="Keynote on Scalable Systems",
        room="Auditorium A",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status="pending_intro_outro",
        cut_start=0.0,
        cut_end=1800.0,
        raw_duration_seconds=1800.0,
    )


@pytest.fixture
def auth_client():
    return models.Client(id=1, hashed_key="valid_hash", event_ids=[1])


def test_intro_outro_unauthorized():
    """POST /talks/{id}/assemble without X-API-Key returns 401."""
    response = client.post("/talks/1/assemble", json={})
    assert response.status_code == 401


def test_intro_outro_talk_not_found(mock_db, auth_client, fake_storage):
    """POST /talks/{id}/assemble for nonexistent talk returns 404."""
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/999/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_intro_outro_unauthorized_event(mock_db, fake_storage, pending_talk):
    """POST /talks/{id}/assemble for talk outside client's event_ids returns 404."""
    unauthorized_client = models.Client(id=2, hashed_key="other_hash", event_ids=[99])
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: unauthorized_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


@pytest.mark.parametrize(
    "invalid_status",
    [
        "waiting_for_files",
        "detecting",
        "pending_approval",
        "cutting",
        "generating_previews",
        "preview",
        "needs_work",
        "assembling",
        "transcoding",
        "uploading",
        "done",
        "rejected",
        "broken",
    ],
)
def test_intro_outro_invalid_status_conflict(
    mock_db, auth_client, fake_storage, pending_talk, invalid_status
):
    """POST /talks/{id}/assemble when talk status != 'pending_intro_outro' returns 409."""
    pending_talk.status = invalid_status
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert "pending_intro_outro" in response.json()["detail"]


@pytest.mark.parametrize("empty_path", [None, "", "   "])
def test_intro_outro_missing_custom_intro_path_returns_422(
    mock_db, auth_client, fake_storage, pending_talk, empty_path
):
    """Custom intro with empty/missing path fails Pydantic validation with 422."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": empty_path,
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422
    mock_db.commit.assert_not_called()


@pytest.mark.parametrize("empty_path", [None, "", "   "])
def test_intro_outro_missing_custom_outro_path_returns_422(
    mock_db, auth_client, fake_storage, pending_talk, empty_path
):
    """Custom outro with empty/missing path fails Pydantic validation with 422."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_outro": True,
            "outro_source": "custom",
            "custom_outro_path": empty_path,
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422
    mock_db.commit.assert_not_called()


def test_intro_outro_invalid_source_value(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Invalid source literal returns 422 Unprocessable Content."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_intro": True,
            "intro_source": "third_party",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422


def test_intro_outro_no_cut_recording(mock_db, auth_client, pending_talk):
    """When talk has no cut recording in storage, returns 400 Bad Request."""
    empty_storage = FakeStorageBackend()
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: empty_storage

    response = client.post(
        "/talks/1/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "No cut recording found" in response.json()["detail"]
    assert pending_talk.status == "pending_intro_outro"
    mock_db.commit.assert_not_called()


def test_intro_outro_custom_intro_outside_roots(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Custom intro path outside ingest_roots returns 400 with no side effects."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": "/etc/passwd",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Intro clip rejected" in response.json()["detail"]
    assert pending_talk.status == "pending_intro_outro"
    mock_db.commit.assert_not_called()


def test_intro_outro_custom_outro_outside_roots(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Custom outro path outside ingest_roots returns 400 with no side effects."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_outro": True,
            "outro_source": "custom",
            "custom_outro_path": "/etc/shadow",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Outro clip rejected" in response.json()["detail"]
    assert pending_talk.status == "pending_intro_outro"
    mock_db.commit.assert_not_called()


def test_intro_outro_custom_path_corrupt_or_not_found(
    mock_db, auth_client, fake_storage, pending_talk, tmp_path, monkeypatch
):
    """Custom path inside ingest_roots but nonexistent or invalid video returns 400."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    non_video = tmp_path / "corrupt.txt"
    non_video.write_text("not a video")

    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        "/talks/1/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": str(non_video),
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Intro clip rejected" in response.json()["detail"]
    assert pending_talk.status == "pending_intro_outro"
    mock_db.commit.assert_not_called()


def test_intro_outro_skip_both_success(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Skipping both intro and outro advances talk to assembling and enqueues job_concat directly."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": False,
                "include_outro": False,
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "assembling"
    assert data["include_intro"] is False
    assert data["include_outro"] is False
    assert data["intro_source"] is None
    assert data["outro_source"] is None

    assert pending_talk.status == "assembling"
    assert pending_talk.include_intro is False
    assert pending_talk.include_outro is False
    mock_db.commit.assert_called_once()

    mock_enqueue.assert_called_once_with(
        job_concat,
        1,
        "1/cut/cut.mp4",
        None,
        None,
        "1/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_intro_outro_generated_both_success(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Generating both intro and outro advances talk to assembling and enqueues job_intro first."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": True,
                "intro_source": "generated",
                "include_outro": True,
                "outro_source": "generated",
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "assembling"
    assert data["include_intro"] is True
    assert data["include_outro"] is True
    assert data["intro_source"] == "generated"
    assert data["outro_source"] == "generated"

    assert pending_talk.status == "assembling"
    mock_db.commit.assert_called_once()

    mock_enqueue.assert_called_once_with(
        job_intro,
        1,
        "1/cut/cut.mp4",
        "1/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_intro_outro_custom_intro_stages_and_enqueues_outro(
    mock_db, auth_client, fake_storage, pending_talk, tmp_path, monkeypatch
):
    """Custom intro stages the clip and immediately triggers job_outro if outro is generated."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_intro = generate_clip(0.5, output_dir=tmp_path)

    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": True,
                "intro_source": "custom",
                "custom_intro_path": str(valid_intro),
                "include_outro": True,
                "outro_source": "generated",
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    assert fake_storage.exists("1/intro/intro.mp4")
    assert pending_talk.status == "assembling"
    assert pending_talk.custom_intro_path == str(valid_intro)

    mock_enqueue.assert_called_once_with(
        job_outro,
        1,
        "1/cut/cut.mp4",
        "1/outro/outro.mp4",
        job_timeout=STAGE_CONFIG["outro"]["job_timeout"],
    )


def test_intro_outro_custom_outro_stages_and_enqueues_intro(
    mock_db, auth_client, fake_storage, pending_talk, tmp_path, monkeypatch
):
    """Custom outro stages the clip and starts job_intro first if intro is generated."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_outro = generate_clip(0.5, output_dir=tmp_path)

    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": True,
                "intro_source": "generated",
                "include_outro": True,
                "outro_source": "custom",
                "custom_outro_path": str(valid_outro),
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    assert fake_storage.exists("1/outro/outro.mp4")
    assert pending_talk.status == "assembling"
    assert pending_talk.custom_outro_path == str(valid_outro)

    mock_enqueue.assert_called_once_with(
        job_intro,
        1,
        "1/cut/cut.mp4",
        "1/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_intro_outro_custom_both_stages_and_enqueues_concat(
    mock_db, auth_client, fake_storage, pending_talk, tmp_path, monkeypatch
):
    """Providing custom clips for both stages both and enqueues job_concat directly."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_intro = generate_clip(0.5, output_dir=tmp_path)
    valid_outro = generate_clip(0.5, output_dir=tmp_path)

    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": True,
                "intro_source": "custom",
                "custom_intro_path": str(valid_intro),
                "include_outro": True,
                "outro_source": "custom",
                "custom_outro_path": str(valid_outro),
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    assert fake_storage.exists("1/intro/intro.mp4")
    assert fake_storage.exists("1/outro/outro.mp4")
    assert pending_talk.status == "assembling"

    mock_enqueue.assert_called_once_with(
        job_concat,
        1,
        "1/cut/cut.mp4",
        "1/intro/intro.mp4",
        "1/outro/outro.mp4",
        "1/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_intro_outro_custom_cut_key_threading(
    mock_db, auth_client, fake_storage, pending_talk
):
    """Custom cut key prefix in storage is threaded to the enqueued task."""
    fake_storage.delete("1/cut/cut.mp4")
    fake_storage.put("1/cut/recording_custom_bounds.mp4", b"special cut content")

    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": False,
                "include_outro": False,
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    mock_enqueue.assert_called_once_with(
        job_concat,
        1,
        "1/cut/recording_custom_bounds.mp4",
        None,
        None,
        "1/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_dispatch_assembly_both_done_enqueues_job_concat(pending_talk):
    """dispatch_assembly enqueues job_concat when both intro and outro jobs are completed."""
    pending_talk.status = "assembling"
    pending_talk.include_intro = True
    pending_talk.intro_source = "generated"
    pending_talk.include_outro = True
    pending_talk.outro_source = "generated"

    intro_job = models.Job(id=10, talk_id=1, kind="intro", status="done")
    outro_job = models.Job(id=11, talk_id=1, kind="outro", status="done")
    jobs = {10: intro_job, 11: outro_job}

    db_ctx = MockSessionContext(pending_talk, jobs)

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        dispatch_assembly(1, cut_key="1/cut/cut.mp4")

    mock_enqueue.assert_called_once_with(
        job_concat,
        1,
        "1/cut/cut.mp4",
        "1/intro/intro.mp4",
        "1/outro/outro.mp4",
        "1/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_job_concat_success_enqueues_job_loudness(pending_talk):
    """job_concat executes concat, leaves talk in assembling, and enqueues job_loudness."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat") as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        job_concat(
            talk_id=1,
            cut_key="1/cut/cut.mp4",
            intro_key=None,
            outro_key=None,
            concat_key="1/assemble/assemble.mp4",
        )

    mock_concat.assert_called_once_with(
        cut_path=Path("/tmp/fake_cut.mp4"),
        intro_path=None,
        outro_path=None,
        output_path="1/assemble/assemble.mp4",
        backend=mock_storage,
    )
    assert pending_talk.status == "assembling"
    assert any(
        j.kind == "concat" and j.status == "done" for j in db_ctx.jobs_dict.values()
    )
    mock_light_enqueue.assert_called_once_with(
        job_loudness,
        1,
        "1/assemble/assemble.mp4",
        "1/assemble/assemble_loud.mp4",
        job_timeout=STAGE_CONFIG["loudness"]["job_timeout"],
    )


def test_job_loudness_success_advances_to_transcoding(pending_talk):
    """job_loudness executes normalize, advances talk to transcoding, and enqueues job_transcode."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_concat.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize") as mock_normalize,
        patch("app.tasks.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        job_loudness(
            talk_id=1,
            cut_key="1/assemble/assemble.mp4",
            loud_key="1/assemble/assemble_loud.mp4",
        )

    mock_normalize.assert_called_once()
    assert pending_talk.status == "transcoding"
    assert any(
        j.kind == "loudness" and j.status == "done" for j in db_ctx.jobs_dict.values()
    )
    mock_heavy_enqueue.assert_called_once_with(
        job_transcode,
        1,
        "1/assemble/assemble_loud.mp4",
        "1/final/final.mp4",
        job_timeout=STAGE_CONFIG["transcode"]["job_timeout"],
    )


def test_job_loudness_failure_marks_talk_broken(pending_talk):
    """job_loudness failure invokes _handle_failure and marks talk broken."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_concat.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize", side_effect=RuntimeError("Loudnorm filter error")),
        patch("app.tasks._handle_failure") as mock_fail,
        pytest.raises(RuntimeError, match="Loudnorm filter error"),
    ):
        job_loudness(
            talk_id=1,
            cut_key="1/assemble/assemble.mp4",
            loud_key="1/assemble/assemble_loud.mp4",
        )

    assert mock_fail.call_count == 1
    call_args = mock_fail.call_args[0]
    assert call_args[0] == 1  # talk_id
    assert call_args[1] == 100  # job_id assigned by MockSessionContext


def test_job_concat_failure_marks_talk_broken(pending_talk):
    """job_concat failure invokes _handle_failure and marks talk broken."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=RuntimeError("FFmpeg concat failure")),
        patch("app.tasks._handle_failure") as mock_fail,
        pytest.raises(RuntimeError, match="FFmpeg concat failure"),
    ):
        job_concat(
            talk_id=1,
            cut_key="1/cut/cut.mp4",
            intro_key=None,
            outro_key=None,
            concat_key="1/assemble/assemble.mp4",
        )

    assert mock_fail.call_count == 1
    call_args = mock_fail.call_args[0]
    assert call_args[0] == 1  # talk_id
    assert call_args[1] == 100  # job_id assigned by MockSessionContext


def test_job_intro_assembling_invokes_dispatch_assembly(pending_talk):
    """job_intro in assembling status completes and triggers dispatch_assembly without DetachedInstanceError."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_intro_clip"),
        patch("app.tasks.dispatch_assembly") as mock_dispatch,
    ):
        job_intro(1, cut_key="1/cut/cut.mp4", intro_key="1/intro/intro.mp4")

    assert any(
        j.kind == "intro" and j.status == "done" for j in db_ctx.jobs_dict.values()
    )
    mock_dispatch.assert_called_once_with(1, "1/cut/cut.mp4")


def test_job_outro_assembling_invokes_dispatch_assembly(pending_talk):
    """job_outro in assembling status completes and triggers dispatch_assembly without DetachedInstanceError."""
    pending_talk.status = "assembling"
    db_ctx = MockSessionContext(pending_talk)
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_outro_clip"),
        patch("app.tasks.dispatch_assembly") as mock_dispatch,
    ):
        job_outro(1, cut_key="1/cut/cut.mp4", outro_key="1/outro/outro.mp4")

    assert any(
        j.kind == "outro" and j.status == "done" for j in db_ctx.jobs_dict.values()
    )
    mock_dispatch.assert_called_once_with(1, "1/cut/cut.mp4")


def test_configure_assembly_dispatch_failure_advances_to_broken(
    mock_db, auth_client, fake_storage, pending_talk
):
    """When dispatch_assembly fails, talk status advances to broken instead of being left in assembling."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.routes.talks.dispatch_assembly",
            side_effect=RuntimeError("Redis connection lost"),
        ),
        pytest.raises(RuntimeError, match="Redis connection lost"),
    ):
        client.post(
            "/talks/1/assemble",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": "valid_key"},
        )

    assert pending_talk.status == "broken"
    assert any(
        isinstance(call.args[0], models.Job)
        and call.args[0].kind == "assembly"
        and call.args[0].status == "failed"
        and call.args[0].log_path == "1/logs/assembly.log"
        for call in mock_db.add.call_args_list
    )
    assert fake_storage.exists("1/logs/assembly.log")
    assert (
        b"Redis connection lost" in fake_storage.get("1/logs/assembly.log").read_bytes()
    )


def test_configure_assembly_locks_talk_row_with_for_update(
    mock_db, auth_client, fake_storage, pending_talk
):
    """POST /talks/{id}/assemble acquires row-level lock via with_for_update() on talk query."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.dispatch_assembly") as mock_dispatch:
        response = client.post(
            "/talks/1/assemble",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": "valid_key"},
        )
    assert response.status_code == 202
    mock_db.query.return_value.filter.return_value.with_for_update.assert_called_once()
    mock_dispatch.assert_called_once_with(1, "1/cut/cut.mp4")


def test_upload_bumper_file_success(mock_db, auth_client, pending_talk):
    """POST /talks/{id}/bumpers/upload saves bumper file and returns server path."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/bumpers/upload",
        files={"file": ("test_intro.mp4", b"dummy video bytes", "video/mp4")},
        data={"kind": "intro"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["filename"] == "test_intro.mp4"
    assert data["path"].startswith("bumpers/bumper_1_intro_")
    staged_file = get_bumper_staging_dir() / Path(data["path"]).name
    assert staged_file.is_file()
    staged_file.unlink(missing_ok=True)


def test_upload_bumper_file_replaces_previous_staged_file(
    mock_db, auth_client, pending_talk
):
    """A new upload removes an older unsubmitted bumper of the same kind."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db

    staging_dir = get_bumper_staging_dir()
    previous_file = staging_dir / "bumper_1_intro_previous.mp4"
    previous_file.write_bytes(b"old bumper")
    try:
        response = client.post(
            "/talks/1/bumpers/upload",
            files={"file": ("test_intro.mp4", b"new bumper", "video/mp4")},
            data={"kind": "intro"},
            headers={"X-API-Key": "valid_key"},
        )

        assert response.status_code == 200
        assert not previous_file.exists()
        staged_file = staging_dir / Path(response.json()["path"]).name
        assert staged_file.is_file()
        staged_file.unlink(missing_ok=True)
    finally:
        previous_file.unlink(missing_ok=True)


def test_upload_bumper_file_exceeds_max_size(
    mock_db, auth_client, pending_talk, monkeypatch
):
    """POST /talks/{id}/bumpers/upload exceeding max size returns 413 and unlinks partial file."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    monkeypatch.setattr(settings, "max_bumper_upload_size_bytes", 10)

    response = client.post(
        "/talks/1/bumpers/upload",
        files={
            "file": (
                "large_bumper.mp4",
                b"this payload is longer than 10 bytes",
                "video/mp4",
            )
        },
        data={"kind": "intro"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 413
    assert "exceeds maximum allowed size" in response.json()["detail"]


def test_settings_max_bumper_upload_size_strictly_positive():
    """Settings rejects zero and negative values for max_bumper_upload_size_bytes."""
    with pytest.raises(ValidationError):
        Settings(max_bumper_upload_size_bytes=0)

    with pytest.raises(ValidationError):
        Settings(max_bumper_upload_size_bytes=-100)

    valid = Settings(max_bumper_upload_size_bytes=1024)
    assert valid.max_bumper_upload_size_bytes == 1024


def test_upload_bumper_file_invalid_kind(mock_db, auth_client, pending_talk):
    """POST /talks/{id}/bumpers/upload with invalid kind returns 400."""
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/1/bumpers/upload",
        files={"file": ("test.mp4", b"dummy video bytes", "video/mp4")},
        data={"kind": "unsupported"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Bumper kind must be 'intro' or 'outro'" in response.json()["detail"]


def test_upload_bumper_file_talk_not_found(mock_db, auth_client):
    """POST /talks/{id}/bumpers/upload for nonexistent talk returns 404."""
    mock_db.query.return_value.filter.return_value.first.return_value = None
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db

    response = client.post(
        "/talks/999/bumpers/upload",
        files={"file": ("test.mp4", b"dummy bytes", "video/mp4")},
        data={"kind": "intro"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404


def test_upload_bumper_and_assemble_flow(
    mock_db, auth_client, fake_storage, pending_talk, tmp_path
):
    """Uploaded custom bumper can be passed directly to /assemble."""
    valid_clip = generate_clip(0.5, output_dir=tmp_path)
    mock_db.query.return_value.filter.return_value.first.return_value = pending_talk
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with open(valid_clip, "rb") as f:
        upload_resp = client.post(
            "/talks/1/bumpers/upload",
            files={"file": ("custom_intro.mp4", f, "video/mp4")},
            data={"kind": "intro"},
            headers={"X-API-Key": "valid_key"},
        )
    assert upload_resp.status_code == 200
    uploaded_path = upload_resp.json()["path"]

    db_ctx = MockSessionContext(pending_talk)
    with (
        patch("app.tasks.SessionLocal", side_effect=db_ctx),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        assemble_resp = client.post(
            "/talks/1/assemble",
            json={
                "include_intro": True,
                "intro_source": "custom",
                "custom_intro_path": uploaded_path,
                "include_outro": False,
            },
            headers={"X-API-Key": "valid_key"},
        )
    assert assemble_resp.status_code == 202
    assert fake_storage.exists("1/intro/intro.mp4")
    assert pending_talk.status == "assembling"
    mock_enqueue.assert_called_once()
    Path(uploaded_path).unlink(missing_ok=True)


def test_format_timecode_filter_rollover():
    """format_timecode_filter handles centisecond rollover without emitting .100."""
    from app.ui.templating import format_timecode_filter

    assert format_timecode_filter(59.996) == "00:01:00.00"
    assert format_timecode_filter(3599.999) == "01:00:00.00"
    assert format_timecode_filter(0) == "00:00:00.00"
    assert format_timecode_filter(None) == "00:00:00.00"
    assert format_timecode_filter(-1) == "00:00:00.00"
    assert format_timecode_filter(12.34) == "00:00:12.34"
