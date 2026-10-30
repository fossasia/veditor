"""Unit tests for the pure waveform peak extractor (pipeline/waveform.py)."""

from pathlib import Path

from app.pipeline.waveform import extract_waveform_peaks
from tests.conftest import (
    generate_clip,
    generate_corrupt_clip,
)


def test_extract_waveform_peaks_audio_clip(tmp_path: Path) -> None:
    """Verify waveform extraction on synthetic audio clip produces bounded peaks."""
    clip = generate_clip(
        2.0,
        has_video=True,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path,
    )
    peaks = extract_waveform_peaks(clip, num_peaks=64)

    assert len(peaks) == 64
    assert all(isinstance(p, float) for p in peaks)
    assert all(0.0 <= p <= 1.0 for p in peaks)
    # Synthetic tone should have non-zero peaks
    assert max(peaks) > 0.0


def test_extract_waveform_peaks_video_only(tmp_path: Path) -> None:
    """Verify waveform extraction returns empty list for media with no audio."""
    clip = generate_clip(
        1.0,
        has_video=True,
        has_audio=False,
        output_dir=tmp_path,
    )
    peaks = extract_waveform_peaks(clip, num_peaks=50)
    assert peaks == []


def test_extract_waveform_peaks_corrupt_clip(tmp_path: Path) -> None:
    """Verify waveform extraction returns empty list gracefully on corrupt input."""
    corrupt = generate_corrupt_clip(output_dir=tmp_path)
    peaks = extract_waveform_peaks(corrupt)
    assert peaks == []


def test_extract_waveform_peaks_nonexistent_file(tmp_path: Path) -> None:
    """Verify waveform extraction handles non-existent paths gracefully."""
    nonexistent = tmp_path / "does_not_exist.mp4"
    peaks = extract_waveform_peaks(nonexistent)
    assert peaks == []


def test_extract_waveform_peaks_num_peaks_scaling(tmp_path: Path) -> None:
    """Verify requested peak count is respected for various bucket sizes."""
    clip = generate_clip(
        3.0,
        has_video=False,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path,
    )
    for target_peaks in (10, 50, 128):
        peaks = extract_waveform_peaks(clip, num_peaks=target_peaks)
        assert len(peaks) == target_peaks
        assert max(peaks) == 1.0  # Normalized peak
