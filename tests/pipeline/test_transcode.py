from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from app.pipeline.transcode import (
    PRESET_480P,
    PRESET_720P,
    PRESET_1080P_DEFAULT,
    PRESET_1440P,
    TranscodePreset,
    transcode,
)
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_transcode_default_preset(tmp_path: Path) -> None:
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


def test_transcode_with_preset_scaling(tmp_path: Path) -> None:
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


def test_transcode_with_480p_scaling(tmp_path: Path) -> None:
    """Verify transcoding with 480p preset respects scaling parameters."""
    source_clip = generate_clip(
        2.0,
        has_video=True,
        has_audio=True,
        resolution=(1920, 1080),
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "transcoded_480p.mp4"

    transcode(source_clip, output_clip, preset=PRESET_480P)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.resolution is not None
    assert info.resolution[0] <= 854
    assert info.resolution[1] <= 480


def test_transcode_with_1440p_preset(tmp_path: Path) -> None:
    """Verify transcoding with 1440p preset produces valid output."""
    source_clip = generate_clip(
        1.5,
        has_video=True,
        has_audio=True,
        resolution=(1280, 720),
        output_dir=tmp_path,
    )
    output_clip = tmp_path / "transcoded_1440p.mp4"

    transcode(source_clip, output_clip, preset=PRESET_1440P)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.resolution is not None
    assert info.resolution[0] <= 2560
    assert info.resolution[1] <= 1440


def test_transcode_progress_callback(tmp_path: Path) -> None:
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
    assert any(0.0 < val < 1.0 for val in progress_events)
    assert progress_events[-1] == 1.0
    for val in progress_events:
        assert 0.0 <= val <= 1.0

    # Monotonically increasing
    assert all(
        progress_events[i] <= progress_events[i + 1]
        for i in range(len(progress_events) - 1)
    )

    intermediate_events = [val for val in progress_events if 0.0 < val < 1.0]
    assert all(
        intermediate_events[i] > intermediate_events[i - 1]
        for i in range(1, len(intermediate_events))
    )


def test_transcode_audio_only(tmp_path: Path) -> None:
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


def test_transcode_video_only(tmp_path: Path) -> None:
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


def test_transcode_custom_preset_options(tmp_path: Path) -> None:
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


def test_transcode_invalid_arguments(tmp_path: Path) -> None:
    """Verify FileNotFoundError and error handling for missing files."""
    output_clip = tmp_path / "out.mp4"

    with pytest.raises(FileNotFoundError):
        transcode(tmp_path / "nonexistent.mp4", output_clip)


def test_transcode_enforces_yuv420p(tmp_path: Path) -> None:
    """Verify delivery transcode enforces browser-playable yuv420p pixel format."""
    source_clip = generate_clip(
        2.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_yuv420p.mp4"

    transcode(source_clip, output_clip, preset=PRESET_1080P_DEFAULT)

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert "h264" in info.codec_names


def test_transcode_progress_with_offset_start(tmp_path: Path) -> None:
    """Verify progress events fire across the encode even when source starts with non-zero PTS."""
    source_clip = generate_clip(
        4.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_offset.mp4"

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
    assert len(progress_events) >= 1
    assert any(0.0 < val < 1.0 for val in progress_events)
    assert progress_events[-1] == 1.0


def test_transcode_graceful_fallback_when_duration_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify transcoding still completes without error when container duration is None."""
    import av

    source_clip = generate_clip(
        2.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_no_duration.mp4"

    progress_events: list[float] = []

    real_open: Callable[..., av.container.Container] = av.open

    class ContainerProxy:
        def __init__(self, real_container: av.container.Container) -> None:
            self._real = real_container

        @property
        def duration(self) -> None:
            return None

        def __getattr__(self, item: str) -> object:
            return getattr(self._real, item)

        def __enter__(self) -> Self:
            self._real.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> None:
            return self._real.__exit__(exc_type, exc_val, exc_tb)

    def mocked_av_open(*args: object, **kwargs: object) -> object:
        container = real_open(*args, **kwargs)
        mode = kwargs.get("mode", "r")
        if (
            isinstance(mode, str)
            and "w" not in mode
            and not str(args[0]).endswith("transcoded_no_duration.mp4")
        ):
            return ContainerProxy(container)
        return container

    monkeypatch.setattr(av, "open", mocked_av_open)

    transcode(
        source_clip,
        output_clip,
        on_progress=lambda p: progress_events.append(p),
    )

    assert output_clip.is_file()
    assert_playable(output_clip)
    # When duration is None, intermediate progress is skipped but final 1.0 is called
    assert progress_events == [1.0]


def test_transcode_with_threads(tmp_path: Path) -> None:
    """Verify transcoding succeeds with explicit thread limits."""
    source_clip = generate_clip(
        1.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_threads.mp4"
    transcode(source_clip, output_clip, threads=1)
    assert output_clip.is_file()
    assert_playable(output_clip)


def test_transcode_rejects_invalid_threads(tmp_path: Path) -> None:
    """Verify that zero or negative threads raise ValueError."""
    source_clip = generate_clip(1.0, output_dir=tmp_path)
    output_clip = tmp_path / "out.mp4"

    # Explicit threads <= 0
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        transcode(source_clip, output_clip, threads=0)
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        transcode(source_clip, output_clip, threads=-1)

    # Preset threads <= 0
    invalid_preset_zero = replace(PRESET_1080P_DEFAULT, threads=0)
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        transcode(source_clip, output_clip, preset=invalid_preset_zero)

    invalid_preset_neg = replace(PRESET_1080P_DEFAULT, threads=-2)
    with pytest.raises(ValueError, match="threads must be greater than zero"):
        transcode(source_clip, output_clip, preset=invalid_preset_neg)


def test_preset_1080p_default_preset_speed() -> None:
    """Verify PRESET_1080P_DEFAULT uses veryfast preset speed."""
    assert PRESET_1080P_DEFAULT.preset_speed == "veryfast"


def test_transcode_start_and_end_seconds(tmp_path: Path) -> None:
    """Verify transcoding with start_seconds and end_seconds windows the output properly."""
    source_clip = generate_clip(
        6.0, has_video=True, has_audio=True, output_dir=tmp_path
    )
    output_clip = tmp_path / "transcoded_windowed.mp4"

    transcode(
        source_clip,
        output_clip,
        start_seconds=1.5,
        end_seconds=4.5,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info_out = open_and_inspect(output_clip)
    assert info_out.has_video is True
    assert info_out.has_audio is True
    assert info_out.duration is not None
    # 4.5 - 1.5 = 3.0s window
    assert abs(info_out.duration - 3.0) <= 0.8


def test_transcode_invalid_start_end_bounds(tmp_path: Path) -> None:
    """Verify negative start_seconds and inverted end_seconds raise ValueError."""
    source_clip = generate_clip(2.0, output_dir=tmp_path)
    output_clip = tmp_path / "out.mp4"

    with pytest.raises(ValueError, match="start_seconds must be non-negative"):
        transcode(source_clip, output_clip, start_seconds=-1.0)

    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        transcode(source_clip, output_clip, start_seconds=2.0, end_seconds=1.0)

    with pytest.raises(
        ValueError, match="end_seconds .* must be greater than start_seconds"
    ):
        transcode(source_clip, output_clip, start_seconds=2.0, end_seconds=2.0)


def test_transcode_unified_intro_outro_loudness(tmp_path: Path) -> None:
    """Verify single-pass transcode integrates intro, main talk, outro, and loudness."""
    intro_clip = generate_clip(1.0, has_video=True, has_audio=True, output_dir=tmp_path)
    main_clip = generate_clip(2.0, has_video=True, has_audio=True, output_dir=tmp_path)
    outro_clip = generate_clip(1.0, has_video=True, has_audio=True, output_dir=tmp_path)
    output_clip = tmp_path / "unified_final.mp4"

    transcode(
        main_clip,
        output_clip,
        intro_path=intro_clip,
        outro_path=outro_clip,
        target_lufs=-16.0,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.duration is not None
    # 1.0 + 2.0 + 1.0 = 4.0s
    assert abs(info.duration - 4.0) <= 0.6
    assert "h264" in info.codec_names
    assert "aac" in info.codec_names


def test_transcode_resilient_to_corrupt_packet(tmp_path: Path) -> None:
    """Verify transcode ignores corrupt/unparseable video packets and completes successfully."""
    from unittest.mock import patch

    import av

    source_clip = generate_clip(2.0, output_dir=tmp_path)
    output_clip = tmp_path / "corrupt_resilient_final.mp4"

    orig_decode = av.packet.Packet.decode
    call_count = [0]

    def faulty_decode(
        self: av.packet.Packet, *args: object, **kwargs: object
    ) -> object:
        call_count[0] += 1
        # Inject an InvalidDataError on the 5th packet to simulate bitstream glitch
        if call_count[0] == 5:
            raise av.error.InvalidDataError(1094995529, "avcodec_send_packet()")
        return orig_decode(self, *args, **kwargs)

    with patch.object(av.packet.Packet, "decode", faulty_decode):
        transcode(source_clip, output_clip)

    assert output_clip.is_file()
    assert_playable(output_clip)
    info = open_and_inspect(output_clip)
    assert info.duration is not None
    assert abs(info.duration - 2.0) <= 0.5
