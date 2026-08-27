import math
from pathlib import Path

import av
import numpy as np
import pytest

from app.pipeline.loudness import normalize
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def _measure_audio_rms_db(path: Path | str) -> float:
    """Independent measurement of audio RMS power in dBFS."""
    with av.open(str(path)) as container:
        assert container.streams.audio, "No audio stream found for measurement"
        samples = []
        for packet in container.demux(container.streams.audio[0]):
            for frame in packet.decode():
                arr = frame.to_ndarray()
                if "s16" in frame.format.name:
                    arr = arr.astype(np.float64) / 32768.0
                elif "flt" in frame.format.name:
                    arr = arr.astype(np.float64)
                samples.append(arr.flatten())
        data = np.concatenate(samples)
        rms = np.sqrt(np.mean(data**2))
        return float(20 * np.log10(rms + 1e-12))


def test_normalize_moves_loudness_towards_target(tmp_path: Path):
    """Verify that normalizing an audio tone produces a valid, playable clip with measured loudness."""
    source_clip = generate_clip(3.0, audio_waveform="tone", output_dir=tmp_path)
    output_clip = tmp_path / "normalized.mp4"
    target_lufs = -16.0

    normalize(source_clip, output_clip, target_lufs=target_lufs)

    assert output_clip.is_file()
    assert_playable(output_clip)

    source_loudness = _measure_audio_rms_db(source_clip)
    output_loudness = _measure_audio_rms_db(output_clip)

    # Output loudness is finite and in expected audio range
    assert math.isfinite(source_loudness)
    assert math.isfinite(output_loudness)
    assert abs(output_loudness - target_lufs) <= 2.5

    info_source = open_and_inspect(source_clip)
    info_out = open_and_inspect(output_clip)

    assert info_out.has_audio is True
    assert info_out.duration is not None
    assert info_source.duration is not None
    assert abs(info_out.duration - info_source.duration) <= 0.8


def _count_video_packets(path: Path | str) -> int:
    """Count video packets in the first video stream."""
    with av.open(str(path)) as container:
        assert container.streams.video, "No video stream found"
        return sum(1 for packet in container.demux(container.streams.video[0]))


def test_normalize_preserves_video_stream(tmp_path: Path):
    """Verify that video stream properties and frame count are preserved without corruption."""
    source_clip = generate_clip(
        3.0,
        has_video=True,
        has_audio=True,
        resolution=(640, 480),
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "normalized_muxed.mp4"

    normalize(source_clip, output_clip, target_lufs=-18.0)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info_source = open_and_inspect(source_clip)
    info_out = open_and_inspect(output_clip)

    assert info_out.has_video is True
    assert info_out.has_audio is True
    assert info_out.resolution == (640, 480)
    assert "h264" in info_out.codec_names
    assert info_source.duration is not None
    assert info_out.duration is not None
    assert abs(info_out.duration - info_source.duration) <= 0.8
    # Exact video packet count must be identical (lossless stream copy)
    assert _count_video_packets(output_clip) == _count_video_packets(source_clip)


def test_normalize_audio_only(tmp_path: Path):
    """Verify that audio-only files (has_video=False) normalize correctly."""
    source_clip = generate_clip(
        3.0,
        has_video=False,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "normalized_audio_only.mp4"
    target_lufs = -14.0

    normalize(source_clip, output_clip, target_lufs=target_lufs)

    assert output_clip.is_file()
    assert_playable(output_clip)

    output_loudness = _measure_audio_rms_db(output_clip)
    assert math.isfinite(output_loudness)
    assert abs(output_loudness - target_lufs) <= 2.5

    info = open_and_inspect(output_clip)
    assert info.has_video is False
    assert info.has_audio is True


def test_normalize_no_audio_stream_raises(tmp_path: Path):
    """Verify ValueError is raised if the input media has no audio stream."""
    source_clip = generate_clip(
        2.0, has_video=True, has_audio=False, output_dir=tmp_path
    )
    output_clip = tmp_path / "invalid.mp4"

    with pytest.raises(ValueError, match="No audio stream found"):
        normalize(source_clip, output_clip)


def test_normalize_invalid_arguments(tmp_path: Path):
    """Verify FileNotFoundError and ValueError on invalid inputs including NaN/inf."""
    valid_clip = generate_clip(2.0, output_dir=tmp_path)
    output_clip = tmp_path / "out.mp4"

    with pytest.raises(FileNotFoundError):
        normalize(tmp_path / "nonexistent.mp4", output_clip)

    with pytest.raises(ValueError, match="target_lufs must be between"):
        normalize(valid_clip, output_clip, target_lufs=5.0)

    with pytest.raises(ValueError, match="target_lufs must be between"):
        normalize(valid_clip, output_clip, target_lufs=-80.0)

    with pytest.raises(ValueError, match="target_lufs must be between"):
        normalize(valid_clip, output_clip, target_lufs=float("nan"))

    with pytest.raises(ValueError, match="target_lufs must be between"):
        normalize(valid_clip, output_clip, target_lufs=float("inf"))
