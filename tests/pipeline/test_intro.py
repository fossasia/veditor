from pathlib import Path

import av
import pytest
from PIL import Image, ImageDraw

from app.pipeline.intro import generate_intro_clip
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def _inspect_stream_durations(path: Path | str) -> tuple[float, float]:
    """Inspects individual video and audio stream durations in seconds."""
    with av.open(str(path)) as container:
        v_dur = 0.0
        a_dur = 0.0
        if container.streams.video:
            s = container.streams.video[0]
            if s.duration and s.time_base:
                v_dur = float(s.duration * s.time_base)
            elif container.duration:
                v_dur = float(container.duration / av.time_base)
        if container.streams.audio:
            s = container.streams.audio[0]
            if s.duration and s.time_base:
                a_dur = float(s.duration * s.time_base)
            elif container.duration:
                a_dur = float(container.duration / av.time_base)
        return v_dur, a_dur


def test_generate_intro_clip_basic(tmp_path: Path):
    """Verify basic intro clip generation with default audio chime and stream sync."""
    output_clip = tmp_path / "intro_basic.mp4"

    generate_intro_clip(
        output_path=output_clip,
        title="Open Source Video Editing Pipelines",
        speakers="John Doe",
        event_name="FOSSASIA Summit 2026",
        room_date="Hall A • March 2026",
        duration_seconds=3.0,
        resolution=(1280, 720),
        fps=24,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.resolution == (1280, 720)
    assert info.duration is not None
    assert abs(info.duration - 3.0) <= 0.5
    assert "h264" in info.codec_names
    assert "aac" in info.codec_names

    v_dur, a_dur = _inspect_stream_durations(output_clip)
    assert abs(v_dur - 3.0) <= 0.3
    assert abs(a_dur - 3.0) <= 0.3
    assert abs(v_dur - a_dur) <= 0.2


def test_generate_intro_clip_with_logo_and_jingle(tmp_path: Path):
    """Verify intro clip generation with composited logo and external audio jingle."""
    logo_path = tmp_path / "event_logo.png"
    logo_img = Image.new("RGBA", (150, 80), (0, 0, 0, 0))
    d = ImageDraw.Draw(logo_img)
    d.rectangle([(0, 0), (150, 80)], fill=(56, 189, 248, 220))
    logo_img.save(logo_path)

    jingle_path = generate_clip(
        2.0,
        has_video=False,
        has_audio=True,
        audio_waveform="tone",
        output_dir=tmp_path,
    )

    output_clip = tmp_path / "intro_with_assets.mp4"

    generate_intro_clip(
        output_path=output_clip,
        title="Building Distributed Media Workers",
        speakers=["Alice Smith", "Bob Jones"],
        event_name="Eventyay Conference",
        room_date="Main Stage",
        logo_path=logo_path,
        audio_jingle_path=jingle_path,
        duration_seconds=2.5,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.resolution == (1920, 1080)
    assert info.duration is not None
    assert abs(info.duration - 2.5) <= 0.5


def test_generate_intro_with_48khz_jingle(tmp_path: Path):
    """Verify external 48 kHz broadcast audio jingle resamples cleanly to 44.1 kHz."""
    jingle_48k = generate_clip(
        2.0,
        has_video=False,
        has_audio=True,
        sample_rate=48000,
        audio_waveform="tone",
        output_dir=tmp_path,
    )

    output_clip = tmp_path / "intro_48k.mp4"
    generate_intro_clip(
        output_path=output_clip,
        title="48kHz Audio Resample Test",
        speakers="Audio Engineer",
        audio_jingle_path=jingle_48k,
        duration_seconds=2.0,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_audio is True
    assert abs(info.duration - 2.0) <= 0.5


def test_generate_intro_clip_metadata_dict(tmp_path: Path):
    """Verify talk_metadata dictionary ingestion and empty path tolerance."""
    output_clip = tmp_path / "intro_dict.mp4"

    metadata = {
        "title": "A Very Long Title That Tests Pixel Width Wrapping Across Multiple Lines",
        "speakers": ["Dev A", "Dev B"],
        "event_name": "FOSSASIA",
        "room_date": "Online • 2026",
        "duration": 2.0,
        "logo_path": "",
        "audio_jingle_path": "",
    }

    generate_intro_clip(
        output_path=output_clip,
        talk_metadata=metadata,
        logo_path="",
        audio_jingle_path="",
    )

    assert output_clip.is_file()
    assert_playable(output_clip)


def test_generate_intro_clip_small_resolution(tmp_path: Path):
    """Verify small custom resolution clamps font sizes without raising ImageFont ValueError."""
    output_clip = tmp_path / "intro_small.mp4"

    generate_intro_clip(
        output_path=output_clip,
        title="Small",
        speakers="S",
        duration_seconds=1.0,
        resolution=(160, 90),
    )

    assert output_clip.is_file()
    assert_playable(output_clip)


def test_generate_intro_clip_invalid_arguments(tmp_path: Path):
    """Verify error handling on invalid parameters."""
    output_clip = tmp_path / "invalid.mp4"

    with pytest.raises(ValueError, match="duration_seconds must be positive"):
        generate_intro_clip(
            output_path=output_clip,
            title="Invalid",
            speakers="Speaker",
            duration_seconds=-1.0,
        )

    with pytest.raises(ValueError, match="resolution must be at least 16x16"):
        generate_intro_clip(
            output_path=output_clip,
            title="Invalid",
            speakers="Speaker",
            resolution=(10, 10),
        )

    with pytest.raises(ValueError, match="threads must be positive"):
        generate_intro_clip(
            output_path=output_clip,
            title="Invalid",
            speakers="Speaker",
            threads=0,
        )

    with pytest.raises(FileNotFoundError, match="Logo file not found"):
        generate_intro_clip(
            output_path=output_clip,
            title="Invalid",
            speakers="Speaker",
            logo_path=tmp_path / "nonexistent_logo.png",
        )
