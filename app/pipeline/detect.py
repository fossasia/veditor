"""Validate whether a recording plausibly belongs to a scheduled talk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import av
from av.error import FFmpegError

DETECT_DURATION_TOLERANCE_SECONDS = 300.0


@dataclass(frozen=True)
class DetectResult:
    passed: bool
    actual_duration_seconds: float
    has_video: bool
    has_audio: bool
    reason: str | None


def detect(
    file_path: Path,
    scheduled_start: datetime,
    scheduled_end: datetime,
    tolerance_seconds: float = DETECT_DURATION_TOLERANCE_SECONDS,
) -> DetectResult:
    """Inspect a media file and return a pass/fail validation result.

    Missing paths are treated as clean detection failures rather than caller
    exceptions so queue tasks can surface a useful reason without knowing PyAV
    or filesystem exception types.
    """

    if not file_path.exists():
        return _failed("file not found")

    if file_path.stat().st_size == 0:
        return _failed("file is empty (0 bytes)")

    try:
        with av.open(str(file_path)) as container:
            duration = container_duration_seconds(container)
            actual_duration = duration if duration is not None else 0.0
            has_video = bool(container.streams.video)
            has_audio = bool(container.streams.audio)
    except FFmpegError, OSError, ValueError:
        return _failed("unreadable container")

    if actual_duration <= 0:
        return DetectResult(
            passed=False,
            actual_duration_seconds=actual_duration,
            has_video=has_video,
            has_audio=has_audio,
            reason="no duration metadata found",
        )

    if not has_video:
        return DetectResult(
            passed=False,
            actual_duration_seconds=actual_duration,
            has_video=has_video,
            has_audio=has_audio,
            reason="missing video stream",
        )

    scheduled_duration = (scheduled_end - scheduled_start).total_seconds()
    duration_delta = abs(actual_duration - scheduled_duration)
    if duration_delta > tolerance_seconds:
        return DetectResult(
            passed=False,
            actual_duration_seconds=actual_duration,
            has_video=has_video,
            has_audio=has_audio,
            reason=(
                "duration outside scheduled window "
                f"(expected {scheduled_duration:.3f}s, got {actual_duration:.3f}s)"
            ),
        )

    return DetectResult(
        passed=True,
        actual_duration_seconds=actual_duration,
        has_video=has_video,
        has_audio=has_audio,
        reason=None,
    )


def _failed(reason: str) -> DetectResult:
    return DetectResult(
        passed=False,
        actual_duration_seconds=0.0,
        has_video=False,
        has_audio=False,
        reason=reason,
    )


def container_duration_seconds(container) -> float | None:
    if container.duration is not None and container.duration > 0:
        return container.duration / av.time_base

    stream_durations = [
        float(stream.duration * stream.time_base)
        for stream in container.streams
        if stream.duration is not None
        and stream.duration > 0
        and stream.time_base is not None
    ]
    return max(stream_durations) if stream_durations else None
