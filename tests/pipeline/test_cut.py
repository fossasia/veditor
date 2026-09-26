from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.pipeline.cut import (
    CutStrategy,
    _add_video_stream,
    _resolve_audio_encoder,
    _resolve_video_encoder,
    cut,
)
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

    # Invalid thread count (zero or negative)
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        cut(valid_clip, output_clip, 0.0, 1.0, threads=0)
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        cut(valid_clip, output_clip, 0.0, 1.0, threads=-1)


def test_resolve_encoders():
    """Verify decoder-to-encoder mapping for video and audio."""
    assert _resolve_video_encoder("h264") == "libx264"
    assert _resolve_video_encoder("hevc") == "libx265"
    assert _resolve_video_encoder("vp9") == "libvpx-vp9"
    assert _resolve_video_encoder("unknown_codec") == "libx264"
    assert _resolve_video_encoder(None) == "libx264"

    assert _resolve_audio_encoder("aac") == "aac"
    assert _resolve_audio_encoder("mp3") == "libmp3lame"
    assert _resolve_audio_encoder("opus") == "libopus"
    assert _resolve_audio_encoder("unknown_audio") == "aac"
    assert _resolve_audio_encoder(None) == "aac"


def test_cut_reencode_video_and_audio(tmp_path: Path):
    """Verify forced re-encode path handles both video and audio streams seamlessly."""
    source_clip = generate_clip(
        5.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "cut_reencode_both.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.0,
        end_seconds=4.0,
        force_reencode=True,
    )

    assert strategy == CutStrategy.RE_ENCODE
    assert output_clip.is_file()

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration is not None
    assert abs(info.duration - 3.0) <= 0.5
    assert_playable(output_clip)


def test_cut_rejects_identical_paths(tmp_path: Path):
    """Verify cut rejects identical input and output paths to prevent data truncation."""
    source_clip = generate_clip(3.0, output_dir=tmp_path)

    with pytest.raises(ValueError, match="Input and output paths must be different"):
        cut(source_clip, source_clip, 0.5, 2.5)

    # Verify original file remains intact and not truncated
    assert source_clip.stat().st_size > 0
    assert_playable(source_clip)


def test_cut_reencode_container_codec_fallback(tmp_path: Path):
    """Verify fallback encoder handles container incompatibilities without crashing."""
    # Create an input clip
    source_clip = generate_clip(3.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_fallback.mp4"

    # Cutting into MP4 will resolve libx264/aac even if input codecs or fallbacks are invoked
    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=0.5,
        end_seconds=2.5,
        force_reencode=True,
    )

    assert strategy == CutStrategy.RE_ENCODE
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_cut_reencode_with_threads(tmp_path: Path):
    """Verify cut re-encode works with explicit thread constraints."""
    source_clip = generate_clip(2.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_threads.mp4"
    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=0.2,
        end_seconds=1.2,
        force_reencode=True,
        threads=1,
    )
    assert strategy == CutStrategy.RE_ENCODE
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_add_video_stream_fallback_preserves_options_and_defaults_preset():
    """Verify fallback to libx264 preserves options and defaults preset to veryfast."""
    # Scenario 1: options is None -> defaults to {"preset": "veryfast"}
    container_1 = MagicMock()
    container_1.add_stream.side_effect = [ValueError("unsupported"), MagicMock()]
    _add_video_stream(container_1, "unknown_codec", rate=24, options=None)
    container_1.add_stream.assert_called_with(
        "libx264", rate=24, options={"preset": "veryfast"}
    )

    # Scenario 2: options with threads -> preserves threads and adds preset="veryfast"
    container_2 = MagicMock()
    container_2.add_stream.side_effect = [ValueError("unsupported"), MagicMock()]
    _add_video_stream(container_2, "unknown_codec", rate=24, options={"threads": "2"})
    container_2.add_stream.assert_called_with(
        "libx264", rate=24, options={"threads": "2", "preset": "veryfast"}
    )

    # Scenario 3: explicit preset provided -> preserves existing preset
    container_3 = MagicMock()
    container_3.add_stream.side_effect = [ValueError("unsupported"), MagicMock()]
    _add_video_stream(
        container_3,
        "unknown_codec",
        rate=24,
        options={"preset": "medium", "threads": "1"},
    )
    container_3.add_stream.assert_called_with(
        "libx264", rate=24, options={"preset": "medium", "threads": "1"}
    )
