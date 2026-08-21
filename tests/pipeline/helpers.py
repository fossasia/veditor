from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import av


@dataclass(frozen=True)
class ClipInfo:
    duration: float | None
    has_video: bool
    has_audio: bool
    codec_names: tuple[str, ...]
    resolution: tuple[int, int] | None


def open_and_inspect(path: Path | str) -> ClipInfo:
    with av.open(path) as container:
        video_streams = list(container.streams.video)
        audio_streams = list(container.streams.audio)
        codec_names = tuple(stream.codec_context.name for stream in container.streams)
        resolution = None

        if video_streams:
            video = video_streams[0]
            resolution = (video.codec_context.width, video.codec_context.height)

        return ClipInfo(
            duration=_container_duration_seconds(container),
            has_video=bool(video_streams),
            has_audio=bool(audio_streams),
            codec_names=codec_names,
            resolution=resolution,
        )


def assert_duration_close(
    path_a: Path | str,
    path_b: Path | str,
    tolerance_seconds=0.25,
) -> None:
    duration_a = open_and_inspect(path_a).duration
    duration_b = open_and_inspect(path_b).duration

    assert duration_a is not None, f"{path_a} has no readable duration"
    assert duration_b is not None, f"{path_b} has no readable duration"
    assert abs(duration_a - duration_b) <= tolerance_seconds


def assert_playable(path: Path | str) -> None:
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


def _container_duration_seconds(container) -> float | None:
    if container.duration is not None:
        return container.duration / av.time_base

    durations = []
    for stream in container.streams:
        if stream.duration is not None and stream.time_base is not None:
            durations.append(float(stream.duration * stream.time_base))

    return max(durations) if durations else None
