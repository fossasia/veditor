"""Final publish-quality video and audio transcoding module for VEditor pipeline.

Re-encodes media to production delivery standards using PyAV with pinned
codec, CRF, and bitrate presets. Provides an optional throttled progress
callback hook for worker job tracking.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import cast

import av
import numpy as np
from av.audio.frame import AudioFrame
from av.audio.stream import AudioStream
from av.packet import Packet
from av.stream import Stream
from av.video.stream import VideoStream

from .concat import _can_stream_copy, _concat_stream_copy
from .loudness import (
    DEFAULT_LRA,
    DEFAULT_TRUE_PEAK,
    _mux_packet_with_monotonic_dts,
    rescale_pts,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscodePreset:
    """Configuration structure defining target encode parameters."""

    name: str
    video_codec: str = 'libx264'
    crf: int | None = 22
    video_bitrate: int | None = None
    preset_speed: str = 'veryfast'
    audio_codec: str = 'aac'
    audio_bitrate: int = 96_000
    container_format: str = 'mp4'
    pix_fmt: str = 'yuv420p'
    max_width: int | None = None
    max_height: int | None = None
    threads: int | None = None


PRESET_480P = TranscodePreset(
    name='480p',
    video_codec='libx264',
    crf=22,
    preset_speed='veryfast',
    audio_codec='aac',
    audio_bitrate=96_000,
    container_format='mp4',
    pix_fmt='yuv420p',
    max_width=854,
    max_height=480,
)

PRESET_720P = TranscodePreset(
    name='720p',
    video_codec='libx264',
    crf=23,
    preset_speed='veryfast',
    audio_codec='aac',
    audio_bitrate=96_000,
    container_format='mp4',
    pix_fmt='yuv420p',
    max_width=1280,
    max_height=720,
)

PRESET_1080P_DEFAULT = TranscodePreset(
    name='1080p_default',
    video_codec='libx264',
    crf=22,
    preset_speed='veryfast',
    audio_codec='aac',
    audio_bitrate=96_000,
    container_format='mp4',
    pix_fmt='yuv420p',
    max_width=1920,
    max_height=1080,
)

PRESET_1440P = TranscodePreset(
    name='1440p',
    video_codec='libx264',
    crf=21,
    preset_speed='veryfast',
    audio_codec='aac',
    audio_bitrate=96_000,
    container_format='mp4',
    pix_fmt='yuv420p',
    max_width=2560,
    max_height=1440,
)

# 4K master preset commented out for now:
# PRESET_4K_MASTER = TranscodePreset(
#     name="4k_master",
#     video_codec="libx264",
#     crf=20,
#     preset_speed="slow",
#     audio_codec="aac",
#     audio_bitrate=256_000,
#     container_format="mp4",
#     pix_fmt="yuv420p",
# )


def transcode(
    input_path: Path | str,
    output_path: Path | str,
    preset: TranscodePreset | None = None,
    on_progress: Callable[[float], None] | None = None,
    threads: int | None = None,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    intro_path: Path | str | None = None,
    outro_path: Path | str | None = None,
    target_lufs: float | None = None,
    allow_stream_copy: bool = True,
) -> None:
    """Encode media to final publish quality using the given preset.

    Supports optional intro and outro slates in a unified single-pass transcode,
    as well as integrated loudness normalization via EBU R128 when target_lufs
    is specified.

    Args:
        input_path: Path to the source recording.
        output_path: Destination path for the final transcoded media.
        preset: Target transcode preset (defaults to PRESET_1080P_DEFAULT).
        on_progress: Optional callback invoked periodically with completion ratio (0.0 to 1.0).
        threads: Optional thread count limit for the video encoder.
        start_seconds: Optional start timestamp in seconds (defaults to 0.0).
        end_seconds: Optional end timestamp in seconds.
        intro_path: Optional path to an intro title slate media file.
        outro_path: Optional path to an outro closing media file.
        target_lufs: Optional target integrated loudness in LUFS (e.g. -16.0).
        allow_stream_copy: When True, enables lossless stream-copy remuxing if input
            satisfies the target preset.

    Raises:
        FileNotFoundError: If input_path, intro_path, or outro_path does not exist.
        ValueError: If input contains no audio or video streams, or parameters are invalid.
    """
    in_path = Path(input_path)
    out_path = Path(output_path)

    if not in_path.is_file():
        raise FileNotFoundError(f'Input file not found: {in_path}')

    if start_seconds < 0:
        raise ValueError(f'start_seconds must be non-negative: {start_seconds}')
    if end_seconds is not None and end_seconds <= start_seconds:
        raise ValueError(
            f'end_seconds ({end_seconds}) must be greater than start_seconds ({start_seconds})'
        )

    if target_lufs is not None and (
        not math.isfinite(target_lufs) or target_lufs > 0 or target_lufs < -70.0
    ):
        raise ValueError(
            f'target_lufs must be between -70.0 and 0.0, got: {target_lufs}'
        )

    active_preset = preset or PRESET_1080P_DEFAULT

    if threads is not None and threads <= 0:
        raise ValueError(f'threads must be greater than zero: {threads}')
    if active_preset.threads is not None and active_preset.threads <= 0:
        raise ValueError(f'threads must be greater than zero: {active_preset.threads}')

    intro_p = Path(intro_path) if intro_path is not None else None
    if intro_p is not None and not intro_p.is_file():
        raise FileNotFoundError(f'Intro file not found: {intro_p}')

    outro_p = Path(outro_path) if outro_path is not None else None
    if outro_p is not None and not outro_p.is_file():
        raise FileNotFoundError(f'Outro file not found: {outro_p}')

    segments: list[tuple[str, Path, float, float | None]] = []
    if intro_p is not None:
        segments.append(('intro', intro_p, 0.0, None))
    segments.append(('main', in_path, start_seconds, end_seconds))
    if outro_p is not None:
        segments.append(('outro', outro_p, 0.0, None))

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(in_path)) as probe_container:
        video_streams = list(probe_container.streams.video)
        audio_streams = list(probe_container.streams.audio)

        if not video_streams and not audio_streams:
            raise ValueError(f'No audio or video streams found in {input_path}')

        if target_lufs is not None and not audio_streams:
            raise ValueError(f'No audio stream found in {input_path}')

        in_v = video_streams[0] if video_streams else None
        in_a = audio_streams[0] if audio_streams else None

        fps_source = (in_v.average_rate or in_v.guessed_rate) if in_v else None
        fps = (
            Fraction(fps_source.numerator, fps_source.denominator)
            if fps_source
            else Fraction(24, 1)
        )
        sample_rate = in_a.codec_context.sample_rate or 44100 if in_a else 44100
        channels = in_a.codec_context.channels or 2 if in_a else 2
        if in_a and in_a.codec_context.layout:
            layout_name = in_a.codec_context.layout.name
        elif channels == 1:
            layout_name = 'mono'
        else:
            layout_name = 'stereo'

        if in_v:
            width = in_v.codec_context.width
            height = in_v.codec_context.height
            if active_preset.max_width and width and width > active_preset.max_width:
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

            out_width = (width // 2) * 2 if width else 640
            out_height = (height // 2) * 2 if height else 480
        else:
            out_width = 640
            out_height = 480

    segment_durations: list[float] = []
    for _seg_type, seg_p, seg_start, seg_end in segments:
        try:
            with av.open(str(seg_p)) as c:
                c_dur = float(c.duration / av.time_base) if c.duration else 0.0
                if seg_end is not None:
                    dur = seg_end - seg_start
                elif c_dur > seg_start:
                    dur = c_dur - seg_start
                else:
                    dur = c_dur
                segment_durations.append(max(0.0, dur))
        except av.FFmpegError, ValueError, OSError:
            segment_durations.append(0.0)

    total_duration_s = sum(segment_durations)

    container_options: dict[str, str] = {}
    if active_preset.container_format == 'mp4':
        container_options['movflags'] = 'faststart'

    can_stream_copy_video = False
    can_stream_copy_audio = False

    if (
        allow_stream_copy
        and start_seconds == 0.0
        and end_seconds is None
        and in_v is not None
    ):
        v_ctx = in_v.codec_context
        v_codec = v_ctx.name.lower() if v_ctx and v_ctx.name else ''
        target_v_codec = active_preset.video_codec.lower()
        codec_matches = (
            (v_codec in ('h264', 'avc1') and target_v_codec in ('libx264', 'h264'))
            or (
                v_codec in ('hevc', 'h265')
                and target_v_codec in ('libx265', 'hevc', 'h265')
            )
            or (v_codec == target_v_codec)
        )
        pix_fmt_val = v_ctx.pix_fmt if v_ctx else None
        pix_matches = pix_fmt_val == active_preset.pix_fmt or (
            pix_fmt_val == 'yuv420p' and active_preset.pix_fmt == 'yuv420p'
        )
        res_matches = (
            active_preset.max_width is None
            or (v_ctx.width or 0) <= active_preset.max_width
        ) and (
            active_preset.max_height is None
            or (v_ctx.height or 0) <= active_preset.max_height
        )
        is_standard_crf = active_preset.video_bitrate is None and (
            active_preset.crf is None or active_preset.crf == PRESET_1080P_DEFAULT.crf
        )
        preset_video_matches = bool(
            codec_matches and pix_matches and res_matches and is_standard_crf
        )

        audio_matches = False
        if in_a is None:
            audio_matches = True
        elif target_lufs is None:
            a_ctx = in_a.codec_context
            a_codec = a_ctx.name.lower() if a_ctx and a_ctx.name else ''
            target_a_codec = active_preset.audio_codec.lower()
            a_codec_ok = (
                a_codec in ('aac', 'mp4a') and target_a_codec in ('aac', 'libfdk_aac')
            ) or (a_codec == target_a_codec)
            a_bitrate_ok = (
                active_preset.audio_bitrate is None
                or a_ctx.bit_rate is None
                or a_ctx.bit_rate <= int(active_preset.audio_bitrate * 1.15)
            )
            audio_matches = bool(a_codec_ok and a_bitrate_ok)

        if len(segments) == 1:
            can_stream_copy_video = preset_video_matches
            can_stream_copy_audio = bool(can_stream_copy_video and audio_matches)
        elif len(segments) > 1 and preset_video_matches and audio_matches:
            seg_paths = [p for _, p, _, _ in segments]
            if _can_stream_copy(seg_paths):
                can_stream_copy_video = True
                can_stream_copy_audio = True

    if len(segments) > 1 and can_stream_copy_video and can_stream_copy_audio:
        try:
            _concat_stream_copy(
                [p for _, p, _, _ in segments], out_path, canonical_cut_path=in_path
            )
            if on_progress:
                on_progress(1.0)
            return
        except (av.FFmpegError, ValueError, OSError) as exc:
            logger.warning(
                'Multi-segment stream copy failed (%s); falling back to full re-encode.',
                exc,
            )
            can_stream_copy_video = False
            can_stream_copy_audio = False

    if len(segments) == 1 and can_stream_copy_video and can_stream_copy_audio:
        try:
            with (
                av.open(str(in_path)) as in_c,
                av.open(
                    str(out_path),
                    mode='w',
                    format=active_preset.container_format,
                    options=container_options if container_options else None,
                ) as out_container,
            ):
                out_v = (
                    out_container.add_stream_from_template(in_c.streams.video[0])
                    if in_c.streams.video
                    else None
                )
                out_a = (
                    out_container.add_stream_from_template(in_c.streams.audio[0])
                    if in_c.streams.audio
                    else None
                )

                stream_map: dict[int, Stream] = {}
                if in_c.streams.video and out_v:
                    stream_map[in_c.streams.video[0].index] = out_v
                if in_c.streams.audio and out_a:
                    stream_map[in_c.streams.audio[0].index] = out_a

                out_container.start_encoding()
                last_dts: dict[int, int] = {}
                stream_initial_dts: dict[int, int] = {}
                stream_first_pts: dict[int, float] = {}
                last_reported_pct = 0.0

                streams_to_demux = [
                    s
                    for s in (in_c.streams.video[:1] + in_c.streams.audio[:1])
                    if s is not None
                ]
                for packet in in_c.demux(*streams_to_demux):
                    if packet.stream.index not in stream_map:
                        continue

                    if on_progress and total_duration_s > 0:
                        ts = packet.pts if packet.pts is not None else packet.dts
                        if ts is not None:
                            stream_idx = packet.stream.index
                            tb = (
                                float(packet.stream.time_base)
                                if packet.stream.time_base
                                else (1.0 / av.time_base)
                            )
                            current_ts_s = float(ts * tb)
                            if stream_idx not in stream_first_pts:
                                stream_first_pts[stream_idx] = current_ts_s
                            seg_elapsed_s = max(
                                0.0, current_ts_s - stream_first_pts[stream_idx]
                            )
                            pct = min(0.99, max(0.0, seg_elapsed_s / total_duration_s))
                            reported_pct = round(pct, 4)
                            if reported_pct > last_reported_pct:
                                on_progress(reported_pct)
                                last_reported_pct = reported_pct

                    out_s = stream_map[packet.stream.index]
                    if packet.dts is None and packet.pts is None:
                        continue
                    rescale_pts(packet, packet.stream.time_base, out_s.time_base)
                    if out_s.index not in stream_initial_dts:
                        first_ts = packet.dts if packet.dts is not None else packet.pts
                        stream_initial_dts[out_s.index] = (
                            first_ts if first_ts is not None else 0
                        )
                    init_dts = stream_initial_dts[out_s.index]
                    if packet.dts is not None:
                        packet.dts -= init_dts
                    if packet.pts is not None:
                        packet.pts -= init_dts
                    packet.stream = out_s
                    _mux_packet_with_monotonic_dts(
                        out_container, packet, out_s.index, last_dts
                    )

            if on_progress:
                on_progress(1.0)
            return
        except (av.FFmpegError, ValueError, OSError) as exc:
            logger.warning(
                'Single-segment stream copy remux failed (%s); falling back to full re-encode.',
                exc,
            )
            can_stream_copy_video = False
            can_stream_copy_audio = False

    with (
        av.open(str(in_path)) as in_ref_c,
        av.open(
            str(out_path),
            mode='w',
            format=active_preset.container_format,
            options=container_options if container_options else None,
        ) as out_container,
    ):
        out_video = None
        out_audio = None
        video_time_base = Fraction(1, 1) / fps

        # 1. Configure Video Stream if present
        if video_streams:
            if can_stream_copy_video and in_ref_c.streams.video:
                out_video = out_container.add_stream_from_template(
                    in_ref_c.streams.video[0]
                )
            else:
                video_options: dict[str, str] = {}
                if active_preset.crf is not None:
                    video_options['crf'] = str(active_preset.crf)
                if active_preset.preset_speed:
                    video_options['preset'] = active_preset.preset_speed
                active_threads = (
                    threads if threads is not None else active_preset.threads
                )
                if active_threads is not None:
                    video_options['threads'] = str(active_threads)

                out_video = cast(
                    VideoStream,
                    out_container.add_stream(
                        active_preset.video_codec,
                        rate=fps,
                        options=video_options,
                    ),
                )
                out_video.width = out_width
                out_video.height = out_height
                out_video.pix_fmt = active_preset.pix_fmt
                if active_preset.video_bitrate:
                    out_video.bit_rate = active_preset.video_bitrate

        # 2. Configure Audio Stream if present
        audio_layout = 'stereo' if target_lufs is not None else layout_name
        if audio_streams:
            out_audio = cast(
                AudioStream,
                out_container.add_stream(
                    active_preset.audio_codec,
                    rate=sample_rate,
                ),
            )
            out_audio.bit_rate = active_preset.audio_bitrate
            out_audio.layout = audio_layout

        audio_graph = None
        if target_lufs is not None and out_audio is not None:
            audio_graph = av.filter.Graph()
            buf_node = audio_graph.add_abuffer(
                sample_rate=sample_rate,
                format='fltp',
                layout='stereo',
                time_base=Fraction(1, sample_rate),
            )
            loudnorm_node = audio_graph.add(
                'loudnorm',
                f'I={target_lufs}:TP={DEFAULT_TRUE_PEAK}:LRA={DEFAULT_LRA}',
            )
            aformat_node = audio_graph.add(
                'aformat',
                f'sample_rates={sample_rate}:channel_layouts=stereo:sample_fmts=fltp',
            )
            sink_node = audio_graph.add('abuffersink')
            buf_node.link_to(loudnorm_node)
            loudnorm_node.link_to(aformat_node)
            aformat_node.link_to(sink_node)
            audio_graph.configure()

        out_container.start_encoding()
        video_frame_count = 0
        audio_sample_count = 0
        last_dts = {}
        stream_initial_dts = {}

        def emit_video_frame(frame: av.VideoFrame) -> None:
            nonlocal video_frame_count
            if out_video is None:
                return
            if (
                frame.width != out_width
                or frame.height != out_height
                or frame.format.name != active_preset.pix_fmt
            ):
                frame = frame.reformat(
                    width=out_width,
                    height=out_height,
                    format=active_preset.pix_fmt,
                )
            frame.pts = video_frame_count
            frame.time_base = video_time_base
            video_frame_count += 1
            for enc_packet in out_video.encode(frame):
                _mux_packet_with_monotonic_dts(
                    out_container, enc_packet, out_video.index, last_dts
                )

        def emit_audio_frame(frame: av.AudioFrame) -> None:
            nonlocal audio_sample_count
            if out_audio is None:
                return
            frame.pts = audio_sample_count
            frame.time_base = Fraction(1, sample_rate)
            audio_sample_count += frame.samples
            for enc_packet in out_audio.encode(frame):
                _mux_packet_with_monotonic_dts(
                    out_container, enc_packet, out_audio.index, last_dts
                )

        def feed_audio_frame(frame: av.AudioFrame) -> None:
            if audio_graph is not None:
                audio_graph.push(frame)
                while True:
                    try:
                        out_f = audio_graph.pull()
                        emit_audio_frame(cast(AudioFrame, out_f))
                    except av.BlockingIOError, av.EOFError:
                        break
            else:
                emit_audio_frame(frame)

        last_reported_pct = 0.0

        for seg_idx, (_seg_type, seg_path, seg_start, seg_end) in enumerate(segments):
            seg_accumulated_duration_s = sum(segment_durations[:seg_idx])
            stream_first_pts = {}
            streams_past_end: set[int] = set()
            seg_had_video = False
            seg_had_audio = False
            seg_v_start = video_frame_count
            seg_a_start = audio_sample_count

            with av.open(str(seg_path)) as seg_c:
                if seg_start > 0.0:
                    seg_c.seek(
                        int(seg_start * av.time_base),
                        backward=True,
                        any_frame=False,
                    )

                seg_video_streams = list(seg_c.streams.video)
                seg_audio_streams = list(seg_c.streams.audio)

                seg_v_stream = seg_video_streams[0] if seg_video_streams else None
                seg_a_stream = seg_audio_streams[0] if seg_audio_streams else None

                fps_graph = None
                if seg_v_stream is not None and out_video is not None:
                    in_rate = seg_v_stream.average_rate or seg_v_stream.guessed_rate
                    if in_rate and abs(float(in_rate) - float(fps)) > 0.05:
                        fps_graph = av.filter.Graph()
                        buffer = fps_graph.add_buffer(template=seg_v_stream)
                        fps_filter = fps_graph.add('fps', f'fps={float(fps)}')
                        sink = fps_graph.add('buffersink')
                        buffer.link_to(fps_filter)
                        fps_filter.link_to(sink)
                        fps_graph.configure()

                seg_resampler = None
                if seg_a_stream is not None and out_audio is not None:
                    seg_resampler = av.AudioResampler(
                        format='fltp',
                        layout=audio_layout,
                        rate=sample_rate,
                    )

                streams_to_demux = [
                    s
                    for s in (seg_video_streams[:1] + seg_audio_streams[:1])
                    if s is not None
                ]

                for packet in seg_c.demux(*streams_to_demux):
                    if len(streams_past_end) >= len(streams_to_demux):
                        break
                    if packet.stream.index in streams_past_end:
                        continue

                    # Progress calculation relative to stream start timestamp throttled to >= 2% delta
                    if on_progress and total_duration_s > 0:
                        ts = packet.pts if packet.pts is not None else packet.dts
                        if ts is not None:
                            stream_idx = packet.stream.index
                            tb = (
                                float(packet.stream.time_base)
                                if packet.stream.time_base
                                else (1.0 / av.time_base)
                            )
                            current_ts_s = float(ts * tb)
                            if stream_idx not in stream_first_pts:
                                stream_first_pts[stream_idx] = current_ts_s

                            seg_elapsed_s = max(
                                0.0, current_ts_s - stream_first_pts[stream_idx]
                            )
                            if (
                                seg_idx < len(segment_durations)
                                and segment_durations[seg_idx] > 0
                            ):
                                seg_elapsed_s = min(
                                    seg_elapsed_s, segment_durations[seg_idx]
                                )
                            elapsed_s = seg_accumulated_duration_s + seg_elapsed_s
                            pct = min(0.99, max(0.0, elapsed_s / total_duration_s))
                            reported_pct = round(pct, 4)
                            if reported_pct > last_reported_pct:
                                on_progress(reported_pct)
                                last_reported_pct = reported_pct

                    if packet.stream.type == 'video' and out_video is not None:
                        if can_stream_copy_video and in_v is not None:
                            if packet.dts is None and packet.pts is None:
                                continue
                            rescale_pts(
                                packet, packet.stream.time_base, out_video.time_base
                            )
                            if out_video.index not in stream_initial_dts:
                                first_ts = (
                                    packet.dts if packet.dts is not None else packet.pts
                                )
                                stream_initial_dts[out_video.index] = (
                                    first_ts if first_ts is not None else 0
                                )
                            init_dts = stream_initial_dts[out_video.index]
                            if packet.dts is not None:
                                packet.dts -= init_dts
                            if packet.pts is not None:
                                packet.pts -= init_dts
                            packet.stream = out_video
                            _mux_packet_with_monotonic_dts(
                                out_container, packet, out_video.index, last_dts
                            )
                            seg_had_video = True
                            continue

                        vpacket = cast('Packet[VideoStream]', packet)
                        try:
                            decoded_frames = vpacket.decode()
                        except (av.error.InvalidDataError, av.FFmpegError) as exc:
                            logger.warning(
                                'Skipping unparseable video packet in transcode: %s',
                                exc,
                            )
                            continue
                        for frame in decoded_frames:
                            time_base = (
                                float(frame.time_base)
                                if frame.time_base is not None
                                else (
                                    float(packet.stream.time_base)
                                    if packet.stream.time_base is not None
                                    else 1.0
                                )
                            )
                            frame_time_s = (
                                float(frame.pts * time_base)
                                if frame.pts is not None
                                else (
                                    float(frame.time) if frame.time is not None else 0.0
                                )
                            )
                            if seg_end is not None and frame_time_s > seg_end:
                                streams_past_end.add(packet.stream.index)
                                break
                            if frame_time_s < seg_start:
                                continue

                            seg_had_video = True
                            if fps_graph is not None:
                                fps_graph.push(frame)
                                while True:
                                    try:
                                        emit_video_frame(fps_graph.vpull())
                                    except av.BlockingIOError, av.EOFError:
                                        break
                            else:
                                emit_video_frame(frame)

                    elif packet.stream.type == 'audio' and out_audio is not None:
                        apacket = cast('Packet[AudioStream]', packet)
                        try:
                            decoded_audio_frames = apacket.decode()
                        except (av.error.InvalidDataError, av.FFmpegError) as exc:
                            logger.warning(
                                'Skipping unparseable audio packet in transcode: %s',
                                exc,
                            )
                            continue
                        for a_frame in decoded_audio_frames:
                            time_base = (
                                float(a_frame.time_base)
                                if a_frame.time_base is not None
                                else (
                                    float(packet.stream.time_base)
                                    if packet.stream.time_base is not None
                                    else 1.0
                                )
                            )
                            frame_time_s = (
                                float(a_frame.pts * time_base)
                                if a_frame.pts is not None
                                else (
                                    float(a_frame.time)
                                    if a_frame.time is not None
                                    else 0.0
                                )
                            )
                            if seg_end is not None and frame_time_s > seg_end:
                                streams_past_end.add(packet.stream.index)
                                break
                            if frame_time_s < seg_start:
                                continue

                            seg_had_audio = True
                            if seg_resampler is not None:
                                for r_frame in seg_resampler.resample(a_frame):
                                    feed_audio_frame(r_frame)
                            else:
                                feed_audio_frame(a_frame)

                # Flush segment's fps_graph if any
                if fps_graph is not None and out_video is not None:
                    fps_graph.push(None)
                    while True:
                        try:
                            emit_video_frame(fps_graph.vpull())
                        except av.BlockingIOError, av.EOFError:
                            break

                # Flush segment's resampler if any
                if seg_resampler is not None and out_audio is not None:
                    for r_frame in seg_resampler.resample(None):
                        seg_had_audio = True
                        feed_audio_frame(r_frame)

            # Pad silence if segment had no audio but audio stream is active and video advanced
            if (
                out_audio is not None
                and not can_stream_copy_audio
                and not seg_had_audio
                and video_frame_count > seg_v_start
            ):
                seg_duration_s = (video_frame_count - seg_v_start) / float(fps)
                needed_samples = round(seg_duration_s * sample_rate)
                num_channels = 2 if audio_layout == 'stereo' else 1
                samples_left = needed_samples
                chunk_size = 1024
                while samples_left > 0:
                    cur_chunk = min(chunk_size, samples_left)
                    silence_arr = np.zeros((num_channels, cur_chunk), dtype=np.float32)
                    silence_frame = av.AudioFrame.from_ndarray(
                        silence_arr, format='fltp', layout=audio_layout
                    )
                    silence_frame.sample_rate = sample_rate
                    feed_audio_frame(silence_frame)
                    samples_left -= cur_chunk

            # Pad black frames if segment had no video but video stream is active and audio advanced
            if (
                out_video is not None
                and not can_stream_copy_video
                and not seg_had_video
                and audio_sample_count > seg_a_start
            ):
                seg_duration_s = (audio_sample_count - seg_a_start) / float(sample_rate)
                needed_frames = round(seg_duration_s * float(fps))
                if needed_frames > 0:
                    black_frame = av.VideoFrame.from_ndarray(
                        np.zeros((out_height, out_width, 3), dtype=np.uint8),
                        format='rgb24',
                    ).reformat(format=active_preset.pix_fmt)
                    for _ in range(needed_frames):
                        emit_video_frame(black_frame)

        # Flush audio filter graph (if present)
        if audio_graph is not None and out_audio is not None:
            audio_graph.push(None)
            while True:
                try:
                    out_f = audio_graph.pull()
                    emit_audio_frame(cast(AudioFrame, out_f))
                except av.BlockingIOError, av.EOFError:
                    break

        # Flush encoders
        if out_video is not None and not can_stream_copy_video:
            for enc_packet in out_video.encode():
                _mux_packet_with_monotonic_dts(
                    out_container, enc_packet, out_video.index, last_dts
                )
        if out_audio is not None and not can_stream_copy_audio:
            for enc_packet in out_audio.encode():
                _mux_packet_with_monotonic_dts(
                    out_container, enc_packet, out_audio.index, last_dts
                )

        # Final 100% progress notification upon successful completion
        if on_progress:
            on_progress(1.0)
