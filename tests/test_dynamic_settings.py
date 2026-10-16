from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import (
    EXCLUDED_SETTING_KEYS,
    get_setting,
    settings,
)
from app.db import Base
from app.routes.admin import _validate_system_setting

engine = create_engine(settings.database_url)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_system_setting_model_crud(db_session):
    setting = models.SystemSetting(
        key="test_key",
        value="42",
        description="A test setting",
        updated_at=datetime.now(UTC),
    )
    db_session.add(setting)
    db_session.flush()

    retrieved = db_session.get(models.SystemSetting, "test_key")
    assert retrieved is not None
    assert retrieved.key == "test_key"
    assert retrieved.value == "42"
    assert retrieved.description == "A test setting"

    retrieved.value = "99"
    db_session.flush()
    assert db_session.get(models.SystemSetting, "test_key").value == "99"

    db_session.delete(retrieved)
    db_session.flush()
    assert db_session.get(models.SystemSetting, "test_key") is None


def test_get_setting_fallback(db_session):
    # Without any DB override, should return definition default
    keys = (
        "detect_duration_tolerance_seconds",
        "loudness_target_lufs",
        "default_preview_preset",
        "default_transcode_preset",
    )
    db_session.query(models.SystemSetting).filter(
        models.SystemSetting.key.in_(keys)
    ).delete(synchronize_session=False)
    db_session.flush()

    val = get_setting("detect_duration_tolerance_seconds", db=db_session)
    assert val == 300.0
    assert isinstance(val, float)

    val_lufs = get_setting("loudness_target_lufs", db=db_session)
    assert val_lufs == -16.0
    assert isinstance(val_lufs, float)

    val_preset = get_setting("default_preview_preset", db=db_session)
    assert val_preset == "small_video"

    val_transcode = get_setting("default_transcode_preset", db=db_session)
    assert val_transcode == "1080p_default"


def test_get_setting_db_override(db_session):
    # Insert override into DB
    override = models.SystemSetting(
        key="detect_duration_tolerance_seconds",
        value="150.5",
        description="Tighter tolerance",
        updated_at=datetime.now(UTC),
    )
    db_session.add(override)
    db_session.flush()

    # Query with db session
    val = get_setting("detect_duration_tolerance_seconds", db=db_session)
    assert val == 150.5
    assert isinstance(val, float)


def test_dynamic_settings_override(db_session):
    # Test that get_setting respects DB override for default_preview_preset
    override = models.SystemSetting(
        key="default_preview_preset",
        value="big_video",
        description="High quality preview",
        updated_at=datetime.now(UTC),
    )
    db_session.add(override)
    db_session.flush()

    assert get_setting("default_preview_preset", db=db_session) == "big_video"


def test_preview_preset_options_includes_configured_presets(db_session, monkeypatch):
    from app.config import PreviewPreset
    from app.routes.admin import _get_all_settings_data

    custom = dict(settings.preview_presets)
    custom["custom_hd_preview"] = PreviewPreset(
        name="custom_hd_preview",
        resolution=(1280, 720),
        video_bitrate=800_000,
    )
    monkeypatch.setattr(settings, "preview_presets", custom)

    data = _get_all_settings_data(db_session)
    preview_setting = next(s for s in data if s.key == "default_preview_preset")
    option_values = [opt.value for opt in preview_setting.options]

    assert "small_video" in option_values
    assert "big_video" in option_values
    assert "custom_hd_preview" in option_values

    custom_opt = next(
        opt for opt in preview_setting.options if opt.value == "custom_hd_preview"
    )
    assert "1280x720" in custom_opt.label


def test_secrets_exclusion(db_session):
    # Attempting to override a secret key in DB should be ignored by get_setting
    override = models.SystemSetting(
        key="session_secret",
        value="malicious_override_key",
        updated_at=datetime.now(UTC),
    )
    db_session.add(override)
    db_session.flush()

    # Secret is excluded from dynamic resolution
    val = get_setting("session_secret", db=db_session)
    assert val != "malicious_override_key"


def test_validate_system_setting():
    # Valid values
    _validate_system_setting("detect_duration_tolerance_seconds", "60.0")
    _validate_system_setting("detect_duration_tolerance_seconds", "120.0")
    _validate_system_setting("detect_duration_tolerance_seconds", "1800.0")
    _validate_system_setting("loudness_target_lufs", "-23.0")
    _validate_system_setting("speaker_magic_link_expire_hours", "168")
    _validate_system_setting("speaker_magic_link_expire_hours", "24")
    _validate_system_setting("default_preview_preset", "big_video")
    for preset_name in ("480p", "720p", "1080p_default", "1440p"):
        _validate_system_setting("default_transcode_preset", preset_name)

    # Mixed-case keys must be accepted and normalized
    _validate_system_setting("Detect_Duration_Tolerance_Seconds", "120.0")
    _validate_system_setting("DEFAULT_PREVIEW_PRESET", "big_video")

    # Excluded secret
    for secret in EXCLUDED_SETTING_KEYS:
        with pytest.raises(HTTPException) as exc:
            _validate_system_setting(secret, "foo")
        assert exc.value.status_code == 400

    # Unknown key rejected
    with pytest.raises(HTTPException):
        _validate_system_setting("unknown_arbitrary_key", "foo")

    # Tolerance out-of-range (below 60.0 or above 1800.0)
    for bad_tol in ("-10.0", "0.0", "59.9", "1800.1", "invalid_num"):
        with pytest.raises(HTTPException):
            _validate_system_setting("detect_duration_tolerance_seconds", bad_tol)

    # Invalid speaker_magic_link_expire_hours
    for bad_hrs in ("0", "-10", "abc", "1.5"):
        with pytest.raises(HTTPException) as exc_info:
            _validate_system_setting("speaker_magic_link_expire_hours", bad_hrs)
        assert exc_info.value.status_code == 400

    # Invalid LUFS (outside -70.0 to -5.0)
    for bad_lufs in ("-4.9", "-4.0", "0.0", "5.0", "-70.1", "-80.0"):
        with pytest.raises(HTTPException) as exc_info:
            _validate_system_setting("loudness_target_lufs", bad_lufs)
        assert "-70.0 and -5.0" in exc_info.value.detail

    # Invalid presets
    with pytest.raises(HTTPException):
        _validate_system_setting("default_preview_preset", "unknown_preset_xyz")

    with pytest.raises(HTTPException):
        _validate_system_setting("default_transcode_preset", "unknown_transcode_xyz")
    with pytest.raises(HTTPException):
        _validate_system_setting("default_transcode_preset", "4k_master")

    # NaN and Inf injection must be rejected
    for bad_val in ("nan", "NaN", "NAN", "inf", "Inf", "-inf", "+inf"):
        with pytest.raises(HTTPException):
            _validate_system_setting("detect_duration_tolerance_seconds", bad_val)
        with pytest.raises(HTTPException):
            _validate_system_setting("loudness_target_lufs", bad_val)

    # Case-insensitive secret rejection
    with pytest.raises(HTTPException):
        _validate_system_setting("SESSION_SECRET", "val")
    with pytest.raises(HTTPException):
        _validate_system_setting("Database_Url", "val")
    with pytest.raises(HTTPException):
        _validate_system_setting("storage_backend", "val")


def test_cast_setting_value_rejects_nan_inf():
    from app.config import _cast_setting_value

    # For float definitions, NaN or Inf must fall back to default
    assert _cast_setting_value("detect_duration_tolerance_seconds", "nan") == 300.0
    assert _cast_setting_value("detect_duration_tolerance_seconds", "inf") == 300.0
    assert _cast_setting_value("loudness_target_lufs", "nan") == -16.0


def test_get_setting_fallback_default():
    val = get_setting("non_existent_key", default="fallback_val")
    assert val == "fallback_val"


def test_get_setting_db_failure_logged(caplog):
    from unittest.mock import MagicMock

    mock_db = MagicMock()
    mock_db.get.side_effect = RuntimeError("DB connection dropped")

    with caplog.at_level("WARNING"):
        val = get_setting("detect_duration_tolerance_seconds", db=mock_db)

    assert val == 300.0
    assert (
        "Database lookup failed for setting 'detect_duration_tolerance_seconds'"
        in caplog.text
    )
    assert "DB connection dropped" in caplog.text


def test_pipeline_job_detect_dynamic_tolerance():
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from app.pipeline.detect import DetectResult
    from app.tasks import job_detect

    override = models.SystemSetting(
        key="detect_duration_tolerance_seconds",
        value="120.0",
        description="custom tolerance",
        updated_at=datetime.now(UTC),
    )

    talk = models.Talk(
        id=999,
        event_id=1,
        title="Dynamic Settings Talk",
        start=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
        status="detecting",
    )
    job = models.Job(id=101, talk_id=999, kind="detect", status="running")

    mock_detect = MagicMock(
        return_value=DetectResult(
            passed=True,
            actual_duration_seconds=3600.0,
            has_video=True,
            has_audio=True,
            reason=None,
        )
    )
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/fake/path.mp4")

    def mock_db():
        session = MagicMock()
        session.get.side_effect = lambda model, oid: (
            talk if model == models.Talk else (job if model == models.Job else override)
        )
        session.__enter__.return_value = session
        return session

    with (
        patch("app.tasks.SessionLocal", side_effect=mock_db),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", mock_detect),
    ):
        job_detect(999, "999/raw/raw.mp4")

    mock_detect.assert_called_once()
    assert mock_detect.call_args.kwargs.get("tolerance_seconds") == 120.0


def test_pipeline_job_loudness_dynamic_lufs():
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from app.tasks import job_loudness

    override = models.SystemSetting(
        key="loudness_target_lufs",
        value="-23.0",
        description="EBU standard",
        updated_at=datetime.now(UTC),
    )

    talk = models.Talk(
        id=999,
        event_id=1,
        title="Dynamic Loudness Talk",
        start=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
        status="assembling",
    )
    job = models.Job(id=102, talk_id=999, kind="loudness", status="running")

    mock_normalize = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/fake/path.mp4")

    def mock_db():
        session = MagicMock()
        session.get.side_effect = lambda model, oid: (
            talk if model == models.Talk else (job if model == models.Job else override)
        )
        session.__enter__.return_value = session
        return session

    with (
        patch("app.tasks.SessionLocal", side_effect=mock_db),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.normalize", mock_normalize),
        patch("app.tasks.heavy_queue.enqueue"),
    ):
        job_loudness(999, "999/cut/cut.mp4")

    mock_normalize.assert_called_once()
    assert mock_normalize.call_args.kwargs.get("target_lufs") == -23.0


def test_pipeline_job_preview_dynamic_preset():
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from app.config import PREVIEW_PRESETS
    from app.tasks import job_preview

    override = models.SystemSetting(
        key="default_preview_preset",
        value="big_video",
        description="custom preview preset",
        updated_at=datetime.now(UTC),
    )

    talk = models.Talk(
        id=999,
        event_id=1,
        title="Dynamic Preview Talk",
        start=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
        status="generating_previews",
    )
    job = models.Job(id=103, talk_id=999, kind="preview", status="running")

    mock_preview = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/fake/path.mp4")
    mock_storage.exists.return_value = False

    session = MagicMock()
    session.__enter__.return_value = session
    session.query.return_value.filter.return_value.all.return_value = []

    def mock_get(model, ident):
        if model is models.Talk:
            return talk
        if model is models.Job:
            return job
        if model is models.SystemSetting and ident == override.key:
            return override
        return None

    session.get.side_effect = mock_get

    with (
        patch("app.tasks.SessionLocal", return_value=session),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.generate_preview", mock_preview),
        patch("app.tasks._cache_waveform"),
    ):
        job_preview(999, "999/cut/cut.mp4")

    mock_preview.assert_called_once()
    assert mock_preview.call_args.kwargs.get("preset") == PREVIEW_PRESETS["big_video"]
    assert talk.status == "preview"
    assert job.status == "done"


def test_pipeline_job_transcode_dynamic_preset():
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from app.tasks import TRANSCODE_PRESETS, job_transcode

    override = models.SystemSetting(
        key="default_transcode_preset",
        value="720p",
        description="custom transcode preset",
        updated_at=datetime.now(UTC),
    )

    talk = models.Talk(
        id=999,
        event_id=1,
        title="Dynamic Transcode Talk",
        start=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
        end=datetime(2026, 3, 20, 11, 0, tzinfo=UTC),
        status="transcoding",
    )
    job = models.Job(id=104, talk_id=999, kind="transcode", status="running")

    mock_transcode = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/fake/path.mp4")

    session = MagicMock()
    session.__enter__.return_value = session

    def mock_get(model, ident):
        if model is models.Talk:
            return talk
        if model is models.Job:
            return job
        if model is models.SystemSetting and ident == override.key:
            return override
        return None

    session.get.side_effect = mock_get

    with (
        patch("app.tasks.SessionLocal", return_value=session),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.transcode", mock_transcode),
        patch("app.tasks.light_queue.enqueue"),
    ):
        job_transcode(999, "999/cut/cut_loud.mp4")

    mock_transcode.assert_called_once()
    assert mock_transcode.call_args.kwargs.get("preset") == TRANSCODE_PRESETS["720p"]
    assert talk.status == "uploading"
    assert job.status == "done"
