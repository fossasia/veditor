from __future__ import annotations

import logging
from fractions import Fraction
from pathlib import Path
from typing import cast

import av
from av.audio.stream import AudioStream
from av.packet import Packet
from av.video.stream import VideoStream

from ..config import PreviewPreset
from .loudness import _mux_packet_with_monotonic_dts, rescale_pts


logger = logging.getLogger(__name__)


def generate_preview(
    input_path: Path,
    output_path: Path,
    preset: PreviewPreset,
    threads: int | None = None,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
) -> None:
    """
    Generate a low-resolution review clip from an input recording using PyAV.

    Applies the target resolution, video bitrate (or CRF), audio bitrate,
    and speed preset specified by the given PreviewPreset.
    """
    if threads is not None and threads <= 0:
        raise ValueError(f'threads must be greater than zero: {threads}')
    if start_seconds < 0:
        raise ValueError(f'start_seconds must be non-negative: {start_seconds}')
    if end_seconds is not None and end_seconds <= start_seconds:
        raise ValueError(
            f'end_seconds ({end_seconds}) must be greater than start_seconds ({start_seconds})'
        )

    container_options = (
        {'movflags': 'faststart'} if output_path.suffix.lower() == '.mp4' else {}
    )
    with (
        av.open(str(input_path)) as in_container,
        av.open(str(output_path), mode='w', options=container_options) as out_container,
    ):
        if start_seconds > 0.0:
            in_container.seek(
                int(start_seconds * av.time_base),
                backward=True,
                any_frame=False,
            )
        in_video = in_container.streams.video[0] if in_container.streams.video else None
        in_audio = in_container.streams.audio[0] if in_container.streams.audio else None

        if not in_video and not in_audio:
            raise ValueError(f'No video or audio streams found in {input_path}')

        out_video = None
        out_audio = None
        width, height = preset.resolution

        stride = 1
        fps = 24
        filter_graph = None
        if in_video:
            raw_fps = float(in_video.guessed_rate or in_video.average_rate or 24)
            stride = max(1, round(raw_fps / 24.0)) if raw_fps > 30.0 else 1
            fps = max(1, round(raw_fps / stride))
            speed_preset = preset.preset_speed or 'ultrafast'
            options = {
                'preset': speed_preset,
                'tune': 'fastdecode,zerolatency',
            }
            if preset.crf is not None:
                options['crf'] = str(preset.crf)
            if threads is not None:
                options['threads'] = str(threads)
            out_video = out_container.add_stream('libx264', rate=fps, options=options)
            out_video.width = width
            out_video.height = height
            out_video.pix_fmt = 'yuv420p'
            if preset.crf is None:
                out_video.bit_rate = preset.video_bitrate

            filter_graph = av.filter.Graph()
            buf = filter_graph.add_buffer(template=in_video)
            scale = filter_graph.add('scale', f'{width}:{height}:flags=bilinear')
            sink = filter_graph.add('buffersink')
            buf.link_to(scale)
            scale.link_to(sink)
            filter_graph.configure()

        stream_copy_audio = False
        if in_audio:
            codec_name = (
                in_audio.codec_context.name.lower()
                if (in_audio.codec_context and in_audio.codec_context.name)
                else ''
            )
            if codec_name in ('aac', 'mp3') and start_seconds <= 0.0:
                stream_copy_audio = True
                out_audio = out_container.add_stream_from_template(in_audio)
            else:
                out_audio = out_container.add_stream('aac', rate=in_audio.rate or 44100)
                out_audio.bit_rate = preset.audio_bitrate
                out_audio.layout = in_audio.layout.name if in_audio.layout else 'mono'

        last_dts: dict[int, int] = {}
        streams = [s for s in (in_video, in_audio) if s is not None]
        video_frame_count = 0
        input_frame_count = 0
        audio_sample_count = 0
        streams_past_end: set[int] = set()
        for packet in in_container.demux(*streams):
            if len(streams_past_end) >= len(streams):
                break
            if packet.stream.index in streams_past_end:
                continue

            if packet.stream.type == 'video' and out_video:
                vpacket = cast('Packet[VideoStream]', packet)
                assert filter_graph is not None
                try:
                    decoded_frames = vpacket.decode()
                except (av.error.InvalidDataError, av.FFmpegError) as exc:
                    logger.warning(
                        'Skipping unparseable video packet in preview: %s', exc
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
                        else (float(frame.time) if frame.time is not None else 0.0)
                    )
                    if end_seconds is not None and frame_time_s > end_seconds:
                        streams_past_end.add(packet.stream.index)
                        break
                    if frame_time_s < start_seconds:
                        continue

                    skip = stride > 1 and (input_frame_count % stride != 0)
                    input_frame_count += 1
                    if skip:
                        continue

                    filter_graph.push(frame)
                    while True:
                        try:
                            scaled_frame = filter_graph.vpull()
                        except av.FFmpegError, EOFError:
                            break
                        scaled_frame.pts = video_frame_count
                        scaled_frame.time_base = Fraction(1, fps)
                        video_frame_count += 1
                        for out_pkt in out_video.encode(scaled_frame):
                            _mux_packet_with_monotonic_dts(
                                out_container, out_pkt, out_video.index, last_dts
                            )

            elif packet.stream.type == 'audio' and out_audio:
                apacket = cast('Packet[AudioStream]', packet)
                assert in_audio is not None
                if stream_copy_audio:
                    if end_seconds is not None:
                        time_base = (
                            float(in_audio.time_base)
                            if in_audio.time_base is not None
                            else 1.0
                        )
                        packet_time_s = (
                            float(packet.pts * time_base)
                            if packet.pts is not None
                            else (
                                float(packet.dts * time_base)
                                if packet.dts is not None
                                else 0.0
                            )
                        )
                        if packet_time_s > end_seconds:
                            streams_past_end.add(packet.stream.index)
                            continue

                    rescale_pts(packet, in_audio.time_base, out_audio.time_base)
                    packet.stream = out_audio
                    _mux_packet_with_monotonic_dts(
                        out_container, packet, out_audio.index, last_dts
                    )
                else:
                    try:
                        decoded_audio_frames = apacket.decode()
                    except (av.error.InvalidDataError, av.FFmpegError) as exc:
                        logger.warning(
                            'Skipping unparseable audio packet in preview: %s', exc
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
                                float(a_frame.time) if a_frame.time is not None else 0.0
                            )
                        )
                        if end_seconds is not None and frame_time_s > end_seconds:
                            streams_past_end.add(packet.stream.index)
                            break
                        if frame_time_s < start_seconds:
                            continue

                        a_frame.pts = audio_sample_count
                        a_frame.time_base = Fraction(1, in_audio.rate or 44100)
                        audio_sample_count += a_frame.samples
                        for out_pkt in out_audio.encode(a_frame):
                            _mux_packet_with_monotonic_dts(
                                out_container, out_pkt, out_audio.index, last_dts
                            )

        if filter_graph and out_video:
            filter_graph.push(None)
            while True:
                try:
                    scaled_frame = filter_graph.vpull()
                except av.FFmpegError, EOFError:
                    break
                scaled_frame.pts = video_frame_count
                scaled_frame.time_base = Fraction(1, fps)
                video_frame_count += 1
                for out_pkt in out_video.encode(scaled_frame):
                    _mux_packet_with_monotonic_dts(
                        out_container, out_pkt, out_video.index, last_dts
                    )

        if out_video:
            for out_pkt in out_video.encode():
                _mux_packet_with_monotonic_dts(
                    out_container, out_pkt, out_video.index, last_dts
                )

        if out_audio and not stream_copy_audio:
            for out_pkt in out_audio.encode():
                _mux_packet_with_monotonic_dts(
                    out_container, out_pkt, out_audio.index, last_dts
                )
