from pathlib import Path

import pytest

from app.pipeline.transcode import (
    PRESET_720P,
    PRESET_1080P_DEFAULT,
    TranscodePreset,
    transcode,
)
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_transcode_default_preset(tmp_path: Path):
    """Verify transcoding with default 1080p preset produces valid, playable output."""
    source_clip = generate_clip(
        3.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_default.mp4"

    transcode(source_clip, output_clip)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info_source = open_and_inspect(source_clip)
    info_out = open_and_inspect(output_clip)

    assert info_out.has_video is True
    assert info_out.has_audio is True
    assert info_out.duration is not None
    assert info_source.duration is not None
    assert abs(info_out.duration - info_source.duration) <= 0.8
    assert "h264" in info_out.codec_names
    assert "aac" in info_out.codec_names


def test_transcode_with_preset_scaling(tmp_path: Path):
    """Verify transcoding with 720p preset respects scaling parameters."""
    source_clip = generate_clip(
        2.0,
        has_video=True,
        has_audio=True,
        resolution=(1920, 1080),
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "transcoded_720p.mp4"

    transcode(source_clip, output_clip, preset=PRESET_720P)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.resolution is not None
    assert info.resolution[0] <= 1280
    assert info.resolution[1] <= 720


def test_transcode_progress_callback(tmp_path: Path):
    """Verify on_progress is called monotonically and ends with 1.0."""
    source_clip = generate_clip(
        4.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_progress.mp4"

    progress_events: list[float] = []

    def on_progress(pct: float) -> None:
        progress_events.append(pct)

    transcode(
        source_clip,
        output_clip,
        preset=PRESET_1080P_DEFAULT,
        on_progress=on_progress,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    assert len(progress_events) >= 1
    assert progress_events[-1] == 1.0
    for val in progress_events:
        assert 0.0 <= val <= 1.0

    # Monotonically increasing
    assert all(
        progress_events[i] <= progress_events[i + 1]
        for i in range(len(progress_events) - 1)
    )


def test_transcode_audio_only(tmp_path: Path):
    """Verify audio-only media transcodes successfully."""
    source_clip = generate_clip(
        2.0,
        has_video=False,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "transcoded_audio_only.mp4"

    transcode(source_clip, output_clip)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is False
    assert info.has_audio is True


def test_transcode_video_only(tmp_path: Path):
    """Verify video-only media transcodes successfully."""
    source_clip = generate_clip(
        2.0, has_video=True, has_audio=False, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_video_only.mp4"

    transcode(source_clip, output_clip)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is False


def test_transcode_custom_preset_options(tmp_path: Path):
    """Verify custom TranscodePreset configuration."""
    custom_preset = TranscodePreset(
        name="custom_fast",
        video_codec="libx264",
        crf=28,
        video_bitrate=500_000,
        preset_speed="ultrafast",
        audio_codec="aac",
        audio_bitrate=64_000,
    )
    source_clip = generate_clip(1.5, output_dir=tmp_path)
    output_clip = tmp_path / "transcoded_custom.mp4"

    transcode(source_clip, output_clip, preset=custom_preset)

    assert output_clip.is_file()
    assert_playable(output_clip)


def test_transcode_invalid_arguments(tmp_path: Path):
    """Verify FileNotFoundError and error handling for missing files."""
    output_clip = tmp_path / "out.mp4"

    with pytest.raises(FileNotFoundError):
        transcode(tmp_path / "nonexistent.mp4", output_clip)
