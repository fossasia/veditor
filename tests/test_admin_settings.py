import math
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app import models
from app.config import settings
from app.db import Base, SessionLocal, engine, get_db
from app.ingest import InsufficientStorageError, stage_recording
from app.main import app
from app.pipeline.detect import DETECT_DURATION_TOLERANCE_SECONDS
from app.pipeline.transcode import PRESET_720P
from app.runtime_settings import RUNTIME_SETTINGS, get_setting
from app.schemas import RecordingIngestRequest
from app.security import create_session_token, hash_password
from app.tasks import job_detect, job_loudness, job_preview, job_transcode

EMAIL_DOMAIN = "@settings-test.com"


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture(autouse=True)
def clean_settings():
    def _wipe():
        with SessionLocal() as db:
            db.query(models.SystemSetting).delete()
            db.query(models.User).filter(
                models.User.email.like(f"%{EMAIL_DOMAIN}")
            ).delete(synchronize_session=False)
            db.commit()

    _wipe()
    yield
    _wipe()


@pytest.fixture
def db_session():
    db = SessionLocal()
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield db
    finally:
        db.rollback()
        app.dependency_overrides.pop(get_db, None)
        db.close()


@pytest.fixture
def client():
    return TestClient(app)


def _login(client: TestClient, db, role: str = "admin") -> models.User:
    user = models.User(
        email=f"{role}{EMAIL_DOMAIN}",
        hashed_password=hash_password("Password123!"),
        role=role,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    client.cookies.set("veditor_session", create_session_token(user.id, user.role))
    return user


def _stored(key: str) -> models.SystemSetting | None:
    with SessionLocal() as db:
        return db.get(models.SystemSetting, key)


def _override(key: str, value) -> None:
    with SessionLocal() as db:
        db.merge(models.SystemSetting(key=key, value=value))
        db.commit()


# --- Access control ---


def test_settings_requires_authentication(client: TestClient):
    assert client.get("/admin/settings").status_code == 401
    res = client.post("/admin/settings/loudness_target_lufs", data={"value": "-14"})
    assert res.status_code == 401


@pytest.mark.parametrize("role", ["user", "organizer"])
def test_settings_forbidden_for_non_admin(client: TestClient, db_session, role):
    _login(client, db_session, role=role)
    assert client.get("/admin/settings").status_code == 403
    res = client.post("/admin/settings/loudness_target_lufs", data={"value": "-14"})
    assert res.status_code == 403
    assert _stored("loudness_target_lufs") is None


# --- UI page ---


def test_settings_page_lists_all_settings_with_defaults(client: TestClient, db_session):
    _login(client, db_session)
    res = client.get("/admin/settings")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    for spec in RUNTIME_SETTINGS.values():
        assert f'id="setting-{spec.key}"' in res.text
        assert spec.label in res.text
    assert 'badge-info">Override' not in res.text
    assert 'id="nav-admin-settings-link"' in res.text


def test_save_setting_persists_and_applies(client: TestClient, db_session):
    admin = _login(client, db_session)
    res = client.post(
        "/admin/settings/loudness_target_lufs",
        data={"value": "-14", "action": "save"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/admin/settings?saved=loudness_target_lufs"

    row = _stored("loudness_target_lufs")
    assert row is not None
    assert row.value == -14.0
    assert row.updated_by_user_id == admin.id
    assert get_setting("loudness_target_lufs") == -14.0

    page = client.get(res.headers["location"])
    assert "Saved Loudness target." in page.text
    assert 'badge-info">Override' in page.text
    assert admin.email in page.text


def test_save_twice_updates_existing_row(client: TestClient, db_session):
    _login(client, db_session)
    for choice in ("4k_master", "720p"):
        res = client.post(
            "/admin/settings/transcode_preset",
            data={"value": choice},
            follow_redirects=False,
        )
        assert res.status_code == 303
    assert get_setting("transcode_preset") == "720p"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("loudness_target_lufs", "5"),
        ("loudness_target_lufs", "-71"),
        ("loudness_target_lufs", "nan"),
        ("loudness_target_lufs", "abc"),
        ("detect_duration_tolerance_seconds", ""),
        ("disk_guard_multiplier", "0.5"),
        ("transcode_preset", "8k_ultra"),
        ("preview_preset", ""),
    ],
)
def test_invalid_value_rejected(client: TestClient, db_session, key, value):
    _login(client, db_session)
    res = client.post(f"/admin/settings/{key}", data={"value": value})
    assert res.status_code == 400
    assert 'role="alert"' in res.text
    assert RUNTIME_SETTINGS[key].label in res.text
    assert _stored(key) is None


def test_unknown_setting_and_action_rejected(client: TestClient, db_session):
    _login(client, db_session)
    res = client.post("/admin/settings/session_secret", data={"value": "x"})
    assert res.status_code == 404
    res = client.post(
        "/admin/settings/loudness_target_lufs", data={"value": "-14", "action": "x"}
    )
    assert res.status_code == 400
    assert _stored("session_secret") is None


def test_reset_removes_override(client: TestClient, db_session):
    _login(client, db_session)
    _override("detect_duration_tolerance_seconds", 42.0)
    assert get_setting("detect_duration_tolerance_seconds") == 42.0

    res = client.post(
        "/admin/settings/detect_duration_tolerance_seconds",
        data={"action": "reset"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert _stored("detect_duration_tolerance_seconds") is None
    assert (
        get_setting("detect_duration_tolerance_seconds")
        == DETECT_DURATION_TOLERANCE_SECONDS
    )


# --- Resolution ---


def test_env_default_used_without_override(monkeypatch):
    monkeypatch.setattr(settings, "disk_guard_multiplier", 7.5)
    assert get_setting("disk_guard_multiplier") == 7.5
    _override("disk_guard_multiplier", 2.0)
    assert get_setting("disk_guard_multiplier") == 2.0


def test_invalid_stored_value_falls_back_to_default():
    _override("loudness_target_lufs", "loud")
    assert get_setting("loudness_target_lufs") == -16.0


def test_database_error_falls_back_to_default():
    broken = MagicMock(side_effect=OperationalError("SELECT", {}, Exception("down")))
    with patch("app.runtime_settings.SessionLocal", broken):
        assert get_setting("transcode_preset") == "1080p_default"


# --- Pipeline picks up overrides on the next run ---


def _session_factory(talk_status: str) -> MagicMock:
    """Stand-in for app.tasks.SessionLocal returning a talk in the given state."""
    talk = MagicMock(status=talk_status, start=None, end=None)
    session = MagicMock()
    session.get.side_effect = lambda model, _id: (
        talk if model is models.Talk else MagicMock(status="running")
    )
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    return MagicMock(return_value=session_cm)


def _run_stage(job, args, talk_status: str, target: str) -> MagicMock:
    """Run a job until its pipeline call, which is stubbed to stop the job there."""
    with (
        patch("app.tasks.SessionLocal", _session_factory(talk_status)),
        patch("app.tasks.get_storage_backend", return_value=MagicMock()),
        patch("app.tasks._handle_failure"),
        patch(target, side_effect=RuntimeError("stop")) as stage,
        pytest.raises(RuntimeError, match="stop"),
    ):
        job(*args)
    return stage


def test_detect_job_uses_override_on_next_run():
    args = (1, "1/raw/raw.mp4")
    stage = _run_stage(job_detect, args, "detecting", "app.tasks.detect")
    assert stage.call_args.kwargs["tolerance_seconds"] == (
        DETECT_DURATION_TOLERANCE_SECONDS
    )

    _override("detect_duration_tolerance_seconds", 900.0)
    stage = _run_stage(job_detect, args, "detecting", "app.tasks.detect")
    assert stage.call_args.kwargs["tolerance_seconds"] == 900.0


def test_preview_job_uses_override():
    _override("preview_preset", "big_video")
    stage = _run_stage(
        job_preview, (1, "1/cut/cut.mp4"), "cutting", "app.tasks.generate_preview"
    )
    assert stage.call_args.kwargs["preset"] is settings.preview_presets["big_video"]


def test_loudness_job_uses_override():
    _override("loudness_target_lufs", -14.0)
    stage = _run_stage(
        job_loudness, (1, "1/cut/cut.mp4"), "assembling", "app.tasks.normalize"
    )
    assert stage.call_args.kwargs["target_lufs"] == -14.0


def test_transcode_job_uses_override():
    _override("transcode_preset", "720p")
    stage = _run_stage(
        job_transcode, (1, "1/cut/cut_loud.mp4"), "transcoding", "app.tasks.transcode"
    )
    assert stage.call_args.kwargs["preset"] is PRESET_720P


def test_ingest_disk_guard_uses_override(tmp_path: Path, monkeypatch):
    target = tmp_path / "video.mp4"
    with av.open(str(target), mode="w") as container:
        stream = container.add_stream("mpeg4", rate=1)
        stream.width, stream.height, stream.pix_fmt = 16, 16, "yuv420p"
        frame = av.VideoFrame.from_ndarray(
            np.zeros((16, 16, 3), dtype=np.uint8), format="rgb24"
        )
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    monkeypatch.setattr(settings, "ingest_roots", [tmp_path])
    size = target.stat().st_size

    backend = MagicMock()
    backend.free_bytes.return_value = math.ceil(size * 4.0)
    payload = RecordingIngestRequest(relative_key="video.mp4")

    _override("disk_guard_multiplier", 5.0)
    with pytest.raises(InsufficientStorageError) as exc_info:
        stage_recording(1, payload, backend)
    assert exc_info.value.required_bytes == math.ceil(size * 5.0)

    _override("disk_guard_multiplier", 4.0)
    assert stage_recording(1, payload, backend) == "1/raw/video.mp4"
