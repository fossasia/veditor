"""Unit tests for the pure preview video generator (pipeline/preview.py)."""

from pathlib import Path
from unittest.mock import patch

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
    assert info.resolution == (320, 180)
    assert info.has_video
    assert info.has_audio

    # Preview output must be meaningfully smaller than the uncompressed/higher-res cut input
    assert output_clip.stat().st_size < input_clip.stat().st_size


def test_generate_preview_big_video_preset(tmp_path: Path):
    """Verify preview generation with the big_video preset."""
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
    assert info.resolution == (640, 360)
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

    assert small_info.resolution == (320, 180)
    assert big_info.resolution == (640, 360)

    # small_video preset must produce a smaller file size than big_video preset
    assert small_output.stat().st_size < big_output.stat().st_size


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

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["big_video"])

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


def test_generate_preview_with_threads_and_preset_speed(tmp_path: Path):
    """Verify preview generation with explicit thread count and speed preset."""
    preset = PreviewPreset(
        name="custom_speed",
        resolution=(320, 180),
        video_bitrate=150_000,
        preset_speed="ultrafast",
    )
    input_clip = generate_clip(
        1.0,
        resolution=(640, 360),
        pattern="gradient",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "threads_preview.mp4"

    real_open = av.open
    captured_streams = []

    class ContainerProxy:
        def __init__(self, target):
            self._target = target

        def __enter__(self):
            self._target.__enter__()
            return self

        def __exit__(self, *args):
            return self._target.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._target, name)

        def add_stream(self, *args, **kwargs):
            captured_streams.append((args, kwargs))
            return self._target.add_stream(*args, **kwargs)

    def fake_open(*args, **kwargs):
        c = real_open(*args, **kwargs)
        return ContainerProxy(c) if kwargs.get("mode") == "w" else c

    with patch("app.pipeline.preview.av.open", side_effect=fake_open):
        generate_preview(input_clip, output_clip, preset, threads=1)

    libx264_calls = [
        kwargs for args, kwargs in captured_streams if args and args[0] == "libx264"
    ]
    assert len(libx264_calls) == 1
    encoder_options = libx264_calls[0].get("options", {})
    assert encoder_options.get("preset") == "ultrafast"
    assert encoder_options.get("threads") == "1"

    assert output_clip.is_file()
    assert_playable(output_clip)
    info = open_and_inspect(output_clip)
    assert info.resolution == (320, 180)


def test_generate_preview_rejects_invalid_threads(tmp_path: Path):
    """Verify that non-positive threads values raise ValueError."""
    input_clip = generate_clip(1.0, output_dir=tmp_path)
    output_clip = tmp_path / "invalid_threads_preview.mp4"
    preset = PREVIEW_PRESETS["small_video"]

    with pytest.raises(ValueError, match="threads must be greater than zero"):
        generate_preview(input_clip, output_clip, preset, threads=0)

    with pytest.raises(ValueError, match="threads must be greater than zero"):
        generate_preview(input_clip, output_clip, preset, threads=-1)


def test_generate_preview_high_framerate_decimation(tmp_path: Path):
    """Verify that >30 fps videos are decimated to <=30 fps and duration matches."""
    input_clip = generate_clip(2.0, fps=60, pattern="solid", output_dir=tmp_path)
    output_clip = tmp_path / "decimated_preview.mp4"

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["small_video"])

    assert output_clip.is_file()
    assert_playable(output_clip)
    assert_duration_close(input_clip, output_clip, tolerance_seconds=0.25)
    with av.open(str(output_clip)) as c:
        v = c.streams.video[0]
        avg_rate = float(v.average_rate or v.guessed_rate)
        assert avg_rate <= 30.0
