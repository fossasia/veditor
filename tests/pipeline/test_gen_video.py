from datetime import UTC, datetime, timedelta
from pathlib import Path

import av
import pytest

from tests.pipeline.gen_video import (
    generate_clip,
    generate_corrupt_clip,
    generate_mismatched_duration_clip,
)
from tests.pipeline.helpers import (
    assert_duration_close,
    assert_playable,
    open_and_inspect,
)


def test_generates_standard_clip(tmp_path: Path):
    path = generate_clip(0.5, pattern="gradient", output_dir=tmp_path)

    info = open_and_inspect(path)
    assert info.duration is not None
    assert info.duration > 0
    assert info.has_video
    assert info.has_audio
    assert "h264" in info.codec_names
    assert "aac" in info.codec_names
    assert info.resolution == (320, 240)
    assert_playable(path)


def test_generate_clip_requires_at_least_one_stream(tmp_path: Path):
    with pytest.raises(ValueError, match="generate_clip requires at least one stream"):
        generate_clip(0.5, has_video=False, has_audio=False, output_dir=tmp_path)


def test_generate_clip_rejects_non_positive_duration(tmp_path: Path):
    with pytest.raises(ValueError, match="duration_s must be greater than zero"):
        generate_clip(0, pattern="gradient", output_dir=tmp_path)

    with pytest.raises(ValueError, match="duration_s must be greater than zero"):
        generate_clip(-1.0, pattern="gradient", output_dir=tmp_path)


def test_generate_clip_rejects_unknown_video_pattern(tmp_path: Path):
    with pytest.raises(ValueError, match="Unsupported video pattern: unknown-pattern"):
        generate_clip(0.5, pattern="unknown-pattern", output_dir=tmp_path)


def test_generate_clip_rejects_unknown_audio_waveform(tmp_path: Path):
    with pytest.raises(
        ValueError, match="Unsupported audio waveform: unknown-waveform"
    ):
        generate_clip(
            0.5,
            pattern="gradient",
            audio_waveform="unknown-waveform",
            output_dir=tmp_path,
        )


def test_generates_video_only_clip(tmp_path: Path):
    path = generate_clip(
        0.25,
        has_audio=False,
        pattern="noise",
        resolution=(64, 48),
        output_dir=tmp_path,
    )

    info = open_and_inspect(path)
    assert info.has_video
    assert not info.has_audio
    assert info.resolution == (64, 48)
    assert_playable(path)


def test_generates_audio_only_clip(tmp_path: Path):
    path = generate_clip(
        0.25,
        has_video=False,
        audio_waveform="silence",
        output_dir=tmp_path,
    )

    info = open_and_inspect(path)
    assert not info.has_video
    assert info.has_audio
    assert info.duration is not None
    assert info.duration > 0
    assert_playable(path)


def test_corrupt_clip_raises_on_open(tmp_path: Path):
    path = generate_corrupt_clip(output_dir=tmp_path)

    with pytest.raises(av.FFmpegError):
        open_and_inspect(path)


def test_generates_mismatched_duration_clip(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=0.5)

    scheduled_clip = generate_clip(0.5, output_dir=tmp_path)
    mismatched_clip = generate_mismatched_duration_clip(
        scheduled_start,
        scheduled_end,
        0.5,
        output_dir=tmp_path,
    )

    mismatch_info = open_and_inspect(mismatched_clip)
    assert mismatch_info.duration is not None
    assert mismatch_info.duration > 0.5

    with pytest.raises(AssertionError):
        assert_duration_close(scheduled_clip, mismatched_clip, tolerance_seconds=0.1)


def test_assert_duration_close_within_tolerance(tmp_path: Path):
    scheduled_start = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
    scheduled_end = scheduled_start + timedelta(seconds=0.5)

    scheduled_clip = generate_clip(0.5, output_dir=tmp_path)
    within_tolerance_clip = generate_mismatched_duration_clip(
        scheduled_start,
        scheduled_end,
        0.05,
        output_dir=tmp_path,
    )

    # Positive path: offset of 0.05 is within the 0.1 tolerance, so this should not raise.
    assert_duration_close(scheduled_clip, within_tolerance_clip, tolerance_seconds=0.1)


def test_pipeline_directory_contains_no_binary_media_assets():
    pipeline_dir = Path(__file__).parent
    media_suffixes = {".avi", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".wav"}

    committed_media = [
        path
        for path in pipeline_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() in media_suffixes
    ]

    assert committed_media == []
