"""Audio loudness normalization module for VEditor pipeline.

Normalizes audio loudness to EBU R128 / BS.1770 standards using PyAV's
native `av.filter.Graph` API wrapping libavfilter's `loudnorm` filter,
rather than shelling out to an external ffmpeg binary.

Implementation Note:
-------------------
PyAV's `av.filter.Graph` API directly supports libavfilter's `loudnorm`
filter. This implementation uses a single-pass `loudnorm` filter configured
with the desired integrated loudness target (`I`), true peak (`TP=-1.5`),
and loudness range (`LRA=11`). Video streams (if present) are preserved
without decoding via lossless stream-copy remuxing in a single interleaved pass.
"""

from __future__ import annotations

import logging
import math
from fractions import Fraction
from pathlib import Path

import av

logger = logging.getLogger(__name__)

DEFAULT_TARGET_LUFS = -16.0
DEFAULT_TRUE_PEAK = -1.5
DEFAULT_LRA = 11.0


def normalize(
    input_path: Path | str,
    output_path: Path | str,
    target_lufs: float = DEFAULT_TARGET_LUFS,
) -> None:
    """Normalize the audio loudness of a media file to target LUFS.

    Args:
        input_path: Path to the source recording.
        output_path: Destination path for the normalized output file.
        target_lufs: Target integrated loudness in LUFS (default: -16.0).

    Raises:
        FileNotFoundError: If input_path does not exist.
        ValueError: If target_lufs is non-finite or out of range, or if no audio stream exists.
    """
    in_path = Path(input_path)
    out_path = Path(output_path)

    if not in_path.is_file():
        raise FileNotFoundError(f"Input file not found: {in_path}")

    if not math.isfinite(target_lufs) or target_lufs > 0 or target_lufs < -70.0:
        raise ValueError(
            f"target_lufs must be between -70.0 and 0.0, got: {target_lufs}"
        )

    # storage-boundary-exempt: creating parent directory for pipeline output
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(in_path)) as in_container:
        video_streams = list(in_container.streams.video)
        audio_streams = list(in_container.streams.audio)

        if not audio_streams:
            raise ValueError(f"No audio stream found in {input_path}")

        in_a = audio_streams[0]
        sample_rate = in_a.codec_context.sample_rate or 44100
        channels = in_a.codec_context.channels or 1

        # Determine audio channel layout with fallback based on channel count
        if in_a.codec_context.layout:
            layout_name = in_a.codec_context.layout.name
        elif channels == 2:
            layout_name = "stereo"
        else:
            layout_name = "mono"

        # Construct audio filter graph: abuffer -> loudnorm -> abuffersink
        graph = av.filter.Graph()
        buf_node = graph.add_abuffer(template=in_a)
        loudnorm_node = graph.add(
            "loudnorm",
            f"I={target_lufs}:TP={DEFAULT_TRUE_PEAK}:LRA={DEFAULT_LRA}",
        )
        sink_node = graph.add("abuffersink")

        buf_node.link_to(loudnorm_node)
        loudnorm_node.link_to(sink_node)
        graph.configure()

        with av.open(str(out_path), mode="w") as out_container:
            # 1. Video stream copy (if present)
            out_video = None
            if video_streams:
                out_video = out_container.add_stream_from_template(video_streams[0])

            # 2. Configure normalized audio stream
            out_audio = out_container.add_stream("aac", rate=sample_rate)
            out_audio.layout = layout_name

            # Streams to demux in a single interleaved pass
            streams_to_demux = [
                s for s in (video_streams[:1] + audio_streams[:1]) if s is not None
            ]

            audio_sample_count = 0

            # Single interleaved demuxing pass without seeking
            for packet in in_container.demux(*streams_to_demux):
                if packet.stream.type == "video" and out_video is not None:
                    if packet.dts is None:
                        continue
                    packet.stream = out_video
                    out_container.mux(packet)

                elif packet.stream.type == "audio":
                    for frame in packet.decode():
                        graph.push(frame)
                        while True:
                            try:
                                out_frame = graph.pull()
                            except av.FFmpegError, EOFError:
                                break

                            out_frame.pts = audio_sample_count
                            out_frame.time_base = Fraction(1, sample_rate)
                            audio_sample_count += out_frame.samples
                            for enc_packet in out_audio.encode(out_frame):
                                out_container.mux(enc_packet)

            # Flush filter graph
            graph.push(None)
            while True:
                try:
                    out_frame = graph.pull()
                except av.FFmpegError, EOFError:
                    break

                out_frame.pts = audio_sample_count
                out_frame.time_base = Fraction(1, sample_rate)
                audio_sample_count += out_frame.samples
                for enc_packet in out_audio.encode(out_frame):
                    out_container.mux(enc_packet)

            # Flush audio encoder
            for enc_packet in out_audio.encode():
                out_container.mux(enc_packet)
