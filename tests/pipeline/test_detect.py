from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.pipeline.detect import DETECT_DURATION_TOLERANCE_SECONDS, detect
from tests.conftest import (
    generate_clip,
    generate_corrupt_clip,
    generate_mismatched_duration_clip,
)


def test_detect_passes_clip_within_scheduled_window(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    clip = generate_clip(1, output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert result.passed
    assert result.actual_duration_seconds > 0
    assert result.has_video
    assert result.has_audio
    assert result.reason is None


def test_detect_passes_video_only_clip(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    clip = generate_clip(1, has_video=True, has_audio=False, output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert result.passed
    assert result.actual_duration_seconds > 0
    assert result.has_video
    assert not result.has_audio
    assert result.reason is None


def test_detect_tolerance_boundary_pass(tmp_path: Path):
    # Scheduled window is 300s, clip is 1s -> delta is 299s (within 300s default tolerance)
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=300)
    clip = generate_clip(1, output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert result.passed
    assert result.reason is None


def test_detect_tolerance_boundary_fail(tmp_path: Path):
    # Scheduled window is 302s, clip is 1s -> delta is 301s (exceeds 300s default tolerance)
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=302)
    clip = generate_clip(1, output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert not result.passed
    assert "duration" in result.reason


def test_detect_custom_tolerance_parameter(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=15)
    clip = generate_clip(5, output_dir=tmp_path)  # delta is 10s

    # Passes with tolerance >= 10s
    pass_result = detect(clip, scheduled_start, scheduled_end, tolerance_seconds=10.0)
    assert pass_result.passed

    # Fails with tighter tolerance
    fail_result = detect(clip, scheduled_start, scheduled_end, tolerance_seconds=5.0)
    assert not fail_result.passed
    assert "duration outside scheduled window" in fail_result.reason


def test_detect_fails_clip_with_mismatched_duration(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_duration = DETECT_DURATION_TOLERANCE_SECONDS + 2
    scheduled_end = scheduled_start + timedelta(seconds=scheduled_duration)
    clip = generate_mismatched_duration_clip(
        scheduled_start,
        scheduled_end,
        -(DETECT_DURATION_TOLERANCE_SECONDS + 1),
        output_dir=tmp_path,
    )

    result = detect(clip, scheduled_start, scheduled_end)

    assert not result.passed
    assert result.actual_duration_seconds > 0
    assert result.has_video
    assert "duration" in result.reason


def test_detect_fails_audio_only_clip_as_missing_video(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    clip = generate_clip(1, has_video=False, output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert not result.passed
    assert result.actual_duration_seconds > 0
    assert not result.has_video
    assert result.has_audio
    assert "video" in result.reason


def test_detect_fails_corrupt_clip_without_raising(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    clip = generate_corrupt_clip(output_dir=tmp_path)

    result = detect(clip, scheduled_start, scheduled_end)

    assert not result.passed
    assert result.actual_duration_seconds == 0.0
    assert not result.has_video
    assert not result.has_audio
    assert "unreadable" in result.reason


def test_detect_fails_nonexistent_file_without_raising(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    missing_path = tmp_path / "missing.mp4"

    result = detect(missing_path, scheduled_start, scheduled_end)

    assert not result.passed
    assert result.actual_duration_seconds == 0.0
    assert not result.has_video
    assert not result.has_audio
    assert "file not found" == result.reason


def test_detect_fails_empty_file(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=1)
    empty_path = tmp_path / "empty.mp4"
    empty_path.touch()

    result = detect(empty_path, scheduled_start, scheduled_end)

    assert not result.passed
    assert result.actual_duration_seconds == 0.0
    assert not result.has_video
    assert not result.has_audio
    assert "file is empty" in result.reason
