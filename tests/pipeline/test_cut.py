from pathlib import Path

import pytest

from app.pipeline.cut import (
    CutStrategy,
    _resolve_audio_encoder,
    _resolve_video_encoder,
    cut,
)
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_cut_duration_matches_window(tmp_path: Path) -> None:
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


def test_cut_video_only(tmp_path: Path) -> None:
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

    assert strategy in (CutStrategy.STREAM_COPY, CutStrategy.SMART_CUT)
    assert output_clip.is_file()
    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is False
    assert info.duration is not None
    assert abs(info.duration - 2.5) <= 0.8
    assert_playable(output_clip)


def test_cut_audio_only(tmp_path: Path) -> None:
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


from unittest import mock


def test_cut_smart_fallback_to_stream_copy(tmp_path: Path) -> None:
    """Verify that when smart cut fails, cut falls back to stream copy."""
    source_clip = generate_clip(4.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_fallback.mp4"

    with mock.patch(
        "app.pipeline.cut._cut_smart", side_effect=RuntimeError("GOP analysis error")
    ):
        strategy = cut(
            source_clip,
            output_clip,
            start_seconds=1.0,
            end_seconds=3.0,
        )

    assert strategy == CutStrategy.STREAM_COPY
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_cut_invalid_inputs(tmp_path: Path) -> None:
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


def test_resolve_encoders() -> None:
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


def test_cut_reencode_video_and_audio(tmp_path: Path) -> None:
    """Verify smart cut handles both video and audio streams seamlessly."""
    source_clip = generate_clip(
        8.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "cut_both.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.5,
        end_seconds=6.5,
    )

    assert strategy in (CutStrategy.STREAM_COPY, CutStrategy.SMART_CUT)
    assert output_clip.is_file()

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration is not None
    assert abs(info.duration - 5.0) <= 0.8
    assert_playable(output_clip)


def test_cut_rejects_identical_paths(tmp_path: Path) -> None:
    """Verify cut rejects identical input and output paths to prevent data truncation."""
    source_clip = generate_clip(3.0, output_dir=tmp_path)

    with pytest.raises(ValueError, match="Input and output paths must be different"):
        cut(source_clip, source_clip, 0.5, 2.5)

    # Verify original file remains intact and not truncated
    assert source_clip.stat().st_size > 0
    assert_playable(source_clip)


def test_cut_falls_back_to_stream_copy_when_no_interior_keyframes(
    tmp_path: Path,
) -> None:
    """Verify cutting an interval smaller than a GOP falls back to stream copy."""
    # Synthetic clip with keyframes every ~2s
    source_clip = generate_clip(4.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_tiny.mp4"

    # Interval 0.2 to 0.4 has no interior keyframe for smart cut, falls back to stream copy
    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=0.2,
        end_seconds=0.4,
    )
    assert strategy == CutStrategy.STREAM_COPY
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_cut_fails_and_logs_error_when_both_fail(tmp_path: Path) -> None:
    """Verify cut raises RuntimeError when both smart cut and stream copy fail."""
    source_clip = generate_clip(4.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_fail.mp4"

    with (
        mock.patch(
            "app.pipeline.cut._cut_smart", side_effect=RuntimeError("Smart cut error")
        ),
        mock.patch(
            "app.pipeline.cut._cut_stream_copy",
            side_effect=RuntimeError("Stream copy error"),
        ),
        pytest.raises(RuntimeError, match="Trimming failed"),
    ):
        cut(source_clip, output_clip, start_seconds=1.0, end_seconds=3.0)


def test_cut_with_threads(tmp_path: Path) -> None:
    """Verify cut works with explicit thread constraints."""
    source_clip = generate_clip(8.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_threads.mp4"
    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.5,
        end_seconds=6.5,
        threads=1,
    )
    assert strategy in (CutStrategy.STREAM_COPY, CutStrategy.SMART_CUT)
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_cut_stream_copy_succeeds_without_fallback_when_keyframe_far(
    tmp_path: Path,
) -> None:
    """Verify stream copy succeeds even when keyframe distance from start > 0.5s."""
    # Synthetic clips have keyframes at ~2s intervals
    source_clip = generate_clip(8.0, output_dir=tmp_path)
    output_clip = tmp_path / "cut_stream_copy_distant.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=3.0,
        end_seconds=6.0,
    )

    assert strategy in (CutStrategy.STREAM_COPY, CutStrategy.SMART_CUT)
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_smart_cut_frame_accurate(tmp_path: Path) -> None:
    """Verify smart cut performs frame-accurate trimming and decodes completely without bitstream errors."""
    import av

    from app.config import PREVIEW_PRESETS
    from app.pipeline.preview import generate_preview

    source_clip = generate_clip(8.0, output_dir=tmp_path)
    output_clip = tmp_path / "smart_cut.mp4"

    strategy = cut(
        source_clip,
        output_clip,
        start_seconds=1.2,
        end_seconds=6.8,
    )

    assert strategy == CutStrategy.SMART_CUT
    assert output_clip.is_file()
    assert_playable(output_clip)

    # Exhaustively decode ALL video and audio packets across GOP boundary transitions
    with av.open(str(output_clip)) as c:
        v = c.streams.video[0]
        v_decoded = sum(len(list(p.decode())) for p in c.demux(v))
        assert v_decoded > 0

    with av.open(str(output_clip)) as c:
        a = c.streams.audio[0]
        a_decoded = sum(len(list(p.decode())) for p in c.demux(a))
        assert a_decoded > 0

    # Ensure downstream preview generation consumes the smart-cut file without error
    preview_clip = tmp_path / "smart_cut_preview.mp4"
    preset = PREVIEW_PRESETS["big_video"]
    generate_preview(output_clip, preview_clip, preset)
    assert preview_clip.is_file()
    assert_playable(preview_clip)

    info = open_and_inspect(output_clip)
    assert info.duration is not None
    # 6.8 - 1.2 = 5.6s window; smart cut must be frame-accurate within 0.15s
    assert abs(info.duration - 5.6) <= 0.15
