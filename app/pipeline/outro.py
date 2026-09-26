"""Closing outro slate rendering module for VEditor pipeline.

Renders high-definition outro slates (event branding, thank you messages, links,
and sponsor/event logo) with closing audio jingles for conference talk videos.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from app.pipeline.intro import (
    _get_audio_samples,
    _get_font,
    _render_video_and_audio,
    _wrap_text_to_width,
)

logger = logging.getLogger(__name__)


def _create_outro_slate_image(
    event_name: str,
    thank_you_text: str,
    website_or_links: str,
    logo_path: Path | str | None,
    resolution: tuple[int, int],
) -> np.ndarray:
    """Renders a centered outro slate RGB numpy array."""
    width, height = resolution
    img = Image.new("RGB", (width, height), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)

    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(15 + (26 - 15) * ratio)
        g = int(23 + (32 - 23) * ratio)
        b = int(42 + (58 - 42) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    event_font = _get_font(max(1, int(height * 0.042)), bold=True)
    main_font = _get_font(max(1, int(height * 0.072)), bold=True)
    sub_font = _get_font(max(1, int(height * 0.038)), bold=False)

    center_x = width // 2
    max_text_width = max(1, int(width * 0.85))

    # Optional centered logo at top
    start_y = int(height * 0.22)
    if logo_path:
        lp = Path(logo_path)
        if lp.is_file():
            try:
                with Image.open(lp) as logo_img:
                    logo_rgba = logo_img.convert("RGBA")
                    max_logo_w = max(1, int(width * 0.25))
                    max_logo_h = max(1, int(height * 0.18))
                    logo_rgba.thumbnail(
                        (max_logo_w, max_logo_h), Image.Resampling.LANCZOS
                    )
                    logo_x = center_x - (logo_rgba.width // 2)
                    img.paste(logo_rgba, (logo_x, int(height * 0.12)), mask=logo_rgba)
                    start_y = int(height * 0.38)
            except (OSError, ValueError) as exc:
                logger.warning("Failed to composite outro logo: %s", exc)

    if event_name:
        event_wrapped = _wrap_text_to_width(
            event_name.upper(), event_font, max_text_width, draw
        )
        draw.text(
            (center_x, start_y),
            event_wrapped,
            font=event_font,
            fill=(56, 189, 248),
            anchor="mm",
            align="center",
        )
        start_y += int(height * 0.13)

    if thank_you_text:
        thank_wrapped = _wrap_text_to_width(
            thank_you_text, main_font, max_text_width, draw
        )
        draw.text(
            (center_x, start_y),
            thank_wrapped,
            font=main_font,
            fill=(255, 255, 255),
            anchor="mm",
            align="center",
        )
        start_y += int(height * 0.15)

    if website_or_links:
        links_wrapped = _wrap_text_to_width(
            website_or_links, sub_font, max_text_width, draw
        )
        draw.text(
            (center_x, start_y),
            links_wrapped,
            font=sub_font,
            fill=(203, 213, 225),
            anchor="mm",
            align="center",
        )

    return np.array(img)


def generate_outro_clip(
    output_path: Path | str,
    talk_metadata: dict[str, Any] | str | None = None,
    *,
    event_name: str = "",
    thank_you_text: str = "Thank You For Watching!",
    website_or_links: str = "eventyay.com • fossasia.org",
    logo_path: Path | str | None = None,
    audio_jingle_path: Path | str | None = None,
    duration_seconds: float = 3.5,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
    threads: int | None = None,
    audio_sample_rate: int = 44100,
) -> None:
    """Generates a closing outro video clip with event branding and links.

    Args:
        output_path: Destination path for the rendered MP4 outro clip.
        talk_metadata: Metadata dictionary containing talk/event attributes, or event name string.
        event_name: Conference or event name.
        thank_you_text: Concluding message text.
        website_or_links: URL or event links text.
        logo_path: Optional path to logo image.
        audio_jingle_path: Optional path to closing audio jingle.
        duration_seconds: Target clip duration in seconds.
        resolution: Target video resolution tuple (width, height).
        fps: Target video frame rate.
    """
    if isinstance(talk_metadata, str):
        event_name = talk_metadata
        talk_metadata = None

    if isinstance(talk_metadata, dict):
        event_name = talk_metadata.get("event_name", event_name)
        thank_you_text = talk_metadata.get("thank_you_text", thank_you_text)
        website_or_links = talk_metadata.get("website_or_links", website_or_links)

        # Derive duration
        if "duration_seconds" in talk_metadata:
            duration_seconds = float(talk_metadata["duration_seconds"])
        elif "duration" in talk_metadata:
            duration_seconds = float(talk_metadata["duration"])

        # Derive target resolution
        if "resolution" in talk_metadata:
            resolution = tuple(talk_metadata["resolution"])  # type: ignore
        elif "width" in talk_metadata and "height" in talk_metadata:
            resolution = (int(talk_metadata["width"]), int(talk_metadata["height"]))
        elif "video_metadata" in talk_metadata and isinstance(
            talk_metadata["video_metadata"], dict
        ):
            vm = talk_metadata["video_metadata"]
            if "resolution" in vm:
                resolution = tuple(vm["resolution"])  # type: ignore
            elif "width" in vm and "height" in vm:
                resolution = (int(vm["width"]), int(vm["height"]))

        # Derive framerate
        if "fps" in talk_metadata:
            fps = int(talk_metadata["fps"])
        elif "framerate" in talk_metadata:
            fps = int(talk_metadata["framerate"])
        elif "video_metadata" in talk_metadata and isinstance(
            talk_metadata["video_metadata"], dict
        ):
            vm = talk_metadata["video_metadata"]
            if "fps" in vm:
                fps = int(vm["fps"])
            elif "framerate" in vm:
                fps = int(vm["framerate"])

        if "logo_path" in talk_metadata:
            logo_path = talk_metadata["logo_path"]
        if "audio_jingle_path" in talk_metadata:
            audio_jingle_path = talk_metadata["audio_jingle_path"]

    # Guard against empty string paths
    if logo_path == "":
        logo_path = None
    if audio_jingle_path == "":
        audio_jingle_path = None

    if duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be positive, got {duration_seconds}")
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if threads is not None and threads <= 0:
        raise ValueError(f"threads must be positive, got {threads}")
    if resolution[0] < 16 or resolution[1] < 16:
        raise ValueError(f"resolution must be at least 16x16, got {resolution}")

    if logo_path is not None:
        lp = Path(logo_path)
        if not lp.is_file():
            raise FileNotFoundError(f"Logo file not found: {lp}")

    slate_ndarray = _create_outro_slate_image(
        event_name=event_name,
        thank_you_text=thank_you_text,
        website_or_links=website_or_links,
        logo_path=logo_path,
        resolution=resolution,
    )

    audio_samples = _get_audio_samples(
        jingle_path=audio_jingle_path,
        duration_s=duration_seconds,
        sample_rate=audio_sample_rate,
    )

    _render_video_and_audio(
        output_path=output_path,
        slate_ndarray=slate_ndarray,
        audio_samples=audio_samples,
        duration_seconds=duration_seconds,
        resolution=resolution,
        fps=fps,
        threads=threads,
        sample_rate=audio_sample_rate,
    )
