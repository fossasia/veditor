"""Unit tests for the pure preview video generator (pipeline/preview.py)."""

from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Self, cast
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


def test_generate_preview_small_video_preset(tmp_path: Path) -> None:
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


def test_generate_preview_big_video_preset(tmp_path: Path) -> None:
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


def test_presets_are_differentiated(tmp_path: Path) -> None:
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


def test_generate_preview_video_only(tmp_path: Path) -> None:
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


def test_generate_preview_rejects_corrupt_input(tmp_path: Path) -> None:
    """Verify that corrupt or invalid media files raise appropriate errors."""
    corrupt_clip = generate_corrupt_clip(output_dir=tmp_path)
    output_clip = tmp_path / "corrupt_output.mp4"

    with pytest.raises(av.FFmpegError):
        generate_preview(corrupt_clip, output_clip, PREVIEW_PRESETS["small_video"])


def test_generate_preview_custom_preset(tmp_path: Path) -> None:
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


def test_generate_preview_with_threads_and_preset_speed(tmp_path: Path) -> None:
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

    real_open: Callable[..., av.container.Container] = av.open
    captured_streams: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class ContainerProxy:
        def __init__(self, target: av.container.OutputContainer) -> None:
            self._target = target

        def __enter__(self) -> Self:
            self._target.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> None:
            return self._target.__exit__(exc_type, exc_val, exc_tb)

        def __getattr__(self, name: str) -> object:
            return getattr(self._target, name)

        def add_stream(self, *args: str, **kwargs: object) -> object:
            captured_streams.append((args, kwargs))
            return self._target.add_stream(*args, **kwargs)  # type: ignore[call-overload]

    def fake_open(*args: object, **kwargs: object) -> object:
        c = real_open(*args, **kwargs)
        return (
            ContainerProxy(cast("av.container.OutputContainer", c))
            if kwargs.get("mode") == "w"
            else c
        )

    with patch("app.pipeline.preview.av.open", side_effect=fake_open):
        generate_preview(input_clip, output_clip, preset, threads=1)

    libx264_calls = [
        kwargs for args, kwargs in captured_streams if args and args[0] == "libx264"
    ]
    assert len(libx264_calls) == 1
    encoder_options = libx264_calls[0].get("options", {})
    assert isinstance(encoder_options, dict)
    assert encoder_options.get("preset") == "ultrafast"
    assert encoder_options.get("tune") == "fastdecode,zerolatency"
    assert encoder_options.get("threads") == "1"

    assert output_clip.is_file()
    assert_playable(output_clip)
    info = open_and_inspect(output_clip)
    assert info.resolution == (320, 180)


def test_generate_preview_rejects_invalid_threads(tmp_path: Path) -> None:
    """Verify that non-positive threads values raise ValueError."""
    input_clip = generate_clip(1.0, output_dir=tmp_path)
    output_clip = tmp_path / "invalid_threads_preview.mp4"
    preset = PREVIEW_PRESETS["small_video"]

    with pytest.raises(ValueError, match="threads must be greater than zero"):
        generate_preview(input_clip, output_clip, preset, threads=0)

    with pytest.raises(ValueError, match="threads must be greater than zero"):
        generate_preview(input_clip, output_clip, preset, threads=-1)


def test_generate_preview_high_framerate_decimation(tmp_path: Path) -> None:
    """Verify that >30 fps videos are decimated to <=30 fps and duration matches."""
    input_clip = generate_clip(2.0, fps=60, pattern="solid", output_dir=tmp_path)
    output_clip = tmp_path / "decimated_preview.mp4"

    generate_preview(input_clip, output_clip, PREVIEW_PRESETS["small_video"])

    assert output_clip.is_file()
    assert_playable(output_clip)
    assert_duration_close(input_clip, output_clip, tolerance_seconds=0.25)
    with av.open(str(output_clip)) as c:
        v = c.streams.video[0]
        rate = v.average_rate or v.guessed_rate
        assert rate is not None
        avg_rate = float(rate)
        assert avg_rate <= 30.0


def test_generate_preview_start_and_end_seconds(tmp_path: Path) -> None:
    """Verify generate_preview correctly windows using start_seconds and end_seconds."""
    input_clip = generate_clip(6.0, resolution=(640, 360), output_dir=tmp_path)
    output_clip = tmp_path / "windowed_preview.mp4"

    generate_preview(
        input_clip,
        output_clip,
        PREVIEW_PRESETS["small_video"],
        start_seconds=1.5,
        end_seconds=4.5,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)
    info = open_and_inspect(output_clip)
    assert info.duration is not None
    # 4.5 - 1.5 = 3.0s window
    assert abs(info.duration - 3.0) <= 0.8


def test_generate_preview_invalid_start_end_bounds(tmp_path: Path) -> None:
    """Verify negative start_seconds and inverted bounds raise ValueError."""
    input_clip = generate_clip(2.0, output_dir=tmp_path)
    output_clip = tmp_path / "out.mp4"
    preset = PREVIEW_PRESETS["small_video"]

    with pytest.raises(ValueError, match="start_seconds must be non-negative"):
        generate_preview(input_clip, output_clip, preset, start_seconds=-1.0)

    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        generate_preview(
            input_clip, output_clip, preset, start_seconds=2.0, end_seconds=1.0
        )

    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        generate_preview(
            input_clip, output_clip, preset, start_seconds=2.0, end_seconds=2.0
        )


def test_generate_preview_audio_stream_copy_vs_reencode(tmp_path: Path) -> None:
    """Verify audio is stream-copied when start_seconds==0, but re-encoded when windowed."""
    clip = generate_clip(3.0, has_video=True, has_audio=True, output_dir=tmp_path)
    out_copy = tmp_path / "preview_copy.mp4"
    out_windowed = tmp_path / "preview_windowed.mp4"

    generate_preview(clip, out_copy, PREVIEW_PRESETS["small_video"], start_seconds=0.0)
    assert out_copy.is_file()
    assert_playable(out_copy)
    info_copy = open_and_inspect(out_copy)
    assert info_copy.has_audio is True

    generate_preview(
        clip, out_windowed, PREVIEW_PRESETS["small_video"], start_seconds=1.0
    )
    assert out_windowed.is_file()
    assert_playable(out_windowed)
    info_win = open_and_inspect(out_windowed)
    assert info_win.has_audio is True
