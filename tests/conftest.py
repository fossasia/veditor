"""Shared test fixtures, fake storage, and synthetic media generator for VEditor tests."""

from __future__ import annotations

import logging
import math
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import av
import numpy as np
import pytest

from app.pipeline.detect import container_duration_seconds
from app.storage import StorageBackend, StorageKeyNotFoundError

logger = logging.getLogger(__name__)

# --- Fake Storage ---


class FakePath:
    """A minimal mock for pathlib.Path that provides read_bytes for tests."""

    def __init__(self, key: str, data: bytes):
        self.key = key
        self._data = data

    def __fspath__(self) -> str:
        return f"/fake/{self.key}"

    def read_bytes(self) -> bytes:
        return self._data

    def is_file(self) -> bool:
        return True

    def is_dir(self) -> bool:
        return False

    def as_uri(self) -> str:
        return f"memory://{self.key}"

    def exists(self) -> bool:
        return True


class FakeStorageBackend(StorageBackend):
    """
    In-memory storage backend for fast, deterministic testing.

    LIMITATION: get() returns a FakePath object that supports read_bytes() but
    cannot be opened by C libraries (like PyAV). If a pipeline test requires
    actual media decode/encode, use a LocalDiskBackend backed by a tmp_path
    instead. This fake is intended for logic around the pipeline (ingest,
    state transitions, retention).
    """

    DEFAULT_FREE_BYTES = 1024 * 1024 * 1024 * 100  # 100GB default

    def __init__(self):
        self.storage: dict[str, bytes] = {}
        self._free_bytes: int = self.DEFAULT_FREE_BYTES

    def set_free_bytes(self, size: int) -> None:
        """Helper to simulate low disk space conditions in tests."""
        self._free_bytes = size

    def put(self, key: str, source: Path | bytes) -> None:
        if isinstance(source, bytes):
            self.storage[key] = source
        elif hasattr(source, "read_bytes"):
            self.storage[key] = source.read_bytes()
        else:
            self.storage[key] = Path(source).read_bytes()

    def get(self, key: str) -> Path:
        if key not in self.storage:
            raise StorageKeyNotFoundError(key)
        return FakePath(key, self.storage[key])  # type: ignore

    def url(self, key: str) -> str:
        return f"memory://{key}"

    def delete(self, key: str) -> None:
        # Idempotent delete of exact match
        self.storage.pop(key, None)
        # Delete prefixes (e.g. 'talk_1/raw')
        prefix = key if key.endswith("/") else f"{key}/"
        keys_to_delete = [k for k in self.storage if k.startswith(prefix)]
        for k in keys_to_delete:
            del self.storage[k]

    def exists(self, key: str) -> bool:
        return key in self.storage

    def list_keys(self, prefix: str = "") -> list[str]:
        return [k for k in sorted(self.storage.keys()) if k.startswith(prefix)]

    def free_bytes(self) -> int:
        return self._free_bytes

    def total_bytes(self) -> int:
        return self.DEFAULT_FREE_BYTES

    def get_temp_dir(self) -> Path:
        return Path(tempfile.gettempdir())


@pytest.fixture
def fake_storage() -> FakeStorageBackend:
    return FakeStorageBackend()


def override_storage_backend(app, fake_backend: FakeStorageBackend):
    """
    Helper to override the FastAPI dependency for route-level tests.
    Usage:
        override_storage_backend(app, fake_storage)
    """
    try:
        from app.storage import get_storage_backend

        app.dependency_overrides[get_storage_backend] = lambda: fake_backend
    except ImportError as e:
        logger.warning(
            "Could not override get_storage_backend. It may not be implemented yet. Error: %s",
            e,
        )


# --- Synthetic Media Generator & Inspection Fixtures ---

DEFAULT_CONTAINER_SUFFIX = ".mp4"
VIDEO_CODEC_ALIASES = {
    "h264": "libx264",
}
AUDIO_CODEC = "aac"
_FALLBACK_TEMP_DIR = tempfile.TemporaryDirectory(prefix="veditor-media-")


@dataclass(frozen=True)
class ClipInfo:
    duration: float | None
    has_video: bool
    has_audio: bool
    codec_names: tuple[str, ...]
    resolution: tuple[int, int] | None


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
    """Generate a short synthetic test clip with video and/or audio streams."""
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
    """Generate an unreadable/corrupt media file for testing error paths."""
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
    """Create a clip whose duration differs from the scheduled window."""
    scheduled_duration = (scheduled_end - scheduled_start).total_seconds()
    return generate_clip(
        max(0.05, scheduled_duration + offset_s),
        output_dir=output_dir,
    )


def open_and_inspect(path: Path | str) -> ClipInfo:
    """Inspect container streams and return ClipInfo metadata."""
    with av.open(path) as container:
        video_streams = list(container.streams.video)
        audio_streams = list(container.streams.audio)
        codec_names = tuple(stream.codec_context.name for stream in container.streams)
        resolution = None

        if video_streams:
            video = video_streams[0]
            resolution = (video.codec_context.width, video.codec_context.height)

        return ClipInfo(
            duration=container_duration_seconds(container),
            has_video=bool(video_streams),
            has_audio=bool(audio_streams),
            codec_names=codec_names,
            resolution=resolution,
        )


def assert_duration_close(
    path_a: Path | str,
    path_b: Path | str,
    tolerance_seconds: float = 0.25,
) -> None:
    """Assert that two media files have duration within the specified tolerance."""
    duration_a = open_and_inspect(path_a).duration
    duration_b = open_and_inspect(path_b).duration

    assert duration_a is not None, f"{path_a} has no readable duration"
    assert duration_b is not None, f"{path_b} has no readable duration"
    assert abs(duration_a - duration_b) <= tolerance_seconds


def assert_playable(path: Path | str) -> None:
    """Assert that container streams can be decoded successfully."""
    with av.open(path) as container:
        streams = [*container.streams.video, *container.streams.audio]
        assert streams, f"{path} has no media streams"

        decoded_indices = set()
        for packet in container.demux(*streams):
            for _frame in packet.decode():
                decoded_indices.add(packet.stream.index)
            if len(decoded_indices) == len(streams):
                break

        for stream in streams:
            assert stream.index in decoded_indices, (
                f"{path} has no decodable {stream.type} frame"
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


@pytest.fixture
def make_clip():
    return generate_clip


@pytest.fixture
def make_corrupt_clip():
    return generate_corrupt_clip


@pytest.fixture
def make_mismatched_clip():
    return generate_mismatched_duration_clip


@pytest.fixture
def inspect_clip():
    return open_and_inspect
