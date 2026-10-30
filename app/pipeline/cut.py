"""Trims a media file to scheduled start and end bounds.

Attempts fast, lossless stream-copy remuxing by default. Falls back to full
decode and re-encode if stream copy fails or is incompatible with the output container.
"""

from __future__ import annotations

import logging
import struct
from enum import Enum
from fractions import Fraction
from pathlib import Path

import av

from app.pipeline.loudness import _mux_packet_with_monotonic_dts, rescale_pts

logger = logging.getLogger(__name__)


def _is_avcc(stream: av.video.stream.VideoStream) -> bool:
    """Check if stream uses AVCC / HVCC length-prefixed packet format."""
    extra = (
        bytes(stream.codec_context.extradata) if stream.codec_context.extradata else b""
    )
    return len(extra) >= 7 and extra[0] == 1


def _extract_sps_pps_from_avcc(extra: bytes) -> tuple[list[bytes], list[bytes]]:
    """Extract SPS and PPS parameter sets from an avcC extradata buffer."""
    if not extra or len(extra) < 7 or extra[0] != 1:
        return [], []
    sps_list: list[bytes] = []
    pps_list: list[bytes] = []
    num_sps = extra[5] & 0x1F
    idx = 6
    for _ in range(num_sps):
        if idx + 2 > len(extra):
            break
        sps_len = struct.unpack(">H", extra[idx : idx + 2])[0]
        idx += 2
        sps_list.append(extra[idx : idx + sps_len])
        idx += sps_len
    if idx < len(extra):
        num_pps = extra[idx]
        idx += 1
        for _ in range(num_pps):
            if idx + 2 > len(extra):
                break
            pps_len = struct.unpack(">H", extra[idx : idx + 2])[0]
            idx += 2
            pps_list.append(extra[idx : idx + pps_len])
            idx += pps_len
    return sps_list, pps_list


def _annexb_to_avcc(data: bytes) -> bytes:
    """Convert Annex B (start-code-separated) NALUs to AVCC (length-prefixed)."""
    nalus: list[bytes] = []
    i = 0
    n = len(data)
    start = -1
    while i < n:
        if i + 4 <= n and data[i : i + 4] == b"\x00\x00\x00\x01":
            if start != -1:
                nalus.append(data[start:i])
            i += 4
            start = i
        elif i + 3 <= n and data[i : i + 3] == b"\x00\x00\x01":
            if start != -1:
                nalus.append(data[start:i])
            i += 3
            start = i
        else:
            i += 1
    if start != -1 and start < n:
        nalus.append(data[start:n])

    out = bytearray()
    for nalu in nalus:
        if not nalu:
            continue
        out.extend(struct.pack(">I", len(nalu)))
        out.extend(nalu)
    return bytes(out)


class CutStrategy(str, Enum):
    STREAM_COPY = "stream_copy"
    SMART_CUT = "smart_cut"


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
    threads: int | None = None,
    frame_accurate_fallback: bool = False,
) -> CutStrategy:
    """Trim an input recording to the [start_seconds, end_seconds] window.

    Attempts smart_cut (frame-accurate head/tail re-encode with interior stream copy).
    If smart_cut fails, falls back to stream_copy to the nearest keyframes.
    If stream_copy also fails, logs an error and raises RuntimeError.

    Args:
        input_path: Path to the raw source recording.
        output_path: Destination path for the trimmed output.
        start_seconds: Start timestamp in seconds (non-negative).
        end_seconds: End timestamp in seconds (greater than start_seconds).
        threads: Optional thread limit for video head/tail encoding.

    Returns:
        CutStrategy: CutStrategy.SMART_CUT or CutStrategy.STREAM_COPY.

    Raises:
        ValueError: If timestamps or input paths are invalid.
        RuntimeError: If both smart cut and stream copy fallback fail.
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

    if threads is not None and threads <= 0:
        raise ValueError(f"threads must be greater than zero: {threads}")

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    smart_err: str | None = None
    try:
        strategy = _cut_smart(
            str(in_path), str(out_path), start_seconds, end_seconds, threads=threads
        )
        logger.info(
            "Cut completed for %s [%.2fs -> %.2fs] using strategy: %s",
            in_path.name,
            start_seconds,
            end_seconds,
            strategy.value,
        )
        return strategy
    except Exception as smart_exc:  # noqa: BLE001
        smart_err = str(smart_exc)
        if frame_accurate_fallback:
            logger.info(
                "Smart cut failed for %s [%.2fs -> %.2fs] (%s); falling back to frame-accurate re-encode.",
                in_path.name,
                start_seconds,
                end_seconds,
                smart_exc,
            )
            return _cut_frame_accurate(
                str(in_path), str(out_path), start_seconds, end_seconds, threads=threads
            )
        logger.warning(
            "Smart cut failed for %s [%.2fs -> %.2fs] (%s); falling back to stream copy at nearest keyframes.",
            in_path.name,
            start_seconds,
            end_seconds,
            smart_exc,
        )

    try:
        strategy = _cut_stream_copy(
            str(in_path), str(out_path), start_seconds, end_seconds
        )
        logger.info(
            "Cut completed for %s [%.2fs -> %.2fs] using fallback strategy: %s",
            in_path.name,
            start_seconds,
            end_seconds,
            strategy.value,
        )
        return strategy
    except Exception as stream_exc:
        logger.error(
            "Both smart cut and stream copy fallback failed for %s [%.2fs -> %.2fs]: %s",
            in_path.name,
            start_seconds,
            end_seconds,
            stream_exc,
        )
        raise RuntimeError(
            f"Trimming failed for {in_path.name} [{start_seconds:.2f}s -> {end_seconds:.2f}s]: "
            f"smart cut failed ({smart_err}), stream copy fallback failed ({stream_exc})"
        ) from stream_exc


def _cut_frame_accurate(
    input_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
    *,
    threads: int | None = None,
) -> CutStrategy:
    """Frame-accurate cut re-encoding [start_seconds, end_seconds]."""
    with av.open(input_path) as ref_c:
        in_v = ref_c.streams.video[0] if ref_c.streams.video else None
        in_a = ref_c.streams.audio[0] if ref_c.streams.audio else None
        if not in_v and not in_a:
            raise ValueError(f"No media streams found in {input_path}")

        has_video = bool(in_v)
        has_audio = bool(in_a)

        can_w = 1920
        can_h = 1080
        can_fps = Fraction(24, 1)
        v_enc_name = "libx264"
        if has_video:
            raw_w = in_v.codec_context.width or 1920
            raw_h = in_v.codec_context.height or 1080
            can_w = (raw_w // 2) * 2
            can_h = (raw_h // 2) * 2
            fps_val = in_v.average_rate or in_v.guessed_rate or 24
            can_fps = Fraction(fps_val) if fps_val else Fraction(24, 1)
            v_enc_name = _resolve_video_encoder(in_v.codec_context.name)

        can_sr = 44100
        can_layout = "stereo"
        a_enc_name = "aac"
        if has_audio:
            can_sr = in_a.codec_context.sample_rate or 44100
            if in_a.codec_context.layout and in_a.codec_context.layout.name:
                can_layout = in_a.codec_context.layout.name
            elif in_a.codec_context.channels == 1:
                can_layout = "mono"
            elif in_a.codec_context.channels == 2:
                can_layout = "stereo"
            a_enc_name = _resolve_audio_encoder(in_a.codec_context.name)

    container_options: dict[str, str] = {}
    if str(output_path).lower().endswith((".mp4", ".m4v", ".mov")):
        container_options["movflags"] = "faststart"

    with av.open(
        output_path, mode="w", options=container_options or None
    ) as out_container:
        out_v = None
        if has_video:
            video_options: dict[str, str] = {"crf": "22", "preset": "ultrafast"}
            if threads is not None:
                video_options["threads"] = str(threads)
            out_v = out_container.add_stream(
                v_enc_name, rate=can_fps, options=video_options
            )
            out_v.codec_context.time_base = Fraction(1, 90000)
            out_v.width = can_w
            out_v.height = can_h
            out_v.pix_fmt = "yuv420p"

        out_a = None
        if has_audio:
            out_a = out_container.add_stream(a_enc_name, rate=can_sr)
            out_a.layout = can_layout

        last_v_pts: int | None = None
        a_pts = 0

        with av.open(input_path) as in_c:
            streams = [
                s
                for s in (
                    in_c.streams.video[0] if in_c.streams.video else None,
                    in_c.streams.audio[0] if in_c.streams.audio else None,
                )
                if s is not None
            ]
            seek_target = max(0, int((start_seconds - 1.0) * av.time_base))
            in_c.seek(seek_target, backward=True, any_frame=False)

            resampler = (
                av.AudioResampler(format="fltp", layout=can_layout, rate=can_sr)
                if out_a
                else None
            )

            for packet in in_c.demux(*streams):
                try:
                    frames = packet.decode()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Skipping unparseable packet during frame-accurate cut: %s", exc
                    )
                    continue
                for frame in frames:
                    if isinstance(frame, av.VideoFrame) and out_v:
                        t = (
                            float(frame.pts * frame.time_base)
                            if frame.pts is not None
                            else float(frame.time or 0)
                        )
                        if t < start_seconds - 0.001:
                            continue
                        if t > end_seconds + 0.001:
                            continue
                        rf = frame.reformat(width=can_w, height=can_h, format="yuv420p")
                        offset_s = max(0.0, t - start_seconds)
                        target_pts = round(offset_s * 90000)
                        v_pts = max(
                            target_pts,
                            (last_v_pts + 1) if last_v_pts is not None else 0,
                        )
                        last_v_pts = v_pts
                        rf.pts = v_pts
                        rf.time_base = Fraction(1, 90000)
                        for enc_p in out_v.encode(rf):
                            out_container.mux(enc_p)
                    elif isinstance(frame, av.AudioFrame) and out_a:
                        t = (
                            float(frame.pts * frame.time_base)
                            if frame.pts is not None
                            else float(frame.time or 0)
                        )
                        if t < start_seconds - 0.05:
                            continue
                        if t > end_seconds + 0.05:
                            continue
                        resampled_list = (
                            resampler.resample(frame) if resampler else [frame]
                        )
                        for rf in resampled_list:
                            rf.pts = a_pts
                            rf.time_base = Fraction(1, can_sr)
                            a_pts += rf.samples
                            for enc_p in out_a.encode(rf):
                                out_container.mux(enc_p)

        if resampler and out_a:
            for rf in resampler.resample(None):
                rf.pts = a_pts
                rf.time_base = Fraction(1, can_sr)
                a_pts += rf.samples
                for enc_p in out_a.encode(rf):
                    out_container.mux(enc_p)

        if out_v:
            for enc_p in out_v.encode():
                out_container.mux(enc_p)
        if out_a:
            for enc_p in out_a.encode():
                out_container.mux(enc_p)

    return CutStrategy.SMART_CUT


def _cut_smart(
    input_path: str,
    output_path: str,
    start_seconds: float,
    end_seconds: float,
    threads: int | None = None,
) -> CutStrategy:
    """Frame-accurate smart / hybrid cut with stream copy of interior GOPs."""
    with av.open(input_path) as in_container:
        video_streams = list(in_container.streams.video)
        audio_streams = list(in_container.streams.audio)

        if not video_streams and not audio_streams:
            raise ValueError(f"No audio or video streams found in {input_path}")

        # If no video stream (e.g. audio-only), fallback to stream copy
        if not video_streams:
            return _cut_stream_copy(input_path, output_path, start_seconds, end_seconds)

        in_v = video_streams[0]
        in_a = audio_streams[0] if audio_streams else None

        fps_val = float(in_v.average_rate or in_v.guessed_rate or 24)
        fps = round(fps_val) if fps_val > 0 else 24
        epsilon = 0.5 / fps

        # Collect keyframe timestamps
        keyframes: list[float] = []
        for packet in in_container.demux(in_v):
            if packet.is_keyframe and packet.pts is not None:
                ts = float(packet.pts * in_v.time_base)
                keyframes.append(ts)
                if ts > end_seconds + 5.0:
                    break

        if not keyframes:
            keyframes = [0.0]

        pre_candidates = [k for k in keyframes if k <= start_seconds + epsilon]
        K_pre = max(pre_candidates) if pre_candidates else 0.0

        is_start_on_keyframe = abs(start_seconds - K_pre) <= epsilon

        video_duration = (
            float(in_v.duration * in_v.time_base)
            if in_v.duration
            else (
                float(in_container.duration / av.time_base)
                if in_container.duration
                else None
            )
        )

        nearest_k_end = (
            min((abs(end_seconds - k), k) for k in keyframes)[1] if keyframes else None
        )
        is_end_on_keyframe = (
            nearest_k_end is not None and abs(end_seconds - nearest_k_end) <= epsilon
        ) or (video_duration is not None and end_seconds >= video_duration - epsilon)

        # Exact keyframe bounds allow pure stream-copy
        if is_start_on_keyframe and is_end_on_keyframe:
            return _cut_stream_copy(input_path, output_path, start_seconds, end_seconds)

        if is_start_on_keyframe:
            K_start_body = K_pre
        else:
            next_candidates = [k for k in keyframes if k > start_seconds + epsilon]
            K_next = min(next_candidates) if next_candidates else None
            if K_next is None or K_next >= end_seconds:
                raise RuntimeError(
                    f"Smart cut cannot trim interval [{start_seconds:.2f}s, {end_seconds:.2f}s]: "
                    "no interior keyframe found between start and end bounds."
                )
            K_start_body = K_next

        # Last keyframe before or at end
        last_candidates = [k for k in keyframes if k <= end_seconds + epsilon]
        K_last = max(last_candidates) if last_candidates else None

        if is_end_on_keyframe:
            K_end_body = end_seconds
        else:
            if K_last is None or K_last <= K_start_body:
                raise RuntimeError(
                    f"Smart cut cannot trim interval [{start_seconds:.2f}s, {end_seconds:.2f}s]: "
                    "interval is smaller than a single GOP (no stream-copyable interior)."
                )
            K_end_body = K_last

    # Head: decode from K_pre, re-encode frames in [start_seconds, K_start_body)
    head_frames: list[av.VideoFrame] = []
    if not is_start_on_keyframe:
        with av.open(input_path) as container:
            v = container.streams.video[0]
            container.seek(int(K_pre * av.time_base), backward=True, any_frame=False)
            for pkt in container.demux(v):
                try:
                    decoded_frames = pkt.decode()
                except (av.error.InvalidDataError, av.FFmpegError) as exc:
                    logger.warning("Skipping unparseable head packet in cut: %s", exc)
                    continue
                for frame in decoded_frames:
                    t = (
                        float(frame.pts * v.time_base)
                        if frame.pts is not None
                        else (float(frame.time) if frame.time is not None else 0.0)
                    )
                    if t >= K_start_body - epsilon:
                        break
                    if t >= start_seconds - epsilon:
                        head_frames.append(frame)
                if head_frames and t >= K_start_body - epsilon:
                    break

    # Tail: decode from K_end_body, re-encode frames in [K_end_body, end_seconds]
    tail_frames: list[av.VideoFrame] = []
    if not is_end_on_keyframe:
        with av.open(input_path) as container:
            v = container.streams.video[0]
            container.seek(
                int(K_end_body * av.time_base), backward=True, any_frame=False
            )
            for pkt in container.demux(v):
                try:
                    decoded_frames = pkt.decode()
                except (av.error.InvalidDataError, av.FFmpegError) as exc:
                    logger.warning("Skipping unparseable tail packet in cut: %s", exc)
                    continue
                for frame in decoded_frames:
                    t = (
                        float(frame.pts * v.time_base)
                        if frame.pts is not None
                        else (float(frame.time) if frame.time is not None else 0.0)
                    )
                    if t > end_seconds + epsilon:
                        break
                    if t >= K_end_body - epsilon:
                        tail_frames.append(frame)
                if tail_frames and t > end_seconds + epsilon:
                    break

    container_options = (
        {"movflags": "faststart"}
        if output_path.lower().endswith((".mp4", ".mov", ".m4v"))
        else {}
    )
    with (
        av.open(input_path) as in_container,
        av.open(output_path, mode="w", options=container_options) as out_container,
    ):
        in_v = in_container.streams.video[0]
        in_a = in_container.streams.audio[0] if in_container.streams.audio else None
        out_v = out_container.add_stream_from_template(in_v)
        is_avcc = _is_avcc(in_v)
        extra = (
            bytes(in_v.codec_context.extradata) if in_v.codec_context.extradata else b""
        )
        sps_list, pps_list = _extract_sps_pps_from_avcc(extra)
        out_a = None
        if in_a:
            try:
                out_a = out_container.add_stream_from_template(in_a)
            except ValueError, av.FFmpegError:
                sample_rate = in_a.codec_context.sample_rate or 44100
                channels = in_a.codec_context.channels or 1
                out_a = out_container.add_stream("aac", rate=sample_rate)
                if in_a.codec_context.layout:
                    out_a.layout = in_a.codec_context.layout.name
                elif channels == 2:
                    out_a.layout = "stereo"
                else:
                    out_a.layout = "mono"

        last_dts: dict[int, int] = {}

        # 1. Stream-copy audio packets from start_seconds to end_seconds
        if in_a and out_a:
            in_container.seek(
                int(start_seconds * av.time_base),
                backward=True,
                any_frame=False,
            )
            first_a_pts = None
            for p in in_container.demux(in_a):
                if p.pts is None:
                    continue
                t = float(p.pts * in_a.time_base)
                if t > end_seconds:
                    break
                if t >= start_seconds:
                    if first_a_pts is None:
                        first_a_pts = p.pts
                    p.pts -= first_a_pts
                    if p.dts is not None:
                        p.dts -= first_a_pts
                    rescale_pts(p, in_a.time_base, out_a.time_base)
                    p.stream = out_a
                    _mux_packet_with_monotonic_dts(
                        out_container, p, out_a.index, last_dts
                    )

        pts_per_frame = round(Fraction(1, fps) / out_v.time_base)

        # 2. Encode & Mux Video Head [start_seconds, K_start_body)
        if head_frames:
            head_options = {"preset": "ultrafast"}
            if threads is not None:
                head_options["threads"] = str(threads)
            if in_v.profile:
                head_options["profile"] = in_v.profile.lower().split()[0]
            encoder_name = _resolve_video_encoder(in_v.codec_context.name)
            head_ctx = av.CodecContext.create(av.Codec(encoder_name, "w"))
            head_ctx.width = in_v.codec_context.width
            head_ctx.height = in_v.codec_context.height
            head_ctx.pix_fmt = in_v.codec_context.pix_fmt or "yuv420p"
            head_ctx.time_base = Fraction(1, fps)
            head_ctx.framerate = Fraction(fps, 1)
            head_ctx.options = head_options
            head_ctx.open()

            for i, h_frame in enumerate(head_frames):
                if h_frame.format.name != head_ctx.pix_fmt:
                    h_frame = h_frame.reformat(format=head_ctx.pix_fmt)
                h_frame.pts = i
                h_frame.time_base = head_ctx.time_base
                for p in head_ctx.encode(h_frame):
                    if is_avcc:
                        avcc_bytes = _annexb_to_avcc(bytes(p))
                        npkt = av.Packet(avcc_bytes)
                        npkt.pts = p.pts
                        npkt.dts = p.dts
                        npkt.time_base = head_ctx.time_base
                        p = npkt
                    rescale_pts(p, head_ctx.time_base, out_v.time_base)
                    p.stream = out_v
                    _mux_packet_with_monotonic_dts(
                        out_container, p, out_v.index, last_dts
                    )
            for p in head_ctx.encode():
                if is_avcc:
                    avcc_bytes = _annexb_to_avcc(bytes(p))
                    npkt = av.Packet(avcc_bytes)
                    npkt.pts = p.pts
                    npkt.dts = p.dts
                    npkt.time_base = head_ctx.time_base
                    p = npkt
                rescale_pts(p, head_ctx.time_base, out_v.time_base)
                p.stream = out_v
                _mux_packet_with_monotonic_dts(out_container, p, out_v.index, last_dts)

        body_start_pts = len(head_frames) * pts_per_frame
        max_body_pts = body_start_pts

        # 3. Stream-copy Video Body [K_start_body, K_end_body)
        in_container.seek(
            int(K_start_body * av.time_base), backward=True, any_frame=False
        )
        body_first_dts = None
        body_first_pts = None
        for p in in_container.demux(in_v):
            if p.pts is None:
                continue
            t = float(p.pts * in_v.time_base)
            if t >= K_end_body - epsilon:
                break
            if t >= K_start_body - epsilon:
                if body_first_dts is None:
                    if not p.is_keyframe:
                        continue
                    body_first_dts = p.dts if p.dts is not None else p.pts
                    body_first_pts = p.pts
                    if is_avcc and sps_list and pps_list:
                        prefix = bytearray()
                        for s in sps_list:
                            prefix.extend(struct.pack(">I", len(s)))
                            prefix.extend(s)
                        for pp in pps_list:
                            prefix.extend(struct.pack(">I", len(pp)))
                            prefix.extend(pp)
                        prefix.extend(bytes(p))
                        npkt = av.Packet(bytes(prefix))
                        npkt.pts = p.pts
                        npkt.dts = p.dts
                        npkt.time_base = p.time_base
                        p = npkt
                p.pts = p.pts - body_first_pts
                p.dts = (p.dts if p.dts is not None else p.pts) - body_first_dts
                rescale_pts(p, in_v.time_base, out_v.time_base)
                p.pts += body_start_pts
                p.dts += body_start_pts
                dur = p.duration if p.duration else pts_per_frame
                max_body_pts = max(max_body_pts, p.pts + dur)
                p.stream = out_v
                _mux_packet_with_monotonic_dts(out_container, p, out_v.index, last_dts)

        tail_start_pts = max(body_start_pts, max_body_pts)

        # 4. Encode & Mux Video Tail [K_end_body, end_seconds]
        if tail_frames:
            tail_options = {"preset": "ultrafast"}
            if threads is not None:
                tail_options["threads"] = str(threads)
            if in_v.profile:
                tail_options["profile"] = in_v.profile.lower().split()[0]
            encoder_name = _resolve_video_encoder(in_v.codec_context.name)
            tail_ctx = av.CodecContext.create(av.Codec(encoder_name, "w"))
            tail_ctx.width = in_v.codec_context.width
            tail_ctx.height = in_v.codec_context.height
            tail_ctx.pix_fmt = in_v.codec_context.pix_fmt or "yuv420p"
            tail_ctx.time_base = Fraction(1, fps)
            tail_ctx.framerate = Fraction(fps, 1)
            tail_ctx.options = tail_options
            tail_ctx.open()

            for j, t_frame in enumerate(tail_frames):
                if t_frame.format.name != tail_ctx.pix_fmt:
                    t_frame = t_frame.reformat(format=tail_ctx.pix_fmt)
                t_frame.pts = j
                t_frame.time_base = tail_ctx.time_base
                for p in tail_ctx.encode(t_frame):
                    if is_avcc:
                        avcc_bytes = _annexb_to_avcc(bytes(p))
                        npkt = av.Packet(avcc_bytes)
                        npkt.pts = p.pts
                        npkt.dts = p.dts
                        npkt.time_base = tail_ctx.time_base
                        p = npkt
                    rescale_pts(p, tail_ctx.time_base, out_v.time_base)
                    p.pts += tail_start_pts
                    if p.dts is not None:
                        p.dts += tail_start_pts
                    p.stream = out_v
                    _mux_packet_with_monotonic_dts(
                        out_container, p, out_v.index, last_dts
                    )
            for p in tail_ctx.encode():
                if is_avcc:
                    avcc_bytes = _annexb_to_avcc(bytes(p))
                    npkt = av.Packet(avcc_bytes)
                    npkt.pts = p.pts
                    npkt.dts = p.dts
                    npkt.time_base = tail_ctx.time_base
                    p = npkt
                rescale_pts(p, tail_ctx.time_base, out_v.time_base)
                p.pts += tail_start_pts
                if p.dts is not None:
                    p.dts += tail_start_pts
                p.stream = out_v
                _mux_packet_with_monotonic_dts(out_container, p, out_v.index, last_dts)

    return CutStrategy.SMART_CUT


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

                if (
                    stream.type == "video"
                    and stream.index not in offset_map
                    and not packet.is_keyframe
                ):
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
