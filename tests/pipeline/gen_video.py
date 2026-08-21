"""Synthetic media fixtures for pipeline testing.

Frames are synthesized manually here rather than relying on PyAV's built-in
`lavfi` inputs. This maintains a unified Python helper capable of handling
muxed audio/video clips, edge cases (audio-only or video-only), precise
waveforms, and NumPy-readable pixel patterns.

For any future codec that is too complex to synthesize using PyAV, commit the
generation script instead of the resulting binary media file.
"""

from __future__ import annotations

import math
import tempfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import av
import numpy as np

DEFAULT_CONTAINER_SUFFIX = ".mp4"
VIDEO_CODEC_ALIASES = {
    "h264": "libx264",
}
AUDIO_CODEC = "aac"
_FALLBACK_TEMP_DIR = tempfile.TemporaryDirectory(prefix="veditor-media-")


def generate_clip(
    duration_s: float,
    *,
    has_video: bool = True,
    has_audio: bool = True,
    resolution: tuple[int, int] = (320, 240),
    codec: str = "h264",
    pattern: str = "solid",
    fps: int = 24,
    sample_rate: int = 44100,
    audio_waveform: str = "tone",
    output_dir: Path | str | None = None,
) -> Path:
    """Generate a short test clip.

    Tests should pass pytest's ``tmp_path`` as ``output_dir`` so files are
    removed after the test. The fallback directory is cleaned at process exit.
    """

    if not has_video and not has_audio:
        raise ValueError("generate_clip requires at least one stream")
    if duration_s <= 0:
        raise ValueError("duration_s must be greater than zero")

    target = _target_path(output_dir)
    with av.open(target, mode="w") as container:
        video_stream = None
        audio_stream = None

        if has_video:
            width, height = resolution
            video_stream = container.add_stream(_video_codec(codec), rate=fps)
            video_stream.width = width
            video_stream.height = height
            video_stream.pix_fmt = "yuv420p"

        if has_audio:
            audio_stream = container.add_stream(AUDIO_CODEC, rate=sample_rate)
            audio_stream.layout = "mono"

        if video_stream is not None:
            frame_count = max(1, math.ceil(duration_s * fps))
            for frame_index in range(frame_count):
                frame = av.VideoFrame.from_ndarray(
                    _video_array(frame_index, frame_count, resolution, pattern),
                    format="rgb24",
                )
                for packet in video_stream.encode(frame):
                    container.mux(packet)
            for packet in video_stream.encode():
                container.mux(packet)

        if audio_stream is not None:
            for frame in _audio_frames(duration_s, sample_rate, audio_waveform):
                for packet in audio_stream.encode(frame):
                    container.mux(packet)
            for packet in audio_stream.encode():
                container.mux(packet)

    return target


def generate_corrupt_clip(output_dir: Path | str | None = None) -> Path:
    target = _target_path(output_dir)
    target.write_bytes(
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom\x00\x00\x00\x08mdatbroken"
    )
    return target


def generate_mismatched_duration_clip(
    scheduled_start: datetime,
    scheduled_end: datetime,
    offset_s: float,
    *,
    output_dir: Path | str | None = None,
) -> Path:
    """Create a clip whose duration differs from the scheduled window.

    ``offset_s`` is added to the scheduled duration, with a small floor so tests
    can exercise too-short negative offsets without creating an empty container.
    """

    scheduled_duration = (scheduled_end - scheduled_start).total_seconds()
    return generate_clip(
        max(0.05, scheduled_duration + offset_s),
        output_dir=output_dir,
    )


def _target_path(output_dir: Path | str | None) -> Path:
    if output_dir is None:
        output_dir = _FALLBACK_TEMP_DIR.name
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{uuid4().hex}{DEFAULT_CONTAINER_SUFFIX}"


def _video_codec(codec: str) -> str:
    return VIDEO_CODEC_ALIASES.get(codec, codec)


def _video_array(
    frame_index: int,
    frame_count: int,
    resolution: tuple[int, int],
    pattern: str,
) -> np.ndarray:
    width, height = resolution
    if pattern == "solid":
        value = int(255 * frame_index / max(frame_count - 1, 1))
        return np.full((height, width, 3), (value, 80, 180), dtype=np.uint8)

    if pattern == "gradient":
        x = np.linspace(0, 255, width, dtype=np.uint8)
        y = np.linspace(0, 255, height, dtype=np.uint8)[:, None]
        return np.stack(
            [
                np.broadcast_to(x, (height, width)),
                np.broadcast_to(y, (height, width)),
                np.full((height, width), frame_index % 256, dtype=np.uint8),
            ],
            axis=2,
        )

    if pattern == "noise":
        rng = np.random.default_rng(seed=frame_index)
        return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)

    raise ValueError(f"Unsupported video pattern: {pattern}")


def _audio_frames(
    duration_s: float, sample_rate: int, waveform: str
) -> Iterator[av.AudioFrame]:
    total_samples = max(1, math.ceil(duration_s * sample_rate))
    chunk_size = 1024

    for start in range(0, total_samples, chunk_size):
        stop = min(start + chunk_size, total_samples)
        if waveform == "tone":
            t = np.arange(start, stop, dtype=np.float64) / sample_rate
            samples = 0.25 * np.sin(2 * np.pi * 440 * t)
        elif waveform == "silence":
            samples = np.zeros(stop - start, dtype=np.float64)
        else:
            raise ValueError(f"Unsupported audio waveform: {waveform}")

        data = (samples * np.iinfo(np.int16).max).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(data, format="s16", layout="mono")
        frame.sample_rate = sample_rate
        yield frame
