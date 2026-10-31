"""Validate whether a recording plausibly belongs to a scheduled talk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    is_room_recording: bool = False,
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
    if not is_room_recording and duration_delta > tolerance_seconds:
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


def container_creation_time(container) -> datetime | None:
    """Extract creation_time from container or video stream metadata."""
    c_time = container.metadata.get("creation_time")
    if not c_time and container.streams.video:
        c_time = container.streams.video[0].metadata.get("creation_time")
    if c_time:
        try:
            dt = datetime.fromisoformat(c_time)
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
        except ValueError, TypeError:
            pass
    return None


@dataclass(frozen=True)
class TalkMatch:
    talk_id: int
    overlap_fraction: float
    cut_start_offset: float
    cut_end_offset: float
    is_full: bool


def match_talks_to_recording(
    talks,
    rec_start: datetime,
    rec_duration_seconds: float,
    min_overlap_fraction: float = 0.5,
) -> list[TalkMatch]:
    """Match scheduled talks to a recording window based on temporal overlap."""
    if rec_duration_seconds <= 0:
        return []
    start_utc = rec_start.replace(tzinfo=UTC) if rec_start.tzinfo is None else rec_start
    end_utc = start_utc + timedelta(seconds=rec_duration_seconds)

    matches: list[TalkMatch] = []
    for talk in talks:
        if talk.start is None or talk.end is None:
            continue
        t_start = (
            talk.start.replace(tzinfo=UTC) if talk.start.tzinfo is None else talk.start
        )
        t_end = talk.end.replace(tzinfo=UTC) if talk.end.tzinfo is None else talk.end
        talk_dur = (t_end - t_start).total_seconds()
        if talk_dur <= 0:
            continue

        overlap_start = max(t_start, start_utc)
        overlap_end = min(t_end, end_utc)
        overlap_secs = (overlap_end - overlap_start).total_seconds()
        if overlap_secs <= 0:
            continue

        fraction = min(1.0, max(0.0, overlap_secs / talk_dur))
        if fraction < min_overlap_fraction:
            continue

        cut_start = max(0.0, (t_start - start_utc).total_seconds())
        cut_end = min(rec_duration_seconds, (t_end - start_utc).total_seconds())
        if cut_end <= cut_start:
            continue

        matches.append(
            TalkMatch(
                talk_id=talk.id,
                overlap_fraction=round(fraction, 3),
                cut_start_offset=round(cut_start, 2),
                cut_end_offset=round(cut_end, 2),
                is_full=fraction >= 0.95,
            )
        )
    return matches
