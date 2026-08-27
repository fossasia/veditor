"""Final publish-quality video and audio transcoding module for VEditor pipeline.

Re-encodes media to production delivery standards using PyAV with pinned
codec, CRF, and bitrate presets. Provides an optional throttled progress
callback hook for worker job tracking.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscodePreset:
    """Configuration structure defining target encode parameters."""

    name: str
    video_codec: str = "libx264"
    crf: int | None = 22
    video_bitrate: int | None = None
    preset_speed: str = "medium"
    audio_codec: str = "aac"
    audio_bitrate: int = 192_000
    container_format: str = "mp4"
    max_width: int | None = None
    max_height: int | None = None


PRESET_1080P_DEFAULT = TranscodePreset(
    name="1080p_default",
    video_codec="libx264",
    crf=22,
    preset_speed="medium",
    audio_codec="aac",
    audio_bitrate=192_000,
    container_format="mp4",
)

PRESET_720P = TranscodePreset(
    name="720p",
    video_codec="libx264",
    crf=23,
    preset_speed="medium",
    audio_codec="aac",
    audio_bitrate=128_000,
    container_format="mp4",
    max_width=1280,
    max_height=720,
)

PRESET_4K_MASTER = TranscodePreset(
    name="4k_master",
    video_codec="libx264",
    crf=20,
    preset_speed="slow",
    audio_codec="aac",
    audio_bitrate=256_000,
    container_format="mp4",
)


def transcode(
    input_path: Path | str,
    output_path: Path | str,
    preset: TranscodePreset | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Encode media to final publish quality using the given preset.

    Args:
        input_path: Path to the source recording.
        output_path: Destination path for the final transcoded media.
        preset: Target transcode preset (defaults to PRESET_1080P_DEFAULT).
        on_progress: Optional callback invoked periodically with completion ratio (0.0 to 1.0).

    Raises:
        FileNotFoundError: If input_path does not exist.
        ValueError: If input contains no audio or video streams.
    """
    in_path = Path(input_path)
    out_path = Path(output_path)

    if not in_path.is_file():
        raise FileNotFoundError(f"Input file not found: {in_path}")

    active_preset = preset or PRESET_1080P_DEFAULT

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(in_path)) as in_container:
        video_streams = list(in_container.streams.video)
        audio_streams = list(in_container.streams.audio)

        if not video_streams and not audio_streams:
            raise ValueError(f"No audio or video streams found in {input_path}")

        in_duration_s = (
            float(in_container.duration / av.time_base)
            if in_container.duration
            else 0.0
        )

        with av.open(
            str(out_path), mode="w", format=active_preset.container_format
        ) as out_container:
            out_video = None
            out_audio = None
            video_time_base = Fraction(1, 24)
            sample_rate = 44100

            # 1. Configure Video Stream if present
            if video_streams:
                in_v = video_streams[0]
                fps = in_v.average_rate or in_v.guessed_rate or 24
                video_options: dict[str, str] = {}

                if active_preset.crf is not None:
                    video_options["crf"] = str(active_preset.crf)
                if active_preset.preset_speed:
                    video_options["preset"] = active_preset.preset_speed

                out_video = out_container.add_stream(
                    active_preset.video_codec,
                    rate=fps,
                    options=video_options,
                )

                width = in_v.codec_context.width
                height = in_v.codec_context.height

                # Apply max dimensions if configured
                if (
                    active_preset.max_width
                    and width
                    and width > active_preset.max_width
                ):
                    scale = active_preset.max_width / width
                    width = active_preset.max_width
                    height = int(height * scale) if height else height

                if (
                    active_preset.max_height
                    and height
                    and height > active_preset.max_height
                ):
                    scale = active_preset.max_height / height
                    height = active_preset.max_height
                    width = int(width * scale) if width else width

                # Ensure dimensions are even numbers for H.264 / yuv420p
                out_video.width = (width // 2) * 2 if width else 640
                out_video.height = (height // 2) * 2 if height else 480
                out_video.pix_fmt = in_v.codec_context.pix_fmt or "yuv420p"

                if active_preset.video_bitrate:
                    out_video.bit_rate = active_preset.video_bitrate

                video_time_base = (
                    Fraction(1, 1) / Fraction(fps) if fps else Fraction(1, 24)
                )

            # 2. Configure Audio Stream if present
            if audio_streams:
                in_a = audio_streams[0]
                sample_rate = in_a.codec_context.sample_rate or 44100
                channels = in_a.codec_context.channels or 1

                out_audio = out_container.add_stream(
                    active_preset.audio_codec,
                    rate=sample_rate,
                )
                out_audio.bit_rate = active_preset.audio_bitrate

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
            last_reported_pct = 0.0

            for packet in in_container.demux(*streams_to_demux):
                # Progress calculation throttled to >= 2% delta
                if on_progress and in_duration_s > 0:
                    ts = packet.pts if packet.pts is not None else packet.dts
                    if ts is not None:
                        tb = (
                            float(packet.stream.time_base)
                            if packet.stream.time_base
                            else (1.0 / av.time_base)
                        )
                        curr_s = float(ts * tb)
                        pct = min(0.99, max(0.0, curr_s / in_duration_s))
                        if pct - last_reported_pct >= 0.02:
                            on_progress(round(pct, 4))
                            last_reported_pct = pct

                if packet.stream.type == "video" and out_video is not None:
                    for frame in packet.decode():
                        frame.pts = video_frame_count
                        frame.time_base = video_time_base
                        video_frame_count += 1
                        for enc_packet in out_video.encode(frame):
                            out_container.mux(enc_packet)

                elif packet.stream.type == "audio" and out_audio is not None:
                    for frame in packet.decode():
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

            # Final 100% progress notification upon successful completion
            if on_progress:
                on_progress(1.0)
