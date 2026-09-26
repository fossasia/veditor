from pathlib import Path

import pytest

from app.pipeline.outro import generate_outro_clip
from tests.conftest import (
    assert_playable,
    open_and_inspect,
)


def test_generate_outro_clip_basic(tmp_path: Path):
    """Verify outro clip generation with branding, links, and audio chime."""
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


def test_generate_outro_clip_metadata_dict(tmp_path: Path):
    """Verify outro clip generation with talk_metadata dict."""
    output_clip = tmp_path / "outro_dict.mp4"

    metadata = {
        "event_name": "Eventyay Conference",
        "thank_you_text": "A Very Long Outro Thank You Message That Tests Pixel Width Wrapping Across Multiple Centered Lines",
        "website_or_links": "https://eventyay.com • https://fossasia.org",
        "duration": 2.5,
        "logo_path": "",
        "audio_jingle_path": "",
    }

    generate_outro_clip(
        output_path=output_clip,
        talk_metadata=metadata,
    )

    assert output_clip.is_file()
    assert_playable(output_clip)


def test_generate_outro_clip_small_resolution(tmp_path: Path):
    """Verify outro clip on small resolution clamps font sizes gracefully."""
    output_clip = tmp_path / "outro_small.mp4"

    generate_outro_clip(
        output_path=output_clip,
        event_name="FOSSASIA",
        thank_you_text="Thanks!",
        website_or_links="eventyay.com",
        duration_seconds=1.5,
        resolution=(160, 90),
    )

    assert output_clip.is_file()
    assert_playable(output_clip)


def test_generate_outro_clip_invalid_arguments(tmp_path: Path):
    """Verify error handling on invalid outro arguments."""
    output_clip = tmp_path / "invalid_outro.mp4"

    with pytest.raises(ValueError, match="duration_seconds must be positive"):
        generate_outro_clip(
            output_path=output_clip,
            duration_seconds=-1.0,
        )

    with pytest.raises(ValueError, match="resolution must be at least 16x16"):
        generate_outro_clip(
            output_path=output_clip,
            resolution=(8, 8),
        )

    with pytest.raises(ValueError, match="threads must be positive"):
        generate_outro_clip(
            output_path=output_clip,
            threads=-1,
        )
