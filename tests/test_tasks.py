from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Job, Talk
from app.pipeline.detect import DetectResult
from app.tasks import (
    STAGE_CONFIG,
    dispatch_assembly,
    job_detect,
    job_ingest,
    job_ingest_room_url,
    job_intro,
    job_outro,
    job_waveform,
)
from tests.conftest import TestSessionLocal, generate_clip


def _worker_session_factory(db_session: Session) -> Callable[[], Session]:
    """Return a factory of job-worker sessions bound to the test connection."""

    def factory() -> Session:
        return TestSessionLocal(bind=db_session.get_bind())

    return factory


@pytest.fixture
def mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.get.return_value = Path("/tmp/fake_media.mp4")
    storage.put.return_value = None
    return storage


def test_job_ingest_success(
    db_session: Session, mock_storage: MagicMock, tmp_path: Path
) -> None:
    event = Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title="Test Talk",
        room="Room A",
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status="waiting_for_files",
        cut_start=0.0,
        cut_end=1800.0,
    )
    db_session.add(talk)
    db_session.commit()

    staged_file = tmp_path / "staged.mp4"
    staged_file.write_bytes(b"dummy video data")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.validate_media_file", return_value=None),
        patch("app.tasks.light_queue.enqueue") as mock_enqueue,
    ):
        job_ingest(talk.id, str(staged_file), f"{talk.id}/raw/raw.mp4")

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == "detecting"
    job = db_session.scalars(select(Job)).one()
    assert job.status == "done"
    assert job.kind == "ingest"
    assert mock_storage.put.call_count == 1
    mock_enqueue.assert_called_once_with(
        job_detect,
        talk.id,
        f"{talk.id}/raw/raw.mp4",
        None,
        job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
    )
    # Staged file is unlinked
    assert not staged_file.exists()


def test_dispatch_assembly_generated_intro_enqueues_job_intro(
    db_session: Session,
) -> None:
    event = Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title="Test Talk",
        room="Room A",
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status="assembling",
        include_intro=True,
        intro_source="generated",
        include_outro=False,
    )
    db_session.add(talk)
    db_session.commit()

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        dispatch_assembly(talk.id, cut_key=f"{talk.id}/cut/special.mp4")

    mock_light_enqueue.assert_called_once_with(
        job_intro,
        talk.id,
        f"{talk.id}/cut/special.mp4",
        f"{talk.id}/intro/intro.mp4",
        job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
    )


def test_dispatch_assembly_intro_done_outro_generated_enqueues_job_outro(
    db_session: Session,
) -> None:
    event = Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title="Test Talk",
        room="Room A",
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status="assembling",
        include_intro=True,
        intro_source="generated",
        include_outro=True,
        outro_source="generated",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.add(Job(talk_id=talk.id, kind="intro", status="done"))
    db_session.commit()

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.light_queue.enqueue") as mock_light_enqueue,
    ):
        dispatch_assembly(talk.id, cut_key=f"{talk.id}/cut/special.mp4")

    mock_light_enqueue.assert_called_once_with(
        job_outro,
        talk.id,
        f"{talk.id}/cut/special.mp4",
        f"{talk.id}/outro/outro.mp4",
        job_timeout=STAGE_CONFIG["outro"]["job_timeout"],
    )


def test_job_waveform_skips_if_waveform_already_exists() -> None:
    mock_storage = MagicMock()
    mock_storage.exists.side_effect = lambda key: key.endswith(".waveform.json")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_skips_if_media_key_missing() -> None:
    mock_storage = MagicMock()
    mock_storage.exists.return_value = False

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_handles_storage_key_not_found_on_get() -> None:
    from app.storage import StorageKeyNotFoundError

    mock_storage = MagicMock()

    def exists_side_effect(key: str) -> bool:
        return not key.endswith(".waveform.json")

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.side_effect = StorageKeyNotFoundError("preview.mp4")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        # Should not raise StorageKeyNotFoundError
        job_waveform(1, "1/preview/preview.mp4")

    mock_cache.assert_not_called()


def test_job_waveform_generates_when_needed() -> None:
    mock_storage = MagicMock()

    def exists_side_effect(key: str) -> bool:
        return not key.endswith(".waveform.json")

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.return_value = Path("/tmp/fake_preview.mp4")

    with (
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks._cache_waveform") as mock_cache,
    ):
        job_waveform(1, "1/preview/preview.mp4")

    mock_storage.get.assert_called_once_with("1/preview/preview.mp4")
    mock_cache.assert_called_once_with(
        mock_storage, "1/preview/preview.mp4", Path("/tmp/fake_preview.mp4")
    )


def test_job_ingest_room_url_success(db_session: Session, tmp_path: Path) -> None:
    clip = generate_clip(1.0, output_dir=tmp_path)
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)

    event = Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk1 = Talk(
        event_id=event.id,
        title="Talk 1",
        room="Room URL",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    talk2 = Talk(
        event_id=event.id,
        title="Talk 2",
        room="Room URL",
        start=now + timedelta(minutes=30),
        end=now + timedelta(minutes=60),
        status="waiting_for_files",
    )
    db_session.add_all([talk1, talk2])
    db_session.commit()

    mock_storage = MagicMock()
    mock_storage.free_bytes.return_value = 10**10

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.download_media_url", return_value=clip),
        patch("app.tasks.container_duration_seconds", return_value=3600.0),
        patch("app.tasks.light_queue.enqueue") as mock_light_q,
    ):
        job_ingest_room_url(
            event_id=event.id,
            room="Room URL",
            video_url="https://example.com/recordings/sample.mp4",
            recording_start_iso=now.isoformat(),
            talk_ids=[talk1.id, talk2.id],
        )

    db_session.expire_all()
    reloaded1 = db_session.get(Talk, talk1.id)
    reloaded2 = db_session.get(Talk, talk2.id)
    assert reloaded1 is not None
    assert reloaded2 is not None
    assert reloaded1.status == "detecting"
    assert reloaded2.status == "detecting"
    assert reloaded1.cut_start == 0.0
    assert reloaded1.cut_end == 1800.0
    assert reloaded2.cut_start == 1800.0
    assert reloaded2.cut_end == 3600.0
    assert mock_storage.link_or_copy.call_count == 2
    assert mock_light_q.call_count == 2


def test_job_ingest_room_url_download_failure_handles_cleanup(
    db_session: Session,
) -> None:
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    event = Event(name="Test Event")
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title="Talk Fail",
        room="Room URL",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch(
            "app.tasks.download_media_url",
            side_effect=RuntimeError("Network download error"),
        ),
        pytest.raises(RuntimeError, match="Network download error"),
    ):
        job_ingest_room_url(
            event_id=event.id,
            room="Room URL",
            video_url="https://example.com/recordings/broken.mp4",
            recording_start_iso=now.isoformat(),
            talk_ids=[talk.id],
        )

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == "broken"
    job = db_session.scalars(select(Job)).one()
    assert job.status == "failed"


def test_resolve_direct_media_url() -> None:
    from app.ingest import resolve_direct_media_url

    # Direct URL passes through
    assert (
        resolve_direct_media_url("https://example.com/recordings/room1.mp4")
        == "https://example.com/recordings/room1.mp4"
    )

    # Dropbox URL normalizes dl=1
    db_res = resolve_direct_media_url(
        "https://www.dropbox.com/s/abcdef123/video.mp4?dl=0"
    )
    assert "dl=1" in db_res

    # Google Drive share link normalizes to export endpoint
    gd_res = resolve_direct_media_url(
        "https://drive.google.com/file/d/1AbC-xYz_123/view?usp=sharing"
    )
    assert (
        gd_res
        == "https://drive.google.com/uc?export=download&id=1AbC-xYz_123&confirm=t"
    )

    # Nextcloud share link normalizes to /download
    nc_res = resolve_direct_media_url("https://cloud.example.org/s/abc123XYZ")
    assert nc_res == "https://cloud.example.org/s/abc123XYZ/download"


def test_download_media_url_streaming_success(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    dummy_content = b"x" * 50000
    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "video/mp4"}
    mock_resp.read.side_effect = [dummy_content[:25000], dummy_content[25000:], b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "staged_download.mp4"
    with patch("urllib.request.build_opener", return_value=mock_opener):
        res = download_media_url("https://example.com/stream.mp4", target)

    assert res == target
    assert target.exists()
    assert target.read_bytes() == dummy_content


def test_download_media_url_exceeds_max_bytes(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "video/mp4"}
    # Return 2MB of data when limit is patched to 1MB
    mock_resp.read.side_effect = [b"A" * 1024 * 1024, b"B" * 1024 * 1024, b""]

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "too_large.mp4"
    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        patch("app.config.settings.max_url_download_bytes", 1024 * 1024),
        pytest.raises(ValueError, match="Download exceeded maximum allowed size"),
    ):
        download_media_url("https://example.com/too_large.mp4", target)

    assert not target.exists()


def test_download_media_url_rejects_html_page(tmp_path: Path) -> None:
    from unittest.mock import MagicMock, patch

    from app.ingest import download_media_url

    mock_resp = MagicMock()
    mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / "out.mp4"
    with (
        patch("urllib.request.build_opener", return_value=mock_opener),
        pytest.raises(ValueError, match="YouTube watch/stream links are web pages"),
    ):
        download_media_url("https://youtu.be/WVR7ZfuhoU8", target)


# ── Cut-Bounds Pre-Seeding Tests ────────────────────────────────────────────


def _make_detect_result(duration: float) -> DetectResult:
    return DetectResult(
        passed=True,
        actual_duration_seconds=duration,
        has_video=True,
        has_audio=True,
        reason=None,
    )


def _create_talk(
    db_session: Session,
    *,
    start: datetime,
    end: datetime,
    status: str = "detecting",
    cut_start: float | None = None,
    cut_end: float | None = None,
) -> Talk:
    event = Event(name=f"Event at {start.isoformat()}")
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title=f"Talk at {start.isoformat()}",
        room="Room A",
        start=start,
        end=end,
        status=status,
        cut_start=cut_start,
        cut_end=cut_end,
    )
    db_session.add(talk)
    db_session.commit()
    return talk


def _run_detect_with_seed(
    db_session: Session, talk_id: int, recording_start: datetime | None
) -> None:
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path("/tmp/fake.mp4")

    with (
        patch(
            "app.tasks.SessionLocal",
            side_effect=_worker_session_factory(db_session),
        ),
        patch("app.tasks.get_storage_backend", return_value=mock_storage),
        patch("app.tasks.detect", return_value=_make_detect_result(14400.0)),
    ):
        job_detect(talk_id, "1/raw/raw.mp4", recording_start)


def test_seed_cut_bounds_happy_path(db_session: Session) -> None:
    """Seeds cut_start/cut_end from schedule offsets when both are unset."""
    # Recording started at 09:00 UTC; talk is 09:15–09:55
    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 15, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 55, tzinfo=UTC),
    )
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(db_session, talk.id, rec_start)

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.cut_start == pytest.approx(900.0)  # 15 min
    assert reloaded.cut_end == pytest.approx(3300.0)  # 55 min
    assert reloaded.status == "pending_approval"


def test_seed_cut_bounds_no_overwrite_when_already_set(db_session: Session) -> None:
    """Does not overwrite existing cut_start/cut_end if user already set them."""
    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 15, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 55, tzinfo=UTC),
        cut_start=42.0,
        cut_end=1800.0,
    )
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(db_session, talk.id, rec_start)

    # Guard: both were set, so seeding must be skipped
    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.cut_start == pytest.approx(42.0)
    assert reloaded.cut_end == pytest.approx(1800.0)


def test_seed_cut_bounds_no_op_without_recording_start(db_session: Session) -> None:
    """Bounds stay None when recording_start is not provided (graceful degradation)."""
    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 15, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 55, tzinfo=UTC),
    )

    _run_detect_with_seed(db_session, talk.id, recording_start=None)

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.cut_start is None
    assert reloaded.cut_end is None
    assert reloaded.status == "pending_approval"


def test_seed_cut_bounds_talk_entirely_outside_recording(db_session: Session) -> None:
    """Skips seeding when offset_e <= offset_s (talk is before the recording start)."""
    # Recording started at 10:00; talk ended before that
    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
    )
    rec_start = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)

    _run_detect_with_seed(db_session, talk.id, rec_start)

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    # offset_s = max(0, -3600) = 0; offset_e = min(14400, -1800) → negative → clamped to ≤0
    assert reloaded.cut_start is None
    assert reloaded.cut_end is None


def test_seed_cut_bounds_clamped_to_duration(db_session: Session) -> None:
    """cut_end is clamped to raw_duration_seconds when talk extends past the recording."""
    # Recording started at 09:00; 4 h long (14400 s); talk goes until 13:15 → end at 15300 s > 14400
    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 13, 15, tzinfo=UTC),
    )
    rec_start = datetime(2026, 8, 29, 9, 0, tzinfo=UTC)

    _run_detect_with_seed(db_session, talk.id, rec_start)

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.cut_start == pytest.approx(0.0)
    assert reloaded.cut_end == pytest.approx(14400.0)  # clamped to duration
