"""Concatenation module for joining intro, cut speech, and outro media segments.

Combines media segments (e.g., talk cut, optional intro slate, optional outro slate)
into a unified presentation video. Favors lossless stream-copy remuxing when
compatible; falls back to full video/audio re-encoding with resamplers and pixel format
adaptation when codecs, resolutions, frame rates, or sample rates differ.
"""

from __future__ import annotations

import logging
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
import numpy as np

from app.storage import LocalDiskBackend, StorageBackend

logger = logging.getLogger(__name__)


def _can_stream_copy(segments: list[Path]) -> bool:
    """Check if all segments have compatible streams for lossless stream copying."""
    if not segments:
        return False

    ref_video_props: list[tuple[Any, ...]] | None = None
    ref_audio_props: list[tuple[Any, ...]] | None = None

    for seg_p in segments:
        try:
            with av.open(str(seg_p)) as container:
                video_streams = list(container.streams.video)
                audio_streams = list(container.streams.audio)

                if not video_streams and not audio_streams:
                    return False

                video_props: list[tuple[Any, ...]] = []
                for v in video_streams:
                    ctx = v.codec_context
                    rate = v.average_rate or v.guessed_rate
                    fps_val = round(float(rate), 2) if rate else None
                    video_props.append(
                        (
                            ctx.name,
                            ctx.width,
                            ctx.height,
                            ctx.pix_fmt,
                            fps_val,
                            v.time_base,
                            bytes(ctx.extradata or b""),
                        )
                    )

                audio_props: list[tuple[Any, ...]] = []
                for a in audio_streams:
                    ctx = a.codec_context
                    fmt_name = ctx.format.name if ctx.format else None
                    audio_props.append(
                        (
                            ctx.name,
                            ctx.sample_rate,
                            ctx.channels,
                            fmt_name,
                            a.time_base,
                            bytes(ctx.extradata or b""),
                        )
                    )

                if ref_video_props is None and ref_audio_props is None:
                    ref_video_props = video_props
                    ref_audio_props = audio_props
                else:
                    if video_props != ref_video_props or audio_props != ref_audio_props:
                        return False
        except (av.FFmpegError, ValueError, OSError) as _:
            return False

    return True


def _concat_stream_copy(
    segments: list[Path],
    output_path: Path,
    canonical_cut_path: Path | None = None,
) -> str:
    """Lossless stream-copy concatenation by remuxing packets with monotonic timestamps."""
    template_path = canonical_cut_path or segments[0]
    with av.open(str(template_path)) as template_c:
        streams_to_copy = [*template_c.streams.video, *template_c.streams.audio]
        if not streams_to_copy:
            raise ValueError(
                f"No audio or video streams found in template {template_path}"
            )

        container_options: dict[str, str] = {}
        if output_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
            container_options["movflags"] = "faststart"

        with av.open(
            str(output_path),
            mode="w",
            options=container_options if container_options else None,
        ) as out_container:
            out_streams: dict[tuple[str, int], av.stream.Stream] = {}
            type_counts: dict[str, int] = {}
            for s in streams_to_copy:
                idx = type_counts.get(s.type, 0)
                type_counts[s.type] = idx + 1
                out_streams[(s.type, idx)] = out_container.add_stream_from_template(s)

            last_dts_end: dict[int, int] = {}
            last_pts_end: dict[int, int] = {}

            for seg_idx, seg_p in enumerate(segments):
                with av.open(str(seg_p)) as in_container:
                    stream_map: dict[int, av.stream.Stream] = {}
                    in_type_counts: dict[str, int] = {}
                    for s in in_container.streams:
                        idx = in_type_counts.get(s.type, 0)
                        in_type_counts[s.type] = idx + 1
                        if (s.type, idx) in out_streams:
                            stream_map[s.index] = out_streams[(s.type, idx)]

                    seg_offset: dict[int, int] = {}

                    for packet in in_container.demux(*in_container.streams):
                        if packet.stream.index not in stream_map:
                            continue

                        out_s = stream_map[packet.stream.index]

                        if packet.dts is None:
                            if packet.pts is not None:
                                packet.dts = packet.pts
                            else:
                                continue

                        if packet.pts is None:
                            packet.pts = packet.dts

                        if out_s.index not in seg_offset:
                            if seg_idx == 0:
                                seg_offset[out_s.index] = -packet.dts
                            else:
                                prev_dts_end = last_dts_end.get(out_s.index, 0)
                                prev_pts_end = last_pts_end.get(out_s.index, 0)
                                seg_offset[out_s.index] = max(
                                    prev_dts_end - packet.dts,
                                    prev_pts_end - packet.pts,
                                )

                        shift = seg_offset[out_s.index]
                        packet.stream = out_s
                        packet.dts += shift
                        packet.pts += shift

                        dur = (
                            packet.duration
                            if (packet.duration is not None and packet.duration > 0)
                            else 1
                        )
                        last_dts_end[out_s.index] = max(
                            last_dts_end.get(out_s.index, 0), packet.dts + dur
                        )
                        last_pts_end[out_s.index] = max(
                            last_pts_end.get(out_s.index, 0), packet.pts + dur
                        )

                        out_container.mux(packet)

    return str(output_path)


def _concat_reencode(
    segments: list[Path],
    canonical_cut_path: Path,
    output_path: Path,
    threads: int | None = None,
) -> str:
    """Full frame decode, resample, and re-encode concatenation fallback."""
    with av.open(str(canonical_cut_path)) as ref_c:
        video_streams = list(ref_c.streams.video)
        audio_streams = list(ref_c.streams.audio)

        if not video_streams and not audio_streams:
            raise ValueError(
                f"No audio or video streams found in canonical cut {canonical_cut_path}"
            )

        has_video = bool(video_streams)
        has_audio = bool(audio_streams)

        can_w = 1920
        can_h = 1080
        can_fps = Fraction(24, 1)
        if has_video:
            ref_v = video_streams[0]
            raw_w = ref_v.codec_context.width or 1920
            raw_h = ref_v.codec_context.height or 1080
            can_w = (raw_w // 2) * 2
            can_h = (raw_h // 2) * 2
            fps_val = ref_v.average_rate or ref_v.guessed_rate or 24
            can_fps = Fraction(fps_val) if fps_val else Fraction(24, 1)

        can_sr = 44100
        can_layout = "stereo"
        if has_audio:
            ref_a = audio_streams[0]
            can_sr = ref_a.codec_context.sample_rate or 44100
            if ref_a.codec_context.layout and ref_a.codec_context.layout.name:
                can_layout = ref_a.codec_context.layout.name
            elif ref_a.codec_context.channels == 1:
                can_layout = "mono"
            elif ref_a.codec_context.channels == 2:
                can_layout = "stereo"

    container_options: dict[str, str] = {}
    if output_path.suffix.lower() in (".mp4", ".m4v", ".mov"):
        container_options["movflags"] = "faststart"

    with av.open(
        str(output_path),
        mode="w",
        options=container_options if container_options else None,
    ) as out_container:
        out_video = None
        if has_video:
            video_options: dict[str, str] = {"crf": "22", "preset": "veryfast"}
            if threads is not None:
                video_options["threads"] = str(threads)
            out_video = out_container.add_stream(
                "libx264",
                rate=can_fps,
                options=video_options,
            )
            out_video.width = can_w
            out_video.height = can_h
            out_video.pix_fmt = "yuv420p"

        out_audio = None
        if has_audio:
            out_audio = out_container.add_stream("aac", rate=can_sr)
            out_audio.layout = can_layout

        v_pts = 0
        a_pts = 0

        def emit_video_frame(f: av.VideoFrame) -> None:
            nonlocal v_pts
            rf = f.reformat(width=can_w, height=can_h, format="yuv420p")
            rf.pts = v_pts
            rf.time_base = Fraction(1, can_fps)
            v_pts += 1
            if out_video is not None:
                for enc_p in out_video.encode(rf):
                    out_container.mux(enc_p)

        def emit_audio_frame(f: av.AudioFrame) -> None:
            nonlocal a_pts
            f.pts = a_pts
            f.time_base = Fraction(1, can_sr)
            a_pts += f.samples
            if out_audio is not None:
                for enc_p in out_audio.encode(f):
                    out_container.mux(enc_p)

        for seg_p in segments:
            seg_v_start = v_pts
            seg_a_start = a_pts
            seg_had_video = False
            seg_had_audio = False

            resampler = None
            if out_audio is not None:
                resampler = av.AudioResampler(
                    format="fltp",
                    layout=can_layout,
                    rate=can_sr,
                )

            with av.open(str(seg_p)) as in_c:
                v_stream = in_c.streams.video[0] if in_c.streams.video else None
                a_stream = in_c.streams.audio[0] if in_c.streams.audio else None

                fps_graph = None
                if v_stream is not None and out_video is not None:
                    in_rate = v_stream.average_rate or v_stream.guessed_rate
                    if in_rate and abs(float(in_rate) - float(can_fps)) > 0.05:
                        fps_graph = av.filter.Graph()
                        buffer = fps_graph.add_buffer(template=v_stream)
                        fps_filter = fps_graph.add("fps", f"fps={float(can_fps)}")
                        sink = fps_graph.add("buffersink")
                        buffer.link_to(fps_filter)
                        fps_filter.link_to(sink)
                        fps_graph.configure()

                streams = [s for s in (v_stream, a_stream) if s is not None]
                for packet in in_c.demux(*streams):
                    for frame in packet.decode():
                        if isinstance(frame, av.VideoFrame) and out_video is not None:
                            seg_had_video = True
                            if fps_graph is not None:
                                fps_graph.push(frame)
                                while True:
                                    try:
                                        emit_video_frame(fps_graph.pull())
                                    except (av.BlockingIOError, av.EOFError) as _:
                                        break
                            else:
                                emit_video_frame(frame)

                        elif (
                            isinstance(frame, av.AudioFrame)
                            and out_audio is not None
                            and resampler is not None
                        ):
                            seg_had_audio = True
                            for r_frame in resampler.resample(frame):
                                emit_audio_frame(r_frame)

                if fps_graph is not None and out_video is not None:
                    fps_graph.push(None)
                    while True:
                        try:
                            emit_video_frame(fps_graph.pull())
                        except (av.BlockingIOError, av.EOFError) as _:
                            break

            if resampler is not None:
                for r_frame in resampler.resample(None):
                    seg_had_audio = True
                    emit_audio_frame(r_frame)

            # Pad silence if segment had no audio frames but audio stream is active
            if out_audio is not None and not seg_had_audio and v_pts > seg_v_start:
                seg_duration_s = (v_pts - seg_v_start) / float(can_fps)
                needed_samples = round(seg_duration_s * can_sr)
                num_channels = out_audio.codec_context.channels or (
                    2 if can_layout == "stereo" else 1
                )
                samples_left = needed_samples
                chunk_size = 1024
                while samples_left > 0:
                    cur_chunk = min(chunk_size, samples_left)
                    silence_arr = np.zeros((num_channels, cur_chunk), dtype=np.float32)
                    silence_frame = av.AudioFrame.from_ndarray(
                        silence_arr, format="fltp", layout=can_layout
                    )
                    silence_frame.sample_rate = can_sr
                    emit_audio_frame(silence_frame)
                    samples_left -= cur_chunk

            # Pad black frames if segment had no video frames but video stream is active
            if out_video is not None and not seg_had_video and a_pts > seg_a_start:
                seg_duration_s = (a_pts - seg_a_start) / float(can_sr)
                needed_frames = round(seg_duration_s * float(can_fps))
                if needed_frames > 0:
                    black_frame = av.VideoFrame.from_ndarray(
                        np.zeros((can_h, can_w, 3), dtype=np.uint8), format="rgb24"
                    ).reformat(format="yuv420p")
                    for _ in range(needed_frames):
                        emit_video_frame(black_frame)

        if out_video is not None:
            for enc_p in out_video.encode():
                out_container.mux(enc_p)

        if out_audio is not None:
            for enc_p in out_audio.encode():
                out_container.mux(enc_p)

    return str(output_path)


def concat(
    cut_path: Path | str,
    intro_path: Path | str | None = None,
    outro_path: Path | str | None = None,
    output_path: Path | str | None = None,
    *,
    backend: StorageBackend | None = None,
    storage: StorageBackend | None = None,
    force_reencode: bool = False,
    threads: int | None = None,
) -> str:
    """Concatenate talk media segments (cut recording with optional intro and outro).

    Persists the concatenated output via StorageBackend, producing the exact artifact
    consumed by subsequent transcoding.

    Args:
        cut_path: Path to the main trimmed talk recording.
        intro_path: Optional path to an intro title slate media file.
        outro_path: Optional path to an outro closing media file.
        output_path: Destination path or storage key for concatenated output. If omitted,
            defaults to `{cut_stem}_concat{cut_suffix}` in cut's directory. For
            cut-only calls without a custom output destination, returns `cut_path`
            unchanged instead.
        backend: Optional StorageBackend abstraction to persist output. If omitted,
            defaults to a LocalDiskBackend scoped to the output destination directory.
        storage: Alias for backend.
        force_reencode: If True, bypass stream-copy remuxing and force re-encoding.

    Returns:
        str: Absolute or relative string path or key of the concatenated output file.

    Raises:
        FileNotFoundError: If any specified input file does not exist.
        ValueError: If output path matches any input path, or invalid stream configurations.
    """
    storage_backend = backend or storage

    if threads is not None and threads <= 0:
        raise ValueError(f"threads must be positive, got {threads}")

    cut_p = Path(cut_path)
    if not cut_p.is_file():
        raise FileNotFoundError(f"Cut file not found: {cut_p}")

    intro_p: Path | None = None
    if intro_path is not None:
        intro_p = Path(intro_path)
        if not intro_p.is_file():
            raise FileNotFoundError(f"Intro file not found: {intro_p}")

    outro_p: Path | None = None
    if outro_path is not None:
        outro_p = Path(outro_path)
        if not outro_p.is_file():
            raise FileNotFoundError(f"Outro file not found: {outro_p}")

    # Instant zero-cost passthrough if only cut is provided without custom output destination
    if (
        intro_p is None
        and outro_p is None
        and (output_path is None or Path(output_path).resolve() == cut_p.resolve())
    ):
        return str(cut_p)

    if output_path is None:
        out_p = cut_p.parent / f"{cut_p.stem}_concat{cut_p.suffix}"
        output_key = out_p.name
    else:
        out_p = Path(output_path)
        output_key = str(output_path)

    input_paths = [p for p in (intro_p, cut_p, outro_p) if p is not None]
    if any(out_p.resolve() == p.resolve() for p in input_paths):
        raise ValueError(f"Output path matches input path: {out_p}")

    def _persist(src: Path) -> str:
        if storage_backend is not None:
            storage_backend.put(output_key, src)
            stored = storage_backend.get(output_key)
            return getattr(stored, "key", str(stored))
        LocalDiskBackend(out_p.parent).put(out_p.name, src)
        return str(out_p)

    if intro_p is None and outro_p is None:
        return _persist(cut_p)

    segments = [p for p in (intro_p, cut_p, outro_p) if p is not None]

    # Render into a temporary file, then persist via StorageBackend
    out_suffix = out_p.suffix or cut_p.suffix or ".mp4"
    scratch_dir = (
        getattr(storage_backend, "get_temp_dir", lambda: None)()
        if storage_backend
        else None
    )
    with tempfile.TemporaryDirectory(
        prefix="veditor-concat-", dir=scratch_dir
    ) as tmpdir:
        tmp_target = Path(tmpdir) / f"concat_tmp{out_suffix}"

        if not force_reencode and _can_stream_copy(segments):
            try:
                _concat_stream_copy(segments, tmp_target, canonical_cut_path=cut_p)
            except (av.FFmpegError, ValueError, RuntimeError, OSError) as exc:
                logger.warning(
                    "Stream-copy concat failed (%s); falling back to full re-encode.",
                    exc,
                )
                _concat_reencode(segments, cut_p, tmp_target, threads=threads)
        else:
            _concat_reencode(segments, cut_p, tmp_target, threads=threads)

        return _persist(tmp_target)
