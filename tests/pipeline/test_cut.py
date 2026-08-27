from pathlib import Path

import pytest

from app.pipeline.cut import CutStrategy, cut
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_cut_duration_matches_window(tmp_path: Path):
    """Verify that trimming a clip produces output duration matching the requested window."""
    source_clip = generate_clip(6.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_standard.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.0,
        end_seconds=4.0,
    )

    assert strategy == CutStrategy.STREAM_COPY
    assert output_clip.is_file()

    info = open_and_inspect(output_clip)
    assert info.duration is not None
    # Tolerance allows for nearest keyframe seeking (typically up to 1 GOP)
    assert abs(info.duration - 3.0) <= 0.8
    assert_playable(output_clip)


def test_cut_video_only(tmp_path: Path):
    """Verify cutting works on video-only clips without audio streams."""
    source_clip = generate_clip(
        5.0, has_video=True, has_audio=False, output_dir=tmp_path
    )
    output_clip = tmp_path / "cut_video_only.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.5,
        end_seconds=4.0,
    )

    assert strategy == CutStrategy.STREAM_COPY
    assert output_clip.is_file()
    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is False
    assert info.duration is not None
    assert abs(info.duration - 2.5) <= 0.8
    assert_playable(output_clip)


def test_cut_audio_only(tmp_path: Path):
    """Verify cutting works on audio-only clips without video streams."""
    source_clip = generate_clip(
        5.0, has_video=False, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "cut_audio_only.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.0,
        end_seconds=3.5,
    )

    assert strategy == CutStrategy.STREAM_COPY
    assert output_clip.is_file()
    info = open_and_inspect(output_clip)
    assert info.has_video is False
    assert info.has_audio is True
    assert info.duration is not None
    assert abs(info.duration - 2.5) <= 0.8
    assert_playable(output_clip)


def test_cut_reencode_fallback(tmp_path: Path):
    """Verify that forced re-encoding produces a valid, playable cut."""
    source_clip = generate_clip(4.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_reencode.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.0,
        end_seconds=3.0,
        force_reencode=True,
    )

    assert strategy == CutStrategy.RE_ENCODE
    assert output_clip.is_file()

    info = open_and_inspect(output_clip)
    assert info.duration is not None
    assert abs(info.duration - 2.0) <= 0.5
    assert_playable(output_clip)


def test_cut_invalid_inputs(tmp_path: Path):
    """Verify that invalid timestamps or non-existent files raise appropriate errors."""
    valid_clip = generate_clip(3.0, output_dir=tmp_path)
    output_clip = tmp_path / "out.mp4"

    # Non-existent input file
    with pytest.raises(FileNotFoundError):
        cut(tmp_path / "non_existent.mp4", output_clip, 0.0, 1.0)

    # Negative start time
    with pytest.raises(ValueError, match="start_seconds must be non-negative"):
        cut(valid_clip, output_clip, -1.0, 2.0)

    # End before or equal to start
    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        cut(valid_clip, output_clip, 2.0, 1.0)
    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        cut(valid_clip, output_clip, 2.0, 2.0)
