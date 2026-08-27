"""Opening title slate, outro slate, and audio jingle rendering module for VEditor pipeline.

Renders high-definition title slates (event name, talk title, speakers, room/date,
and event logo) and outro slates, muxing an opening/closing audio jingle to produce
standard introductory and concluding video segments for conference talks.
"""

from __future__ import annotations

import logging
import textwrap
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Loads a scalable TrueType font with graceful fallback."""
    font_candidates = (
        ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans.ttf"]
        if bold
        else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )
    for name in font_candidates:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _create_title_slate_image(
    title: str,
    speakers: list[str] | str,
    event_name: str,
    room_date: str,
    logo_path: Path | str | None,
    resolution: tuple[int, int],
) -> np.ndarray:
    """Renders an intro title slate RGB numpy array."""
    width, height = resolution
    img = Image.new("RGB", (width, height), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)

    # 1. Subtle vertical gradient background (deep slate -> dark indigo)
    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(15 + (26 - 15) * ratio)
        g = int(23 + (32 - 23) * ratio)
        b = int(42 + (58 - 42) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    # Scalable typography based on frame height
    event_font = _get_font(int(height * 0.038), bold=True)
    title_font = _get_font(int(height * 0.056), bold=True)
    speaker_font = _get_font(int(height * 0.042), bold=False)
    meta_font = _get_font(int(height * 0.032), bold=False)

    content_x = int(width * 0.08)
    content_y = int(height * 0.14)

    # 2. Composite event logo if supplied
    if logo_path:
        lp = Path(logo_path)
        if lp.is_file():
            try:
                with Image.open(lp) as logo_img:
                    logo_rgba = logo_img.convert("RGBA")
                    max_logo_w = int(width * 0.22)
                    max_logo_h = int(height * 0.16)
                    logo_rgba.thumbnail(
                        (max_logo_w, max_logo_h), Image.Resampling.LANCZOS
                    )
                    logo_x = width - content_x - logo_rgba.width
                    img.paste(logo_rgba, (logo_x, content_y), mask=logo_rgba)
            except (OSError, ValueError) as exc:
                logger.warning("Failed to composite logo image %s: %s", logo_path, exc)

    y_offset = content_y

    # 3. Draw Event Name
    if event_name:
        draw.text(
            (content_x, y_offset),
            event_name.upper(),
            font=event_font,
            fill=(56, 189, 248),  # Sky blue accent
        )
        y_offset += int(height * 0.08)

    # 4. Draw Talk Title (wrapped)
    title_text = _wrap_text(title, max_chars=36)
    draw.text(
        (content_x, y_offset),
        title_text,
        font=title_font,
        fill=(255, 255, 255),  # Pure white
        spacing=int(height * 0.015),
    )
    title_lines = title_text.count("\n") + 1
    y_offset += title_lines * int(height * 0.075) + int(height * 0.04)

    # 5. Draw Speaker Names
    if isinstance(speakers, (list, tuple)):
        speaker_str = ", ".join(speakers)
    else:
        speaker_str = str(speakers)

    if speaker_str:
        draw.text(
            (content_x, y_offset),
            f"Speaker: {speaker_str}",
            font=speaker_font,
            fill=(203, 213, 225),  # Light slate
        )
        y_offset += int(height * 0.075)

    # 6. Draw Room & Date
    if room_date:
        draw.text(
            (content_x, y_offset),
            room_date,
            font=meta_font,
            fill=(148, 163, 184),  # Muted slate
        )

    return np.array(img)


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

    event_font = _get_font(int(height * 0.042), bold=True)
    main_font = _get_font(int(height * 0.072), bold=True)
    sub_font = _get_font(int(height * 0.038), bold=False)

    center_x = width // 2

    # Optional centered logo at top
    start_y = int(height * 0.22)
    if logo_path:
        lp = Path(logo_path)
        if lp.is_file():
            try:
                with Image.open(lp) as logo_img:
                    logo_rgba = logo_img.convert("RGBA")
                    max_logo_w = int(width * 0.25)
                    max_logo_h = int(height * 0.18)
                    logo_rgba.thumbnail(
                        (max_logo_w, max_logo_h), Image.Resampling.LANCZOS
                    )
                    logo_x = center_x - (logo_rgba.width // 2)
                    img.paste(logo_rgba, (logo_x, int(height * 0.12)), mask=logo_rgba)
                    start_y = int(height * 0.38)
            except (OSError, ValueError) as exc:
                logger.warning("Failed to composite outro logo: %s", exc)

    if event_name:
        draw.text(
            (center_x, start_y),
            event_name.upper(),
            font=event_font,
            fill=(56, 189, 248),
            anchor="mm",
        )
        start_y += int(height * 0.13)

    draw.text(
        (center_x, start_y),
        thank_you_text,
        font=main_font,
        fill=(255, 255, 255),
        anchor="mm",
    )
    start_y += int(height * 0.15)

    if website_or_links:
        draw.text(
            (center_x, start_y),
            website_or_links,
            font=sub_font,
            fill=(203, 213, 225),
            anchor="mm",
        )

    return np.array(img)


def _wrap_text(text: str, max_chars: int = 40) -> str:
    """Wraps text across multiple lines for title display."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if paragraph.strip():
            lines.extend(textwrap.wrap(paragraph, width=max_chars))
    return "\n".join(lines)


def _get_audio_samples(
    jingle_path: Path | str | None,
    duration_s: float,
    sample_rate: int = 44100,
) -> np.ndarray:
    """Decodes or synthesizes stereo int16 audio samples of shape (2, N)."""
    total_samples = int(duration_s * sample_rate)

    if jingle_path:
        jp = Path(jingle_path)
        if not jp.is_file():
            raise FileNotFoundError(f"Audio jingle file not found: {jp}")

        samples_left: list[np.ndarray] = []
        samples_right: list[np.ndarray] = []

        with av.open(str(jp)) as container:
            if container.streams.audio:
                for packet in container.demux(container.streams.audio[0]):
                    for frame in packet.decode():
                        arr = frame.to_ndarray()
                        if "s16" in frame.format.name:
                            arr = arr.astype(np.float64) / 32768.0
                        elif "flt" in frame.format.name:
                            arr = arr.astype(np.float64)

                        if arr.ndim == 1:
                            samples_left.append(arr)
                            samples_right.append(arr)
                        elif arr.shape[0] >= 2:
                            samples_left.append(arr[0])
                            samples_right.append(arr[1])
                        else:
                            samples_left.append(arr[0])
                            samples_right.append(arr[0])

        if samples_left:
            left = np.concatenate(samples_left)
            right = np.concatenate(samples_right)

            if len(left) < total_samples:
                repeats = (total_samples // len(left)) + 1
                left = np.tile(left, repeats)[:total_samples]
                right = np.tile(right, repeats)[:total_samples]
            else:
                left = left[:total_samples]
                right = right[:total_samples]

            # Smooth fade out over the last 0.4s
            fade_samples = min(total_samples, int(0.4 * sample_rate))
            if fade_samples > 0:
                fade_curve = np.linspace(1.0, 0.0, fade_samples)
                left[-fade_samples:] *= fade_curve
                right[-fade_samples:] *= fade_curve

            int_left = (np.clip(left, -1.0, 1.0) * 32767).astype(np.int16)
            int_right = (np.clip(right, -1.0, 1.0) * 32767).astype(np.int16)
            return np.vstack([int_left, int_right])

    # Default gentle opening chime (harmonic tone)
    t = np.arange(total_samples, dtype=np.float64) / sample_rate
    audio_sig = 0.25 * np.sin(2 * np.pi * 523.25 * t) * np.exp(-1.2 * t)
    audio_int16 = (np.clip(audio_sig, -1.0, 1.0) * 32767).astype(np.int16)
    return np.vstack([audio_int16, audio_int16])


def _render_video_and_audio(
    output_path: Path | str,
    slate_ndarray: np.ndarray,
    audio_samples: np.ndarray,
    duration_seconds: float,
    resolution: tuple[int, int],
    fps: int,
) -> None:
    """Encodes slate frame and audio samples into an MP4 container."""
    out_path = Path(output_path)

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sample_rate = 44100
    total_video_frames = round(duration_seconds * fps)
    total_audio_samples = audio_samples.shape[1]

    with av.open(str(out_path), mode="w", format="mp4") as out_container:
        # Configure video stream
        out_v = out_container.add_stream(
            "libx264",
            rate=fps,
            options={"crf": "20", "preset": "veryfast"},
        )
        out_v.width = resolution[0]
        out_v.height = resolution[1]
        out_v.pix_fmt = "yuv420p"

        # Configure audio stream
        out_a = out_container.add_stream("aac", rate=sample_rate)
        out_a.layout = "stereo"

        # Encode video frames
        for frame_idx in range(total_video_frames):
            v_frame = av.VideoFrame.from_ndarray(slate_ndarray, format="rgb24")
            v_frame.pts = frame_idx
            v_frame.time_base = Fraction(1, fps)
            for packet in out_v.encode(v_frame):
                out_container.mux(packet)

        for packet in out_v.encode():
            out_container.mux(packet)

        # Encode audio chunks
        chunk_size = 1024
        audio_pts = 0
        for start_idx in range(0, total_audio_samples, chunk_size):
            chunk = audio_samples[:, start_idx : start_idx + chunk_size]
            if chunk.shape[1] == 0:
                continue

            a_frame = av.AudioFrame.from_ndarray(chunk, format="s16p", layout="stereo")
            a_frame.sample_rate = sample_rate
            a_frame.pts = audio_pts
            a_frame.time_base = Fraction(1, sample_rate)
            audio_pts += chunk.shape[1]

            for packet in out_a.encode(a_frame):
                out_container.mux(packet)

        for packet in out_a.encode():
            out_container.mux(packet)


def generate_intro_clip(
    output_path: Path | str,
    title: str,
    speakers: list[str] | str,
    event_name: str = "",
    room_date: str = "",
    logo_path: Path | str | None = None,
    audio_jingle_path: Path | str | None = None,
    duration_seconds: float = 4.0,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
) -> None:
    """Generates an opening title slate video clip with synchronized audio."""
    if duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be positive, got {duration_seconds}")
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise ValueError(f"resolution must be positive, got {resolution}")

    if logo_path is not None:
        lp = Path(logo_path)
        if not lp.is_file():
            raise FileNotFoundError(f"Logo file not found: {lp}")

    slate_ndarray = _create_title_slate_image(
        title=title,
        speakers=speakers,
        event_name=event_name,
        room_date=room_date,
        logo_path=logo_path,
        resolution=resolution,
    )

    audio_samples = _get_audio_samples(
        jingle_path=audio_jingle_path,
        duration_s=duration_seconds,
    )

    _render_video_and_audio(
        output_path=output_path,
        slate_ndarray=slate_ndarray,
        audio_samples=audio_samples,
        duration_seconds=duration_seconds,
        resolution=resolution,
        fps=fps,
    )


def generate_outro_clip(
    output_path: Path | str,
    event_name: str = "",
    thank_you_text: str = "Thank You For Watching!",
    website_or_links: str = "eventyay.com • fossasia.org",
    logo_path: Path | str | None = None,
    audio_jingle_path: Path | str | None = None,
    duration_seconds: float = 3.5,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
) -> None:
    """Generates a closing outro video clip with event branding and links."""
    if duration_seconds <= 0:
        raise ValueError(f"duration_seconds must be positive, got {duration_seconds}")
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise ValueError(f"resolution must be positive, got {resolution}")

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
    )

    _render_video_and_audio(
        output_path=output_path,
        slate_ndarray=slate_ndarray,
        audio_samples=audio_samples,
        duration_seconds=duration_seconds,
        resolution=resolution,
        fps=fps,
    )
