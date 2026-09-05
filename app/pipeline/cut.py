"""Trims a media file to scheduled start and end bounds.

Attempts fast, lossless stream-copy remuxing by default. Falls back to full
decode and re-encode if stream copy fails or is incompatible with the output container.
"""

from __future__ import annotations

import logging
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

import av

logger = logging.getLogger(__name__)


class CutStrategy(str, Enum):
    STREAM_COPY = "stream_copy"
    RE_ENCODE = "re_encode"


DECODER_TO_ENCODER_VIDEO: dict[str, str] = {
    "h264": "libx264",
    "hevc": "libx265",
    "h265": "libx265",
    "vp8": "libvpx",
    "vp9": "libvpx-vp9",
    "av1": "libsvtav1",
    "mpeg4": "mpeg4",
}

DECODER_TO_ENCODER_AUDIO: dict[str, str] = {
    "aac": "aac",
    "mp3": "libmp3lame",
    "opus": "libopus",
    "vorbis": "libvorbis",
    "flac": "flac",
    "pcm_s16le": "pcm_s16le",
}


def _resolve_video_encoder(decoder_name: str | None) -> str:
    """Map a decoder codec name (e.g., 'h264') to an encoder (e.g., 'libx264')."""
    if not decoder_name:
        return "libx264"
    return DECODER_TO_ENCODER_VIDEO.get(decoder_name.lower(), "libx264")


def _resolve_audio_encoder(decoder_name: str | None) -> str:
    """Map an audio decoder codec name to an encoder."""
    if not decoder_name:
        return "aac"
    return DECODER_TO_ENCODER_AUDIO.get(decoder_name.lower(), "aac")


def cut(
    input_path: Path | str,
    output_path: Path | str,
    start_seconds: float,
    end_seconds: float,
    *,
    force_reencode: bool = False,
) -> CutStrategy:
    """Trim an input recording to the [start_seconds, end_seconds] window.

    Args:
        input_path: Path to the raw source recording.
        output_path: Destination path for the trimmed output.
        start_seconds: Start timestamp in seconds (non-negative).
        end_seconds: End timestamp in seconds (greater than start_seconds).
        force_reencode: If True, bypass stream-copy and perform full re-encode.

    Returns:
        CutStrategy: Either CutStrategy.STREAM_COPY or CutStrategy.RE_ENCODE.

    Raises:
        ValueError: If timestamps or input paths are invalid.
        FileNotFoundError: If input_path does not exist.
    """
    in_path = Path(input_path)
    out_path = Path(output_path)

    if not in_path.is_file():
        raise FileNotFoundError(f"Input file not found: {in_path}")

    if in_path.resolve() == out_path.resolve():
        raise ValueError(
            f"Input and output paths must be different to prevent file truncation: {in_path}"
        )

    if start_seconds < 0:
        raise ValueError(f"start_seconds must be non-negative: {start_seconds}")

    if end_seconds <= start_seconds:
        raise ValueError(
            f"end_seconds ({end_seconds}) must be greater than start_seconds ({start_seconds})"
        )

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not force_reencode:
        try:
            return _cut_stream_copy(
                str(in_path), str(out_path), start_seconds, end_seconds
            )
        except (av.FFmpegError, ValueError, RuntimeError, OSError) as exc:
            logger.warning(
                "Stream-copy cut failed (%s); falling back to full re-encode.",
                exc,
            )

    return _cut_reencode(str(in_path), str(out_path), start_seconds, end_seconds)


def _cut_stream_copy(
    input_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
) -> CutStrategy:
    """Fast stream-copy remuxing without decoding."""
    with av.open(input_path) as in_container:
        streams = [*in_container.streams.video, *in_container.streams.audio]
        if not streams:
            raise ValueError(f"No audio or video streams found in {input_path}")

        # Seek container to the nearest keyframe at or before start_seconds
        seek_target = int(start_seconds * av.time_base)
        in_container.seek(seek_target, backward=True, any_frame=False)

        # Ensure the keyframe we sought to is close to start_seconds (< 0.5s).
        # If the keyframe is far before start_seconds, stream copy cannot produce
        # an accurate cut window without retaining lead-in footage.
        if in_container.streams.video and start_seconds > 0.2:
            first_v_pts = None
            for packet in in_container.demux(in_container.streams.video[0]):
                if packet.is_keyframe or packet.pts is not None:
                    time_base = (
                        float(packet.stream.time_base)
                        if packet.stream.time_base is not None
                        else (1.0 / av.time_base)
                    )
                    first_v_pts = float(
                        (packet.pts if packet.pts is not None else packet.dts or 0)
                        * time_base
                    )
                    break
            if first_v_pts is not None and (start_seconds - first_v_pts > 0.5):
                raise ValueError(
                    f"Keyframe at {first_v_pts:.2f}s is too far from requested start {start_seconds:.2f}s; falling back to re-encode."
                )
            in_container.seek(seek_target, backward=True, any_frame=False)

        with av.open(output_path, mode="w") as out_container:
            out_streams: dict[int, av.stream.Stream] = {}
            offset_map: dict[int, int] = {}
            streams_past_end: set[int] = set()

            for stream in streams:
                out_stream = out_container.add_stream_from_template(stream)
                out_streams[stream.index] = out_stream

            for packet in in_container.demux(*streams):
                if packet.dts is None:
                    continue

                stream = packet.stream
                time_base = (
                    float(stream.time_base)
                    if stream.time_base is not None
                    else (1.0 / av.time_base)
                )
                packet_time_s = (
                    float(packet.pts * time_base)
                    if packet.pts is not None
                    else float(packet.dts * time_base)
                )

                if packet_time_s > end_seconds:
                    streams_past_end.add(stream.index)
                    if len(streams_past_end) >= len(streams):
                        break
                    continue

                if not in_container.streams.video and packet_time_s < start_seconds:
                    continue

                if stream.index not in offset_map:
                    offset_map[stream.index] = packet.dts

                base_offset = offset_map[stream.index]
                packet.stream = out_streams[stream.index]
                if packet.pts is not None:
                    packet.pts -= base_offset
                packet.dts -= base_offset

                out_container.mux(packet)

    return CutStrategy.STREAM_COPY


def _add_video_stream(
    container: av.container.OutputContainer,
    preferred_encoder: str,
    rate: Any,
) -> av.stream.Stream:
    """Add a video stream with preferred encoder, falling back to libx264 if incompatible."""
    try:
        return container.add_stream(preferred_encoder, rate=rate)
    except (ValueError, av.FFmpegError) as exc:
        logger.warning(
            "Video encoder '%s' not supported by container (%s); falling back to 'libx264'.",
            preferred_encoder,
            exc,
        )
        return container.add_stream("libx264", rate=rate)


def _add_audio_stream(
    container: av.container.OutputContainer,
    preferred_encoder: str,
    rate: int,
) -> av.stream.Stream:
    """Add an audio stream with preferred encoder, falling back to aac if incompatible."""
    try:
        return container.add_stream(preferred_encoder, rate=rate)
    except (ValueError, av.FFmpegError) as exc:
        logger.warning(
            "Audio encoder '%s' not supported by container (%s); falling back to 'aac'.",
            preferred_encoder,
            exc,
        )
        return container.add_stream("aac", rate=rate)


def _cut_reencode(
    input_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
) -> CutStrategy:
    """Full frame decode and re-encode fallback."""
    with av.open(input_path) as in_container:
        video_streams = list(in_container.streams.video)
        audio_streams = list(in_container.streams.audio)

        if not video_streams and not audio_streams:
            raise ValueError(f"No audio or video streams found in {input_path}")

        # Seek to start
        seek_target = int(start_seconds * av.time_base)
        in_container.seek(seek_target, backward=True, any_frame=False)

        with av.open(output_path, mode="w") as out_container:
            out_video = None
            out_audio = None
            video_time_base = Fraction(1, 24)
            sample_rate = 44100

            if video_streams:
                in_v = video_streams[0]
                encoder_name = _resolve_video_encoder(in_v.codec_context.name)
                fps = in_v.average_rate or in_v.guessed_rate or 24
                out_video = _add_video_stream(out_container, encoder_name, rate=fps)
                out_video.width = in_v.codec_context.width
                out_video.height = in_v.codec_context.height
                out_video.pix_fmt = in_v.codec_context.pix_fmt or "yuv420p"
                video_time_base = (
                    Fraction(1, 1) / Fraction(fps) if fps else Fraction(1, 24)
                )

            if audio_streams:
                in_a = audio_streams[0]
                encoder_name = _resolve_audio_encoder(in_a.codec_context.name)
                sample_rate = in_a.codec_context.sample_rate or 44100
                channels = in_a.codec_context.channels or 1
                out_audio = _add_audio_stream(
                    out_container, encoder_name, rate=sample_rate
                )
                if in_a.codec_context.layout:
                    out_audio.layout = in_a.codec_context.layout.name
                elif channels == 2:
                    out_audio.layout = "stereo"
                else:
                    out_audio.layout = "mono"

            streams_to_demux = [
                s for s in (video_streams[:1] + audio_streams[:1]) if s is not None
            ]

            video_frame_count = 0
            audio_sample_count = 0
            streams_past_end: set[int] = set()

            for packet in in_container.demux(*streams_to_demux):
                if len(streams_past_end) >= len(streams_to_demux):
                    break

                for frame in packet.decode():
                    time_base = (
                        float(frame.time_base) if frame.time_base is not None else 1.0
                    )
                    frame_time_s = (
                        float(frame.pts * time_base)
                        if frame.pts is not None
                        else (frame.time if frame.time is not None else 0.0)
                    )

                    if frame_time_s > end_seconds:
                        streams_past_end.add(packet.stream.index)
                        if len(streams_past_end) >= len(streams_to_demux):
                            break
                        continue

                    if frame_time_s < start_seconds:
                        continue

                    if isinstance(frame, av.VideoFrame) and out_video is not None:
                        if frame.format.name != out_video.pix_fmt:
                            try:
                                frame = frame.reformat(format=out_video.pix_fmt)
                            except (av.FFmpegError, ValueError) as exc:
                                logger.warning(
                                    "Frame reformat failed (%s), proceeding without reformat.",
                                    exc,
                                )
                        frame.pts = video_frame_count
                        frame.time_base = video_time_base
                        video_frame_count += 1
                        for enc_packet in out_video.encode(frame):
                            out_container.mux(enc_packet)

                    elif isinstance(frame, av.AudioFrame) and out_audio is not None:
                        frame.pts = audio_sample_count
                        frame.time_base = Fraction(1, sample_rate)
                        audio_sample_count += frame.samples
                        for enc_packet in out_audio.encode(frame):
                            out_container.mux(enc_packet)

            # Flush encoders
            if out_video is not None:
                for enc_packet in out_video.encode():
                    out_container.mux(enc_packet)
            if out_audio is not None:
                for enc_packet in out_audio.encode():
                    out_container.mux(enc_packet)

    return CutStrategy.RE_ENCODE
