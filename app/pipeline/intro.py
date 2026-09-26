"""Opening title slate, outro slate, and audio jingle rendering module for VEditor pipeline.

Renders high-definition title slates (event name, talk title, speakers, room/date,
and event logo) and audio jingles to produce standard introductory video segments for conference talks.
"""

from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Loads a scalable TrueType font with graceful fallback and clamped size."""
    clamped_size = max(1, size)
    font_candidates = (
        ["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "DejaVuSans.ttf"]
        if bold
        else ["DejaVuSans.ttf", "LiberationSans-Regular.ttf"]
    )
    for name in font_candidates:
        try:
            return ImageFont.truetype(name, size=clamped_size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=clamped_size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text_to_width(
    text: str,
    font: ImageFont.ImageFont,
    max_pixel_width: int,
    draw: ImageDraw.ImageDraw,
) -> str:
    """Wraps text across lines by measuring rendered bounding box pixel width."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        clean_para = paragraph.strip()
        if not clean_para:
            continue
        words = clean_para.split()
        if not words:
            continue
        current_line = words[0]
        for word in words[1:]:
            test_line = f"{current_line} {word}"
            bbox = draw.textbbox((0, 0), test_line, font=font)
            w = bbox[2] - bbox[0]
            if w <= max_pixel_width:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = word
        lines.append(current_line)
    return "\n".join(lines)


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

    # Scalable typography based on frame height (clamped to >= 1)
    event_font = _get_font(max(1, int(height * 0.038)), bold=True)
    title_font = _get_font(max(1, int(height * 0.056)), bold=True)
    speaker_font = _get_font(max(1, int(height * 0.042)), bold=False)
    meta_font = _get_font(max(1, int(height * 0.032)), bold=False)

    content_x = max(1, int(width * 0.08))
    content_y = max(1, int(height * 0.14))
    max_text_width = max(1, int(width * 0.84))

    # 2. Composite event logo if supplied
    if logo_path:
        lp = Path(logo_path)
        if lp.is_file():
            try:
                with Image.open(lp) as logo_img:
                    logo_rgba = logo_img.convert("RGBA")
                    max_logo_w = max(1, int(width * 0.22))
                    max_logo_h = max(1, int(height * 0.16))
                    logo_rgba.thumbnail(
                        (max_logo_w, max_logo_h), Image.Resampling.LANCZOS
                    )
                    logo_x = max(0, width - content_x - logo_rgba.width)
                    img.paste(logo_rgba, (logo_x, content_y), mask=logo_rgba)
                    max_text_width = max(1, logo_x - content_x - int(width * 0.04))
            except (OSError, ValueError) as exc:
                logger.warning("Failed to composite logo image %s: %s", logo_path, exc)

    y_offset = content_y

    # 3. Draw Event Name
    if event_name:
        event_wrapped = _wrap_text_to_width(
            event_name.upper(), event_font, max_text_width, draw
        )
        draw.text(
            (content_x, y_offset),
            event_wrapped,
            font=event_font,
            fill=(56, 189, 248),  # Sky blue accent
        )
        event_lines = event_wrapped.count("\n") + 1
        y_offset += event_lines * int(height * 0.05) + int(height * 0.03)

    # 4. Draw Talk Title (wrapped to actual pixel width)
    title_text = _wrap_text_to_width(title, title_font, max_text_width, draw)
    draw.text(
        (content_x, y_offset),
        title_text,
        font=title_font,
        fill=(255, 255, 255),  # Pure white
        spacing=max(1, int(height * 0.015)),
    )
    title_lines = title_text.count("\n") + 1
    y_offset += title_lines * int(height * 0.075) + int(height * 0.04)

    # 5. Draw Speaker Names
    if isinstance(speakers, (list, tuple)):
        speaker_str = ", ".join(speakers)
    else:
        speaker_str = str(speakers)

    if speaker_str:
        speaker_wrapped = _wrap_text_to_width(
            f"Speaker: {speaker_str}", speaker_font, max_text_width, draw
        )
        draw.text(
            (content_x, y_offset),
            speaker_wrapped,
            font=speaker_font,
            fill=(203, 213, 225),  # Light slate
        )
        speaker_lines = speaker_wrapped.count("\n") + 1
        y_offset += speaker_lines * int(height * 0.055) + int(height * 0.02)

    # 6. Draw Room & Date
    if room_date:
        room_wrapped = _wrap_text_to_width(room_date, meta_font, max_text_width, draw)
        draw.text(
            (content_x, y_offset),
            room_wrapped,
            font=meta_font,
            fill=(148, 163, 184),  # Muted slate
        )

    return np.array(img)


def _get_audio_samples(
    jingle_path: Path | str | None,
    duration_s: float,
    sample_rate: int = 44100,
) -> np.ndarray:
    """Decodes, resamples, or synthesizes stereo int16 audio samples of shape (2, N)."""
    total_samples = int(duration_s * sample_rate)

    if jingle_path:
        jp = Path(jingle_path)
        if not jp.is_file():
            raise FileNotFoundError(f"Audio jingle file not found: {jp}")

        resampler = av.AudioResampler(
            format="s16p",
            layout="stereo",
            rate=sample_rate,
        )
        resampled_left: list[np.ndarray] = []
        resampled_right: list[np.ndarray] = []

        with av.open(str(jp)) as container:
            if container.streams.audio:
                for packet in container.demux(container.streams.audio[0]):
                    for frame in packet.decode():
                        for resampled_frame in resampler.resample(frame):
                            arr = resampled_frame.to_ndarray()
                            resampled_left.append(arr[0])
                            resampled_right.append(arr[1])

        # Flush resampler
        for resampled_frame in resampler.resample(None):
            arr = resampled_frame.to_ndarray()
            resampled_left.append(arr[0])
            resampled_right.append(arr[1])

        if resampled_left:
            left = np.concatenate(resampled_left)
            right = np.concatenate(resampled_right)

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
                fade_curve = np.linspace(1.0, 0.0, fade_samples, dtype=np.float64)
                left_f = left.astype(np.float64)
                right_f = right.astype(np.float64)
                left_f[-fade_samples:] *= fade_curve
                right_f[-fade_samples:] *= fade_curve
                left = np.clip(left_f, -32768, 32767).astype(np.int16)
                right = np.clip(right_f, -32768, 32767).astype(np.int16)

            return np.vstack([left, right])

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
    threads: int | None = None,
    sample_rate: int = 44100,
) -> None:
    """Encodes slate frame and audio samples into an MP4 container."""
    out_path = Path(output_path)

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total_video_frames = round(duration_seconds * fps)
    total_audio_samples = audio_samples.shape[1]

    with av.open(
        str(out_path), mode="w", format="mp4", options={"movflags": "faststart"}
    ) as out_container:
        # Configure video stream
        video_options: dict[str, str] = {"crf": "20", "preset": "veryfast"}
        if threads is not None:
            video_options["threads"] = str(threads)
        out_v = out_container.add_stream(
            "libx264",
            rate=fps,
            options=video_options,
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
    talk_metadata: dict[str, Any] | str | None = None,
    *,
    title: str = "",
    speakers: list[str] | str = "",
    event_name: str = "",
    room_date: str = "",
    logo_path: Path | str | None = None,
    audio_jingle_path: Path | str | None = None,
    duration_seconds: float = 4.0,
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 24,
    threads: int | None = None,
    audio_sample_rate: int = 44100,
) -> None:
    """Generates an opening title slate video clip with synchronized audio.

    Args:
        output_path: Destination path for the rendered MP4 intro clip.
        talk_metadata: Metadata dictionary containing talk attributes (title, speakers,
            event_name, room_date, resolution/fps/duration), or string title.
        title: Title of the talk (used if not in talk_metadata).
        speakers: Speaker name(s) (used if not in talk_metadata).
        event_name: Conference or event name.
        room_date: Track, room, or date information.
        logo_path: Path to event/sponsor logo image.
        audio_jingle_path: Optional path to audio jingle file.
        duration_seconds: Target clip duration in seconds.
        resolution: Target video resolution tuple (width, height).
        fps: Target video frame rate.
    """
    if isinstance(talk_metadata, str):
        title = talk_metadata
        talk_metadata = None

    if isinstance(talk_metadata, dict):
        title = talk_metadata.get("title", title)
        speakers = talk_metadata.get("speakers", speakers)
        event_name = talk_metadata.get("event_name", event_name)
        room_date = talk_metadata.get("room_date", room_date)

        # Derive duration from talk metadata
        if "duration_seconds" in talk_metadata:
            duration_seconds = float(talk_metadata["duration_seconds"])
        elif "duration" in talk_metadata:
            duration_seconds = float(talk_metadata["duration"])

        # Derive target resolution from talk/video metadata
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

        # Derive framerate from talk/video metadata
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
