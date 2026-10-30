"""Tests for assembly configuration endpoint (POST /talks/{talk_id}/assemble) and assembly workflow."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app import models
from app.auth import get_client
from app.config import Settings, settings
from app.consts import TalkStatus
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
from tests.conftest import FakeStorageBackend, TestSessionLocal, generate_clip

client = TestClient(app)


def _worker_session_factory(db_session: Session) -> Callable[[], Session]:
    """Return a factory of job-worker sessions bound to the test connection."""

    def factory() -> Session:
        return TestSessionLocal(bind=db_session.get_bind())

    return factory


@pytest.fixture
def fake_storage() -> FakeStorageBackend:
    return FakeStorageBackend()


def _create_stored_talk(db: Session, **overrides: Any) -> models.Talk:
    event_row = models.Event(name="Intro/Outro Test Event")
    db.add(event_row)
    db.commit()
    db.refresh(event_row)

    talk = models.Talk(
        event_id=event_row.id,
        title=overrides.pop("title", "Keynote on Scalable Systems"),
        room="Auditorium A",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status=overrides.pop("status", "pending_intro_outro"),
        cut_start=0.0,
        cut_end=1800.0,
        raw_duration_seconds=1800.0,
        **overrides,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


@pytest.fixture
def stored_talk(db_session: Session, fake_storage: FakeStorageBackend) -> models.Talk:
    talk = _create_stored_talk(db_session)
    fake_storage.put(f"{talk.id}/cut/cut.mp4", b"fake cut video bytes")
    return talk


@pytest.fixture
def auth_client(stored_talk: models.Talk) -> models.Client:
    return models.Client(
        id=1, hashed_key="valid_hash", event_ids=[stored_talk.event_id]
    )


def test_intro_outro_unauthorized() -> None:
    """POST /talks/{id}/assemble without X-API-Key returns 401."""
    response = client.post("/talks/1/assemble", json={})
    assert response.status_code == 401


def test_intro_outro_talk_not_found(
    db_session: Session, auth_client: models.Client, fake_storage: FakeStorageBackend
) -> None:
    """POST /talks/{id}/assemble for nonexistent talk returns 404."""
    missing_id = (db_session.scalar(select(func.max(models.Talk.id))) or 0) + 1
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{missing_id}/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Talk not found"


def test_intro_outro_unauthorized_event(
    fake_storage: FakeStorageBackend, stored_talk: models.Talk
) -> None:
    """POST /talks/{id}/assemble for talk outside client's event_ids returns 404."""
    unauthorized_client = models.Client(
        id=2, hashed_key="other_hash", event_ids=[stored_talk.event_id + 100]
    )
    app.dependency_overrides[get_client] = lambda: unauthorized_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
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
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    invalid_status: str,
) -> None:
    """POST /talks/{id}/assemble when talk status != 'pending_intro_outro' returns 409."""
    stored_talk.status = invalid_status
    db_session.commit()
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 409
    assert "pending_intro_outro" in response.json()["detail"]


@pytest.mark.parametrize("empty_path", [None, "", "   "])
def test_intro_outro_missing_custom_intro_path_returns_422(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    empty_path: str | None,
) -> None:
    """Custom intro with empty/missing path fails Pydantic validation with 422."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": empty_path,
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


@pytest.mark.parametrize("empty_path", [None, "", "   "])
def test_intro_outro_missing_custom_outro_path_returns_422(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    empty_path: str | None,
) -> None:
    """Custom outro with empty/missing path fails Pydantic validation with 422."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_outro": True,
            "outro_source": "custom",
            "custom_outro_path": empty_path,
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_invalid_source_value(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """Invalid source literal returns 422 Unprocessable Content."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_intro": True,
            "intro_source": "third_party",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 422


def test_intro_outro_no_cut_recording(
    db_session: Session, auth_client: models.Client, stored_talk: models.Talk
) -> None:
    """When talk has no cut recording in storage, returns 400 Bad Request."""
    empty_storage = FakeStorageBackend()
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: empty_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={"include_intro": False, "include_outro": False},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "No cut recording found" in response.json()["detail"]
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_custom_intro_outside_roots(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """Custom intro path outside ingest_roots returns 400 with no side effects."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": "/etc/passwd",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Intro clip rejected" in response.json()["detail"]
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_custom_outro_outside_roots(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """Custom outro path outside ingest_roots returns 400 with no side effects."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_outro": True,
            "outro_source": "custom",
            "custom_outro_path": "/etc/shadow",
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Outro clip rejected" in response.json()["detail"]
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_custom_path_corrupt_or_not_found(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom path inside ingest_roots but nonexistent or invalid video returns 400."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    non_video = tmp_path / "corrupt.txt"
    non_video.write_text("not a video")

    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    response = client.post(
        f"/talks/{stored_talk.id}/assemble",
        json={
            "include_intro": True,
            "intro_source": "custom",
            "custom_intro_path": str(non_video),
        },
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Intro clip rejected" in response.json()["detail"]
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.PENDING_INTRO_OUTRO


def test_intro_outro_skip_both_success(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """Skipping both intro and outro advances talk to assembling and enqueues job_concat directly."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
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

    db_session.expire_all()
    persisted = db_session.get(models.Talk, stored_talk.id)
    assert persisted is not None
    assert persisted.status == "assembling"
    assert persisted.include_intro is False
    assert persisted.include_outro is False

    mock_enqueue.assert_called_once_with(
        job_concat,
        stored_talk.id,
        f"{stored_talk.id}/cut/cut.mp4",
        None,
        None,
        f"{stored_talk.id}/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_intro_outro_generated_both_success(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """Generating both intro and outro advances talk to assembling and enqueues job_intro first."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
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

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.ASSEMBLING

    mock_enqueue.assert_called_once_with(
        job_intro,
        stored_talk.id,
        f"{stored_talk.id}/cut/cut.mp4",
        f"{stored_talk.id}/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_intro_outro_custom_intro_stages_and_enqueues_outro(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom intro stages the clip and immediately triggers job_outro if outro is generated."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_intro = generate_clip(0.5, output_dir=tmp_path)

    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
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
    assert fake_storage.exists(f"{stored_talk.id}/intro/intro.mp4")
    db_session.expire_all()
    persisted = db_session.get(models.Talk, stored_talk.id)
    assert persisted is not None
    assert persisted.status == "assembling"
    assert persisted.custom_intro_path == str(valid_intro)

    mock_enqueue.assert_called_once_with(
        job_outro,
        stored_talk.id,
        f"{stored_talk.id}/cut/cut.mp4",
        f"{stored_talk.id}/outro/outro.mp4",
        job_timeout=STAGE_CONFIG["outro"]["job_timeout"],
    )


def test_intro_outro_custom_outro_stages_and_enqueues_intro(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom outro stages the clip and starts job_intro first if intro is generated."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_outro = generate_clip(0.5, output_dir=tmp_path)

    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
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
    assert fake_storage.exists(f"{stored_talk.id}/outro/outro.mp4")
    db_session.expire_all()
    persisted = db_session.get(models.Talk, stored_talk.id)
    assert persisted is not None
    assert persisted.status == "assembling"
    assert persisted.custom_outro_path == str(valid_outro)

    mock_enqueue.assert_called_once_with(
        job_intro,
        stored_talk.id,
        f"{stored_talk.id}/cut/cut.mp4",
        f"{stored_talk.id}/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_intro_outro_custom_both_stages_and_enqueues_concat(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providing custom clips for both stages both and enqueues job_concat directly."""
    monkeypatch.setattr(settings, "ingest_roots", [str(tmp_path)])
    valid_intro = generate_clip(0.5, output_dir=tmp_path)
    valid_outro = generate_clip(0.5, output_dir=tmp_path)

    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
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
    assert fake_storage.exists(f"{stored_talk.id}/intro/intro.mp4")
    assert fake_storage.exists(f"{stored_talk.id}/outro/outro.mp4")
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.ASSEMBLING

    mock_enqueue.assert_called_once_with(
        job_concat,
        stored_talk.id,
        f"{stored_talk.id}/cut/cut.mp4",
        f"{stored_talk.id}/intro/intro.mp4",
        f"{stored_talk.id}/outro/outro.mp4",
        f"{stored_talk.id}/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_intro_outro_custom_cut_key_threading(
    db_session: Session,
    fake_storage: FakeStorageBackend,
    auth_client: models.Client,
    stored_talk: models.Talk,
) -> None:
    """Custom cut key prefix in storage is threaded to the enqueued task."""
    fake_storage.delete(f"{stored_talk.id}/cut/cut.mp4")
    fake_storage.put(
        f"{stored_talk.id}/cut/recording_custom_bounds.mp4", b"special cut content"
    )

    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
            json={
                "include_intro": False,
                "include_outro": False,
            },
            headers={"X-API-Key": "valid_key"},
        )

    assert response.status_code == 202
    mock_enqueue.assert_called_once_with(
        job_concat,
        stored_talk.id,
        f"{stored_talk.id}/cut/recording_custom_bounds.mp4",
        None,
        None,
        f"{stored_talk.id}/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_dispatch_assembly_both_done_enqueues_job_concat(db_session: Session) -> None:
    """dispatch_assembly enqueues job_concat when both intro and outro jobs are completed."""
    talk = _create_stored_talk(
        db_session,
        status="assembling",
        include_intro=True,
        intro_source="generated",
        include_outro=True,
        outro_source="generated",
    )
    db_session.add_all(
        [
            models.Job(talk_id=talk.id, kind="intro", status="done"),
            models.Job(talk_id=talk.id, kind="outro", status="done"),
        ]
    )
    db_session.commit()

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        dispatch_assembly(talk.id, cut_key=f"{talk.id}/cut/cut.mp4")

    mock_enqueue.assert_called_once_with(
        job_concat,
        talk.id,
        f"{talk.id}/cut/cut.mp4",
        f"{talk.id}/intro/intro.mp4",
        f"{talk.id}/outro/outro.mp4",
        f"{talk.id}/assemble/assemble.mp4",
        job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
    )


def test_job_concat_success_enqueues_job_loudness(db_session: Session) -> None:
    """job_concat executes concat, leaves talk in assembling, and enqueues job_loudness."""
    talk = _create_stored_talk(db_session, status="assembling")

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat") as mock_concat,
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        job_concat(
            talk_id=talk.id,
            cut_key=f"{talk.id}/cut/cut.mp4",
            intro_key=None,
            outro_key=None,
            concat_key=f"{talk.id}/assemble/assemble.mp4",
        )

    mock_concat.assert_called_once_with(
        cut_path=Path("/tmp/fake_cut.mp4"),
        intro_path=None,
        outro_path=None,
        output_path=f"{talk.id}/assemble/assemble.mp4",
        backend=mock_storage,
    )
    db_session.expire_all()
    reloaded = db_session.get(models.Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == "assembling"
    job = db_session.scalars(
        select(models.Job).where(
            models.Job.talk_id == talk.id, models.Job.kind == "concat"
        )
    ).one()
    assert job.status == "done"
    mock_light_enqueue.assert_called_once_with(
        job_loudness,
        talk.id,
        f"{talk.id}/assemble/assemble.mp4",
        f"{talk.id}/assemble/assemble_loud.mp4",
        job_timeout=STAGE_CONFIG["loudness"]["job_timeout"],
    )


def test_job_loudness_success_advances_to_transcoding(db_session: Session) -> None:
    """job_loudness executes normalize, advances talk to transcoding, and enqueues job_transcode."""
    talk = _create_stored_talk(db_session, status="assembling")

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_concat.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize") as mock_normalize,
        patch("app.tasks.heavy_queue.enqueue") as mock_heavy_enqueue,
    ):
        job_loudness(
            talk_id=talk.id,
            cut_key=f"{talk.id}/assemble/assemble.mp4",
            loud_key=f"{talk.id}/assemble/assemble_loud.mp4",
        )

    mock_normalize.assert_called_once()
    db_session.expire_all()
    reloaded = db_session.get(models.Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == "transcoding"
    job = db_session.scalars(
        select(models.Job).where(
            models.Job.talk_id == talk.id, models.Job.kind == "loudness"
        )
    ).one()
    assert job.status == "done"
    mock_heavy_enqueue.assert_called_once_with(
        job_transcode,
        talk.id,
        f"{talk.id}/assemble/assemble_loud.mp4",
        f"{talk.id}/final/final.mp4",
        job_timeout=STAGE_CONFIG["transcode"]["job_timeout"],
    )


def test_job_loudness_failure_marks_talk_broken(db_session: Session) -> None:
    """job_loudness failure invokes _handle_failure and marks talk broken."""
    talk = _create_stored_talk(db_session, status="assembling")

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_concat.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize", side_effect=RuntimeError("Loudnorm filter error")),
        patch("app.tasks._handle_failure") as mock_fail,
        pytest.raises(RuntimeError, match="Loudnorm filter error"),
    ):
        job_loudness(
            talk_id=talk.id,
            cut_key=f"{talk.id}/assemble/assemble.mp4",
            loud_key=f"{talk.id}/assemble/assemble_loud.mp4",
        )

    assert mock_fail.call_count == 1
    call_args = mock_fail.call_args[0]
    assert call_args[0] == talk.id
    db_session.expire_all()
    job = db_session.scalars(
        select(models.Job).where(models.Job.talk_id == talk.id)
    ).one()
    assert call_args[1] == job.id


def test_job_concat_failure_marks_talk_broken(db_session: Session) -> None:
    """job_concat failure invokes _handle_failure and marks talk broken."""
    talk = _create_stored_talk(db_session, status="assembling")

    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.concat", side_effect=RuntimeError("FFmpeg concat failure")),
        patch("app.tasks._handle_failure") as mock_fail,
        pytest.raises(RuntimeError, match="FFmpeg concat failure"),
    ):
        job_concat(
            talk_id=talk.id,
            cut_key=f"{talk.id}/cut/cut.mp4",
            intro_key=None,
            outro_key=None,
            concat_key=f"{talk.id}/assemble/assemble.mp4",
        )

    assert mock_fail.call_count == 1
    call_args = mock_fail.call_args[0]
    assert call_args[0] == talk.id
    db_session.expire_all()
    job = db_session.scalars(
        select(models.Job).where(models.Job.talk_id == talk.id)
    ).one()
    assert call_args[1] == job.id


def test_job_intro_assembling_invokes_dispatch_assembly(db_session: Session) -> None:
    """job_intro in assembling status completes and triggers dispatch_assembly without DetachedInstanceError."""
    talk = _create_stored_talk(db_session, status="assembling")
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_intro_clip"),
        patch("app.tasks.dispatch_assembly") as mock_dispatch,
    ):
        job_intro(
            talk.id,
            cut_key=f"{talk.id}/cut/cut.mp4",
            intro_key=f"{talk.id}/intro/intro.mp4",
        )

    db_session.expire_all()
    job = db_session.scalars(
        select(models.Job).where(
            models.Job.talk_id == talk.id, models.Job.kind == "intro"
        )
    ).one()
    assert job.status == "done"
    mock_dispatch.assert_called_once_with(talk.id, f"{talk.id}/cut/cut.mp4")


def test_job_outro_assembling_invokes_dispatch_assembly(db_session: Session) -> None:
    """job_outro in assembling status completes and triggers dispatch_assembly without DetachedInstanceError."""
    talk = _create_stored_talk(db_session, status="assembling")
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake_cut.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_outro_clip"),
        patch("app.tasks.dispatch_assembly") as mock_dispatch,
    ):
        job_outro(
            talk.id,
            cut_key=f"{talk.id}/cut/cut.mp4",
            outro_key=f"{talk.id}/outro/outro.mp4",
        )

    db_session.expire_all()
    job = db_session.scalars(
        select(models.Job).where(
            models.Job.talk_id == talk.id, models.Job.kind == "outro"
        )
    ).one()
    assert job.status == "done"
    mock_dispatch.assert_called_once_with(talk.id, f"{talk.id}/cut/cut.mp4")


def test_configure_assembly_dispatch_failure_advances_to_broken(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """When dispatch_assembly fails, talk status advances to broken instead of being left in assembling."""
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with (
        patch(
            "app.routes.talks.dispatch_assembly",
            side_effect=RuntimeError("Redis connection lost"),
        ),
        pytest.raises(RuntimeError, match="Redis connection lost"),
    ):
        client.post(
            f"/talks/{stored_talk.id}/assemble",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": "valid_key"},
        )

    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.BROKEN
    failed_job = db_session.scalar(
        select(models.Job).where(
            models.Job.talk_id == stored_talk.id,
            models.Job.kind == "assembly",
            models.Job.status == "failed",
        )
    )
    assert failed_job is not None
    assert failed_job.log_path == f"{stored_talk.id}/logs/assembly.log"
    assert fake_storage.exists(f"{stored_talk.id}/logs/assembly.log")
    assert (
        b"Redis connection lost"
        in fake_storage.get(f"{stored_talk.id}/logs/assembly.log").read_bytes()
    )


def test_configure_assembly_locks_talk_row_with_for_update(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
) -> None:
    """POST /talks/{id}/assemble acquires row-level lock via with_for_update() on talk query."""
    statements: list[str] = []

    def record_statement(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(db_session.get_bind(), "before_cursor_execute", record_statement)
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with patch("app.routes.talks.dispatch_assembly") as mock_dispatch:
        response = client.post(
            f"/talks/{stored_talk.id}/assemble",
            json={"include_intro": False, "include_outro": False},
            headers={"X-API-Key": "valid_key"},
        )
    assert response.status_code == 202
    assert any("FOR UPDATE" in statement.upper() for statement in statements)
    mock_dispatch.assert_called_once_with(
        stored_talk.id, f"{stored_talk.id}/cut/cut.mp4"
    )


def test_upload_bumper_file_success(
    auth_client: models.Client, stored_talk: models.Talk
) -> None:
    """POST /talks/{id}/bumpers/upload saves bumper file and returns server path."""
    app.dependency_overrides[get_client] = lambda: auth_client

    response = client.post(
        f"/talks/{stored_talk.id}/bumpers/upload",
        files={"file": ("test_intro.mp4", b"dummy video bytes", "video/mp4")},
        data={"kind": "intro"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["filename"] == "test_intro.mp4"
    assert data["path"].startswith(f"bumpers/bumper_{stored_talk.id}_intro_")
    staged_file = get_bumper_staging_dir() / Path(data["path"]).name
    assert staged_file.is_file()
    staged_file.unlink(missing_ok=True)


def test_upload_bumper_file_replaces_previous_staged_file(
    auth_client: models.Client, stored_talk: models.Talk
) -> None:
    """A new upload removes an older unsubmitted bumper of the same kind."""
    app.dependency_overrides[get_client] = lambda: auth_client

    staging_dir = get_bumper_staging_dir()
    previous_file = staging_dir / f"bumper_{stored_talk.id}_intro_previous.mp4"
    previous_file.write_bytes(b"old bumper")
    try:
        response = client.post(
            f"/talks/{stored_talk.id}/bumpers/upload",
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
    auth_client: models.Client,
    stored_talk: models.Talk,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /talks/{id}/bumpers/upload exceeding max size returns 413 and unlinks partial file."""
    app.dependency_overrides[get_client] = lambda: auth_client
    monkeypatch.setattr(settings, "max_bumper_upload_size_bytes", 10)

    response = client.post(
        f"/talks/{stored_talk.id}/bumpers/upload",
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


def test_settings_max_bumper_upload_size_strictly_positive() -> None:
    """Settings rejects zero and negative values for max_bumper_upload_size_bytes."""
    with pytest.raises(ValidationError):
        Settings(max_bumper_upload_size_bytes=0)

    with pytest.raises(ValidationError):
        Settings(max_bumper_upload_size_bytes=-100)

    valid = Settings(max_bumper_upload_size_bytes=1024)
    assert valid.max_bumper_upload_size_bytes == 1024


def test_upload_bumper_file_invalid_kind(
    auth_client: models.Client, stored_talk: models.Talk
) -> None:
    """POST /talks/{id}/bumpers/upload with invalid kind returns 400."""
    app.dependency_overrides[get_client] = lambda: auth_client

    response = client.post(
        f"/talks/{stored_talk.id}/bumpers/upload",
        files={"file": ("test.mp4", b"dummy video bytes", "video/mp4")},
        data={"kind": "unsupported"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 400
    assert "Bumper kind must be 'intro' or 'outro'" in response.json()["detail"]


def test_upload_bumper_file_talk_not_found(
    db_session: Session, auth_client: models.Client
) -> None:
    """POST /talks/{id}/bumpers/upload for nonexistent talk returns 404."""
    missing_id = (db_session.scalar(select(func.max(models.Talk.id))) or 0) + 1
    app.dependency_overrides[get_client] = lambda: auth_client

    response = client.post(
        f"/talks/{missing_id}/bumpers/upload",
        files={"file": ("test.mp4", b"dummy bytes", "video/mp4")},
        data={"kind": "intro"},
        headers={"X-API-Key": "valid_key"},
    )
    assert response.status_code == 404


def test_upload_bumper_and_assemble_flow(
    db_session: Session,
    auth_client: models.Client,
    fake_storage: FakeStorageBackend,
    stored_talk: models.Talk,
    tmp_path: Path,
) -> None:
    """Uploaded custom bumper can be passed directly to /assemble."""
    valid_clip = generate_clip(0.5, output_dir=tmp_path)
    app.dependency_overrides[get_client] = lambda: auth_client
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    with open(valid_clip, "rb") as f:
        upload_resp = client.post(
            f"/talks/{stored_talk.id}/bumpers/upload",
            files={"file": ("custom_intro.mp4", f, "video/mp4")},
            data={"kind": "intro"},
            headers={"X-API-Key": "valid_key"},
        )
    assert upload_resp.status_code == 200
    uploaded_path = upload_resp.json()["path"]

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        assemble_resp = client.post(
            f"/talks/{stored_talk.id}/assemble",
            json={
                "include_intro": True,
                "intro_source": "custom",
                "custom_intro_path": uploaded_path,
                "include_outro": False,
            },
            headers={"X-API-Key": "valid_key"},
        )
    assert assemble_resp.status_code == 202
    assert fake_storage.exists(f"{stored_talk.id}/intro/intro.mp4")
    db_session.expire_all()
    reloaded_talk = db_session.get(models.Talk, stored_talk.id)
    assert reloaded_talk is not None
    assert reloaded_talk.status == TalkStatus.ASSEMBLING
    mock_enqueue.assert_called_once()
    Path(uploaded_path).unlink(missing_ok=True)


def test_format_timecode_filter_rollover() -> None:
    """format_timecode_filter handles centisecond rollover without emitting .100."""
    from app.ui.templating import format_timecode_filter

    assert format_timecode_filter(59.996) == "00:01:00.00"
    assert format_timecode_filter(3599.999) == "01:00:00.00"
    assert format_timecode_filter(0) == "00:00:00.00"
    assert format_timecode_filter(None) == "00:00:00.00"
    assert format_timecode_filter(-1) == "00:00:00.00"
    assert format_timecode_filter(12.34) == "00:00:12.34"
