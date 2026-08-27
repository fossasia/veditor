"""Unit tests for the pure preview video generator (pipeline/preview.py)."""

from pathlib import Path

import av
import pytest

from app.config import PREVIEW_PRESETS, PreviewPreset
from app.pipeline.preview import generate_preview
from tests.conftest import (
    assert_duration_close,
    assert_playable,
    generate_clip,
    generate_corrupt_clip,
    open_and_inspect,
)


def test_generate_preview_small_video_preset(tmp_path: Path):
    """Verify preview generation with the small_video preset on video+audio clip."""
    # Create higher-resolution synthetic source (720p) with video and audio
    input_clip = generate_clip(
        2.0,
        resolution=(1280, 720),
        pattern="gradient",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "small_video_preview.mp4"

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["small_video"])

    assert output_clip.is_file()
    assert_playable(output_clip)
    assert_duration_close(input_clip, output_clip, tolerance_seconds=0.25)

    info = open_and_inspect(output_clip)
    assert info.resolution == (640, 360)
    assert info.has_video
    assert info.has_audio

    # Preview output must be meaningfully smaller than the uncompressed/higher-res cut input
    assert output_clip.stat().st_size < input_clip.stat().st_size


def test_generate_preview_big_video_preset(tmp_path: Path):
    """
    Verify preview generation with the big_video preset (lower bitrate/resolution for long recordings).

    Note on simulating a long session:
    Since real 60-minute fixtures are impractical for unit tests, a short synthetic clip
    is used to exercise the 'big_video' preset directly. The preset's encoding and
    compression behavior is what is under test, not the literal wall-clock duration.
    """
    input_clip = generate_clip(
        2.0,
        resolution=(1280, 720),
        pattern="gradient",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "big_video_preview.mp4"

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["big_video"])

    assert output_clip.is_file()
    assert_playable(output_clip)
    assert_duration_close(input_clip, output_clip, tolerance_seconds=0.25)

    info = open_and_inspect(output_clip)
    assert info.resolution == (320, 180)
    assert info.has_video
    assert info.has_audio
    assert output_clip.stat().st_size < input_clip.stat().st_size


def test_presets_are_differentiated(tmp_path: Path):
    """
    Verify that small_video and big_video presets produce measurably different output
    (resolution and file size/bitrate) on the exact same input clip.
    """
    input_clip = generate_clip(
        3.0,
        resolution=(1280, 720),
        pattern="gradient",
        output_dir=tmp_path,
    )
    small_output = tmp_path / "diff_small.mp4"
    big_output = tmp_path / "diff_big.mp4"

    generate_preview(input_clip, small_output, PREVIEW_PRESETS["small_video"])
    generate_preview(input_clip, big_output, PREVIEW_PRESETS["big_video"])

    small_info = open_and_inspect(small_output)
    big_info = open_and_inspect(big_output)

    assert small_info.resolution == (640, 360)
    assert big_info.resolution == (320, 180)

    # big_video preset (for long sessions) must produce a smaller file size than small_video preset
    assert big_output.stat().st_size < small_output.stat().st_size


def test_generate_preview_video_only(tmp_path: Path):
    """Verify preview generation for clips with video only (no audio stream)."""
    input_clip = generate_clip(
        1.0,
        has_video=True,
        has_audio=False,
        resolution=(640, 480),
        pattern="solid",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "video_only_preview.mp4"

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["small_video"])

    assert output_clip.is_file()
    assert_playable(output_clip)
    assert_duration_close(input_clip, output_clip, tolerance_seconds=0.25)

    info = open_and_inspect(output_clip)
    assert info.has_video
    assert not info.has_audio
    assert info.resolution == (640, 360)


def test_generate_preview_rejects_corrupt_input(tmp_path: Path):
    """Verify that corrupt or invalid media files raise appropriate errors."""
    corrupt_clip = generate_corrupt_clip(output_dir=tmp_path)
    output_clip = tmp_path / "corrupt_output.mp4"

    with pytest.raises(av.FFmpegError):
        generate_preview(corrupt_clip, output_clip, PREVIEW_PRESETS["small_video"])


def test_generate_preview_custom_preset(tmp_path: Path):
    """Verify generate_preview works with a custom PreviewPreset configuration."""
    custom_preset = PreviewPreset(
        name="custom_low",
        resolution=(160, 120),
        video_bitrate=80_000,
        audio_bitrate=24_000,
    )
    input_clip = generate_clip(
        1.0,
        resolution=(320, 240),
        pattern="gradient",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "custom_preview.mp4"

    generate_preview(input_clip, output_clip, custom_preset)

    assert output_clip.is_file()
    assert_playable(output_clip)
    info = open_and_inspect(output_clip)
    assert info.resolution == (160, 120)
