from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.consts import JobKind, JobStatus, TalkStatus
from app.ingest import download_media_url, resolve_direct_media_url
from app.models import Event, Job, Talk
from app.pipeline.detect import DetectResult
from app.storage import StorageKeyNotFoundError
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

from .conftest import generate_clip


@pytest.fixture
def mock_storage() -> MagicMock:
    storage = MagicMock()
    storage.get.return_value = Path('/tmp/fake_media.mp4')
    storage.put.return_value = None
    return storage


def test_job_ingest_success(
    db_session: Session,
    out_of_band_sessions: None,
    mock_storage: MagicMock,
    tmp_path: Path,
) -> None:
    event = Event(name='Test Event')
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title='Test Talk',
        room='Room A',
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status=TalkStatus.WAITING_FILES,
        cut_start=0.0,
        cut_end=1800.0,
    )
    db_session.add(talk)
    db_session.commit()

    staged_file = tmp_path / 'staged.mp4'
    staged_file.write_bytes(b'dummy video data')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks.validate_media_file', return_value=None),
        patch('app.tasks.light_queue.enqueue') as mock_enqueue,
    ):
        job_ingest(talk.id, str(staged_file), f'{talk.id}/raw/raw.mp4')

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == TalkStatus.DETECTING
    job = db_session.scalars(select(Job)).one()
    assert job.status == JobStatus.DONE
    assert job.kind == JobKind.INGEST
    assert mock_storage.put.call_count == 1
    mock_enqueue.assert_called_once_with(
        job_detect,
        talk.id,
        f'{talk.id}/raw/raw.mp4',
        None,
        job_timeout=STAGE_CONFIG[JobKind.DETECT]['job_timeout'],
    )
    # Staged file is unlinked
    assert not staged_file.exists()


def test_dispatch_assembly_generated_intro_enqueues_job_intro(
    db_session: Session,
    out_of_band_sessions: None,
) -> None:
    event = Event(name='Test Event')
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title='Test Talk',
        room='Room A',
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status=TalkStatus.ASSEMBLING,
        include_intro=True,
        intro_source='generated',
        include_outro=False,
    )
    db_session.add(talk)
    db_session.commit()

    with (
        patch('app.tasks.light_queue.enqueue') as mock_light_enqueue,
    ):
        dispatch_assembly(talk.id, cut_key=f'{talk.id}/cut/special.mp4')

    mock_light_enqueue.assert_called_once_with(
        job_intro,
        talk.id,
        f'{talk.id}/cut/special.mp4',
        f'{talk.id}/intro/intro.mp4',
        job_timeout=STAGE_CONFIG[JobKind.INTRO]['job_timeout'],
    )


def test_dispatch_assembly_intro_done_outro_generated_enqueues_job_outro(
    db_session: Session,
    out_of_band_sessions: None,
) -> None:
    event = Event(name='Test Event')
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title='Test Talk',
        room='Room A',
        start=datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 10, 30, tzinfo=UTC),
        status=TalkStatus.ASSEMBLING,
        include_intro=True,
        intro_source='generated',
        include_outro=True,
        outro_source='generated',
    )
    db_session.add(talk)
    db_session.commit()
    db_session.add(Job(talk_id=talk.id, kind=JobKind.INTRO, status=JobStatus.DONE))
    db_session.commit()

    with (
        patch('app.tasks.light_queue.enqueue') as mock_light_enqueue,
    ):
        dispatch_assembly(talk.id, cut_key=f'{talk.id}/cut/special.mp4')

    mock_light_enqueue.assert_called_once_with(
        job_outro,
        talk.id,
        f'{talk.id}/cut/special.mp4',
        f'{talk.id}/outro/outro.mp4',
        job_timeout=STAGE_CONFIG[JobKind.OUTRO]['job_timeout'],
    )


def test_job_waveform_skips_if_waveform_already_exists() -> None:
    mock_storage = MagicMock()
    mock_storage.exists.side_effect = lambda key: key.endswith('.waveform.json')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks._cache_waveform') as mock_cache,
    ):
        job_waveform(1, '1/preview/preview.mp4')

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_skips_if_media_key_missing() -> None:
    mock_storage = MagicMock()
    mock_storage.exists.return_value = False

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks._cache_waveform') as mock_cache,
    ):
        job_waveform(1, '1/preview/preview.mp4')

    mock_cache.assert_not_called()
    mock_storage.get.assert_not_called()


def test_job_waveform_handles_storage_key_not_found_on_get() -> None:

    mock_storage = MagicMock()

    def exists_side_effect(key: str) -> bool:
        return not key.endswith('.waveform.json')

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.side_effect = StorageKeyNotFoundError('preview.mp4')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks._cache_waveform') as mock_cache,
    ):
        # Should not raise StorageKeyNotFoundError
        job_waveform(1, '1/preview/preview.mp4')

    mock_cache.assert_not_called()


def test_job_waveform_generates_when_needed() -> None:
    mock_storage = MagicMock()

    def exists_side_effect(key: str) -> bool:
        return not key.endswith('.waveform.json')

    mock_storage.exists.side_effect = exists_side_effect
    mock_storage.get.return_value = Path('/tmp/fake_preview.mp4')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks._cache_waveform') as mock_cache,
    ):
        job_waveform(1, '1/preview/preview.mp4')

    mock_storage.get.assert_called_once_with('1/preview/preview.mp4')
    mock_cache.assert_called_once_with(
        mock_storage, '1/preview/preview.mp4', Path('/tmp/fake_preview.mp4')
    )


def test_job_ingest_room_url_success(
    db_session: Session, out_of_band_sessions: None, tmp_path: Path
) -> None:
    clip = generate_clip(1.0, output_dir=tmp_path)
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)

    event = Event(name='Test Event')
    db_session.add(event)
    db_session.commit()
    talk1 = Talk(
        event_id=event.id,
        title='Talk 1',
        room='Room URL',
        start=now,
        end=now + timedelta(minutes=30),
        status=TalkStatus.WAITING_FILES,
    )
    talk2 = Talk(
        event_id=event.id,
        title='Talk 2',
        room='Room URL',
        start=now + timedelta(minutes=30),
        end=now + timedelta(minutes=60),
        status=TalkStatus.WAITING_FILES,
    )
    db_session.add_all([talk1, talk2])
    db_session.commit()

    mock_storage = MagicMock()
    mock_storage.free_bytes.return_value = 10**10

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks.download_media_url', return_value=clip),
        patch('app.tasks.container_duration_seconds', return_value=3600.0),
        patch('app.tasks.light_queue.enqueue') as mock_light_q,
    ):
        job_ingest_room_url(
            event_id=event.id,
            room='Room URL',
            video_url='https://example.com/recordings/sample.mp4',
            recording_start_iso=now.isoformat(),
            talk_ids=[talk1.id, talk2.id],
        )

    db_session.expire_all()
    reloaded1 = db_session.get(Talk, talk1.id)
    reloaded2 = db_session.get(Talk, talk2.id)
    assert reloaded1 is not None
    assert reloaded2 is not None
    assert reloaded1.status == TalkStatus.DETECTING
    assert reloaded2.status == TalkStatus.DETECTING
    assert reloaded1.cut_start == 0.0
    assert reloaded1.cut_end == 1800.0
    assert reloaded2.cut_start == 1800.0
    assert reloaded2.cut_end == 3600.0
    assert mock_storage.link_or_copy.call_count == 2
    assert mock_light_q.call_count == 2


def test_job_ingest_room_url_download_failure_handles_cleanup(
    db_session: Session,
    out_of_band_sessions: None,
) -> None:
    now = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
    event = Event(name='Test Event')
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title='Talk Fail',
        room='Room URL',
        start=now,
        end=now + timedelta(minutes=30),
        status=TalkStatus.WAITING_FILES,
    )
    db_session.add(talk)
    db_session.commit()

    with (
        patch(
            'app.tasks.download_media_url',
            side_effect=RuntimeError('Network download error'),
        ),
        pytest.raises(RuntimeError, match='Network download error'),
    ):
        job_ingest_room_url(
            event_id=event.id,
            room='Room URL',
            video_url='https://example.com/recordings/broken.mp4',
            recording_start_iso=now.isoformat(),
            talk_ids=[talk.id],
        )

    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == TalkStatus.BROKEN
    job = db_session.scalars(select(Job)).one()
    assert job.status == JobStatus.FAILED


def test_resolve_direct_media_url() -> None:

    # Direct URL passes through
    assert (
        resolve_direct_media_url('https://example.com/recordings/room1.mp4')
        == 'https://example.com/recordings/room1.mp4'
    )

    # Dropbox URL normalizes dl=1
    db_res = resolve_direct_media_url(
        'https://www.dropbox.com/s/abcdef123/video.mp4?dl=0'
    )
    assert 'dl=1' in db_res

    # Google Drive share link normalizes to export endpoint
    gd_res = resolve_direct_media_url(
        'https://drive.google.com/file/d/1AbC-xYz_123/view?usp=sharing'
    )
    assert (
        gd_res
        == 'https://drive.google.com/uc?export=download&id=1AbC-xYz_123&confirm=t'
    )

    # Nextcloud share link normalizes to /download
    nc_res = resolve_direct_media_url('https://cloud.example.org/s/abc123XYZ')
    assert nc_res == 'https://cloud.example.org/s/abc123XYZ/download'


def test_download_media_url_streaming_success(tmp_path: Path) -> None:

    dummy_content = b'x' * 50000
    mock_resp = MagicMock()
    mock_resp.headers = {'Content-Type': 'video/mp4'}
    mock_resp.read.side_effect = [dummy_content[:25000], dummy_content[25000:], b'']

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / 'staged_download.mp4'
    with patch('urllib.request.build_opener', return_value=mock_opener):
        res = download_media_url('https://example.com/stream.mp4', target)

    assert res == target
    assert target.exists()
    assert target.read_bytes() == dummy_content


def test_download_media_url_exceeds_max_bytes(tmp_path: Path) -> None:

    mock_resp = MagicMock()
    mock_resp.headers = {'Content-Type': 'video/mp4'}
    # Return 2MB of data when limit is patched to 1MB
    mock_resp.read.side_effect = [b'A' * 1024 * 1024, b'B' * 1024 * 1024, b'']

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / 'too_large.mp4'
    with (
        patch('urllib.request.build_opener', return_value=mock_opener),
        patch('app.config.settings.max_url_download_bytes', 1024 * 1024),
        pytest.raises(ValueError, match='Download exceeded maximum allowed size'),
    ):
        download_media_url('https://example.com/too_large.mp4', target)

    assert not target.exists()


def test_download_media_url_rejects_html_page(tmp_path: Path) -> None:

    mock_resp = MagicMock()
    mock_resp.headers = {'Content-Type': 'text/html; charset=utf-8'}

    mock_opener = MagicMock()
    mock_opener.open.return_value = mock_resp

    target = tmp_path / 'out.mp4'
    with (
        patch('urllib.request.build_opener', return_value=mock_opener),
        pytest.raises(ValueError, match='YouTube watch/stream links are web pages'),
    ):
        download_media_url('https://youtu.be/WVR7ZfuhoU8', target)


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
    status: str = 'detecting',
    cut_start: float | None = None,
    cut_end: float | None = None,
) -> Talk:
    event = Event(name=f'Event at {start.isoformat()}')
    db_session.add(event)
    db_session.commit()
    talk = Talk(
        event_id=event.id,
        title=f'Talk at {start.isoformat()}',
        room='Room A',
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
    db_session: Session,
    talk_id: int,
    recording_start: datetime | None,
) -> None:
    mock_storage = MagicMock()
    mock_storage.get.return_value = Path('/tmp/fake.mp4')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks.detect', return_value=_make_detect_result(14400.0)),
    ):
        job_detect(talk_id, '1/raw/raw.mp4', recording_start)


def test_seed_cut_bounds_happy_path(
    db_session: Session, out_of_band_sessions: None
) -> None:
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
    assert reloaded.status == TalkStatus.PENDING_APPROVAL


def test_seed_cut_bounds_no_overwrite_when_already_set(
    db_session: Session, out_of_band_sessions: None
) -> None:
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


def test_seed_cut_bounds_no_op_without_recording_start(
    db_session: Session, out_of_band_sessions: None
) -> None:
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
    assert reloaded.status == TalkStatus.PENDING_APPROVAL


def test_seed_cut_bounds_talk_entirely_outside_recording(
    db_session: Session, out_of_band_sessions: None
) -> None:
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


def test_seed_cut_bounds_clamped_to_duration(
    db_session: Session, out_of_band_sessions: None
) -> None:
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


def test_job_cut_multi_segment_concatenates_cuts(
    db_session: Session, out_of_band_sessions: None, tmp_path: Path
) -> None:
    import math

    import av
    import numpy as np

    from app.pipeline.detect import container_duration_seconds
    from app.storage import LocalDiskBackend
    from app.tasks import job_cut

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_path = tmp_path / 'raw_src' / 'raw.mp4'
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    fps = 24
    with av.open(str(raw_path), mode='w') as c:
        v = c.add_stream('libx264', rate=fps)
        v.width = 64
        v.height = 64
        v.pix_fmt = 'yuv420p'
        a = c.add_stream('aac', rate=44100)
        a.layout = 'mono'

        for i in range(3 * fps):
            color = (
                (255, 0, 0)
                if i < fps
                else ((0, 255, 0) if i < 2 * fps else (0, 0, 255))
            )
            arr = np.full((64, 64, 3), color, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(arr, format='rgb24')
            for pkt in v.encode(frame):
                c.mux(pkt)
        for pkt in v.encode():
            c.mux(pkt)

        silence = np.zeros(1024, dtype=np.int16).reshape(1, -1)
        for _ in range(math.ceil(3.0 * 44100 / 1024)):
            a_frame = av.AudioFrame.from_ndarray(silence, format='s16', layout='mono')
            a_frame.sample_rate = 44100
            for pkt in a.encode(a_frame):
                c.mux(pkt)
        for pkt in a.encode():
            c.mux(pkt)

    storage.put('1/raw/raw.mp4', raw_path)

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=0.0,
        cut_end=3.0,
    )
    talk.raw_duration_seconds = 3.0
    talk.cut_segments = [
        {'start': 0.0, 'end': 0.8},
        {'start': 2.2, 'end': 3.0},
    ]
    db_session.commit()

    with (
        patch('app.tasks.get_storage_backend', return_value=storage),
        patch('app.tasks.light_queue.enqueue'),
    ):
        job_cut(talk.id, '1/raw/raw.mp4', '1/cut/cut.mp4')

    assert storage.exists('1/cut/cut.mp4')
    out_file = storage.get('1/cut/cut.mp4')

    observed_colors: list[str] = []
    with av.open(str(out_file)) as c:
        out_dur = container_duration_seconds(c)
        for frame in c.decode(video=0):
            rgb = frame.to_ndarray(format='rgb24')
            mean_c = rgb.mean(axis=(0, 1))
            r, g, b = mean_c[0], mean_c[1], mean_c[2]
            if r > 150 and g < 80 and b < 80:
                c_name = 'red'
            elif g > 150 and r < 80 and b < 80:
                c_name = 'green'
            elif b > 150 and r < 80 and g < 80:
                c_name = 'blue'
            else:
                c_name = 'other'
            if not observed_colors or observed_colors[-1] != c_name:
                observed_colors.append(c_name)

    assert out_dur == pytest.approx(1.6, abs=0.5)
    assert 'green' not in observed_colors, (
        'Intervening footage (green) was not removed!'
    )
    assert observed_colors == ['red', 'blue'], (
        f'Expected red followed by blue, got: {observed_colors}'
    )


def test_job_cut_discarded_if_aborted_or_superseded(
    db_session: Session, out_of_band_sessions: None, tmp_path: Path
) -> None:
    from app.db import new_session
    from app.storage import LocalDiskBackend
    from app.tasks import job_cut
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(2.0, output_dir=tmp_path / 'raw_src')
    storage.put('1/raw/raw.mp4', raw_clip)
    storage.put('1/cut/cut.mp4', b'replacement cut content')

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=0.0,
        cut_end=1.0,
    )
    talk.raw_duration_seconds = 2.0
    db_session.commit()

    def fake_cut(inp: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        with new_session() as db:
            t = db.get(Talk, talk.id)
            if t:
                t.status = TalkStatus.WAITING_FILES
                db.commit()
        Path(out).write_bytes(b'stale worker output')

    def fake_concat(segs: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        Path(out).write_bytes(b'concatenated output')

    with (
        patch('app.tasks.get_storage_backend', return_value=storage),
        patch('app.tasks.cut', side_effect=fake_cut),
        patch('app.tasks.concat_segments', side_effect=fake_concat),
        patch('app.tasks._cache_waveform'),
        patch('app.tasks.light_queue.enqueue'),
    ):
        job_cut(talk.id, '1/raw/raw.mp4', '1/cut/cut.mp4')

    assert storage.get('1/cut/cut.mp4').read_bytes() == b'replacement cut content'
    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.status == TalkStatus.WAITING_FILES


def test_job_cut_resynchronizes_stale_cut_segments(
    db_session: Session, out_of_band_sessions: None, tmp_path: Path
) -> None:
    from app.storage import LocalDiskBackend
    from app.tasks import job_cut
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(2.0, output_dir=tmp_path / 'raw_src')
    storage.put('1/raw/raw.mp4', raw_clip)

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=0.2,
        cut_end=0.8,
    )
    talk.raw_duration_seconds = 2.0
    talk.cut_segments = [
        {'start': 1.0, 'end': 1.5},
        {'start': 1.6, 'end': 1.9},
    ]
    db_session.commit()

    captured_bounds: list[tuple[float, float]] = []

    def fake_cut(
        inp: Any, out: Any, start: float, end: float, *args: Any, **kwargs: Any
    ) -> None:
        captured_bounds.append((start, end))
        Path(out).write_bytes(b'cut content')

    def fake_concat(segs: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        Path(out).write_bytes(b'concatenated output')

    with (
        patch('app.tasks.get_storage_backend', return_value=storage),
        patch('app.tasks.cut', side_effect=fake_cut),
        patch('app.tasks.concat_segments', side_effect=fake_concat),
        patch('app.tasks._cache_waveform'),
        patch('app.tasks.light_queue.enqueue'),
    ):
        job_cut(talk.id, '1/raw/raw.mp4', '1/cut/cut.mp4')

    assert captured_bounds == [(0.2, 0.8)]
    db_session.expire_all()
    reloaded = db_session.get(Talk, talk.id)
    assert reloaded is not None
    assert reloaded.cut_segments == [{'start': 0.2, 'end': 0.8}]


def test_job_cut_multi_segment_enforces_frame_accurate_fallback(
    db_session: Session, out_of_band_sessions: None, tmp_path: Path
) -> None:
    from app.storage import LocalDiskBackend
    from app.tasks import job_cut
    from tests.conftest import generate_clip

    storage = LocalDiskBackend(data_dir=tmp_path)
    raw_clip = generate_clip(3.0, output_dir=tmp_path / 'raw_src')
    storage.put('1/raw/raw.mp4', raw_clip)

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=0.5,
        cut_end=2.5,
    )
    talk.raw_duration_seconds = 3.0
    talk.cut_segments = [
        {'start': 0.5, 'end': 1.2},
        {'start': 1.8, 'end': 2.5},
    ]
    db_session.commit()

    cut_calls: list[tuple[float, float, dict[str, Any]]] = []

    def fake_cut(
        inp: Any, out: Any, start: float, end: float, *args: Any, **kwargs: Any
    ) -> None:
        cut_calls.append((start, end, kwargs))
        Path(out).write_bytes(b'cut segment')

    def fake_concat(segs: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        Path(out).write_bytes(b'concatenated output')

    with (
        patch('app.tasks.get_storage_backend', return_value=storage),
        patch('app.tasks.cut', side_effect=fake_cut),
        patch('app.tasks.concat_segments', side_effect=fake_concat),
        patch('app.tasks._cache_waveform'),
        patch('app.tasks.light_queue.enqueue'),
    ):
        job_cut(talk.id, '1/raw/raw.mp4', '1/cut/cut.mp4')

    assert len(cut_calls) == 2
    assert cut_calls[0][2].get('frame_accurate_fallback') is True
    assert cut_calls[1][2].get('frame_accurate_fallback') is True


def test_job_cut_fails_closed_when_newer_job_query_errors(
    db_session: Session, out_of_band_sessions: None, mock_storage: MagicMock
) -> None:
    """Worker fails closed without calling storage.put when newer cut job query fails."""
    from contextlib import contextmanager

    from app.db import new_session
    from app.tasks import job_cut

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=10.0,
        cut_end=50.0,
    )
    talk.cut_segments = [{'start': 10.0, 'end': 50.0}]
    db_session.commit()

    def fake_cut(inp: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        Path(out).write_bytes(b'cut content')

    call_count = 0

    @contextmanager
    def buggy_new_session() -> Generator[Session]:
        nonlocal call_count
        call_count += 1
        with new_session() as sess:
            if call_count > 1:
                orig_query = sess.query

                def query_raise(model: Any, *args: Any, **kwargs: Any) -> Any:
                    if model == Job:
                        raise RuntimeError('Database query failure')
                    return orig_query(model, *args, **kwargs)

                sess.query = query_raise  # type: ignore[method-assign,assignment]
            yield sess

    with (
        patch('app.tasks.new_session', side_effect=buggy_new_session),
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks.cut', side_effect=fake_cut),
        pytest.raises(RuntimeError, match='Database query failure'),
    ):
        job_cut(talk.id, '1/raw/raw.mp4')

    cut_key = f'{talk.id}/cut/cut.mp4'
    for call in mock_storage.put.call_args_list:
        assert call[0][0] != cut_key


def test_job_cut_discards_output_when_talk_superseded_or_aborted(
    db_session: Session, out_of_band_sessions: None, mock_storage: MagicMock
) -> None:
    """Worker discards output without calling storage.put if talk was aborted or superseded."""
    from app.db import new_session
    from app.tasks import job_cut

    talk = _create_talk(
        db_session,
        start=datetime(2026, 8, 29, 9, 0, tzinfo=UTC),
        end=datetime(2026, 8, 29, 9, 30, tzinfo=UTC),
        status='cutting',
        cut_start=10.0,
        cut_end=50.0,
    )
    db_session.commit()

    def fake_cut(inp: Any, out: Any, *args: Any, **kwargs: Any) -> None:
        with new_session() as db:
            t = db.get(Talk, talk.id)
            if t:
                t.status = TalkStatus.WAITING_FILES
                t.cut_start = None
                t.cut_end = None
                db.commit()
        Path(out).write_bytes(b'cut content')

    with (
        patch('app.tasks.get_storage_backend', return_value=mock_storage),
        patch('app.tasks.cut', side_effect=fake_cut),
        patch('app.tasks.light_queue.enqueue') as mock_enqueue,
    ):
        job_cut(talk.id, '1/raw/raw.mp4')

    cut_key = f'{talk.id}/cut/cut.mp4'
    for call in mock_storage.put.call_args_list:
        assert call[0][0] != cut_key
    mock_enqueue.assert_not_called()


def test_job_cut_multi_invokes_job_cut() -> None:
    """Verify job_cut_multi acts as an entrypoint delegating to job_cut."""
    from unittest.mock import patch

    from app.tasks import job_cut_multi

    with patch('app.tasks.job_cut') as mock_job_cut:
        job_cut_multi(42, '42/raw/video.mp4', '42/cut/custom.mp4')
        mock_job_cut.assert_called_once_with(
            42, '42/raw/video.mp4', '42/cut/custom.mp4'
        )
