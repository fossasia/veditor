from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.pipeline.intro import generate_intro_clip, generate_outro_clip
from tests.conftest import (
    assert_playable,
    generate_clip,
    open_and_inspect,
)


def test_generate_intro_clip_basic(tmp_path: Path):
    """Verify basic intro clip generation with default audio chime."""
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


def test_generate_outro_clip(tmp_path: Path):
    """Verify outro clip generation with branding and links."""
    output_clip = tmp_path / "outro.mp4"

    generate_outro_clip(
        output_path=output_clip,
        event_name="FOSSASIA Summit 2026",
        thank_you_text="Thank You For Attending!",
        website_or_links="eventyay.com • fossasia.org",
        duration_seconds=3.0,
        resolution=(1920, 1080),
        fps=24,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)

    info = open_and_inspect(output_clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.resolution == (1920, 1080)
    assert info.duration is not None
    assert abs(info.duration - 3.0) <= 0.5


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

    with pytest.raises(FileNotFoundError, match="Logo file not found"):
        generate_intro_clip(
            output_path=output_clip,
            title="Invalid",
            speakers="Speaker",
            logo_path=tmp_path / "nonexistent_logo.png",
        )
