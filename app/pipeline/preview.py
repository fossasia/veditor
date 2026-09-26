from pathlib import Path

import av

from app.config import PreviewPreset


def generate_preview(
    input_path: Path,
    output_path: Path,
    preset: PreviewPreset,
    threads: int | None = None,
) -> None:
    """
    Generate a low-resolution review clip from an input recording using PyAV.

    Applies the target resolution, video bitrate (or CRF), audio bitrate,
    and speed preset specified by the given PreviewPreset.
    """
    if threads is not None and threads <= 0:
        raise ValueError(f"threads must be greater than zero: {threads}")

    container_options = (
        {"movflags": "faststart"} if output_path.suffix.lower() == ".mp4" else {}
    )
    with (
        av.open(str(input_path)) as in_container,
        av.open(str(output_path), mode="w", options=container_options) as out_container,
    ):
        in_video = in_container.streams.video[0] if in_container.streams.video else None
        in_audio = in_container.streams.audio[0] if in_container.streams.audio else None

        if not in_video and not in_audio:
            raise ValueError(f"No video or audio streams found in {input_path}")

        out_video = None
        out_audio = None
        width, height = preset.resolution

        stride = 1
        if in_video:
            raw_fps = float(in_video.guessed_rate or in_video.average_rate or 24)
            stride = max(1, round(raw_fps / 24.0)) if raw_fps > 30.0 else 1
            fps = max(1, round(raw_fps / stride))
            speed_preset = getattr(preset, "preset_speed", "veryfast") or "veryfast"
            options = {"preset": speed_preset}
            if preset.crf is not None:
                options["crf"] = str(preset.crf)
            if threads is not None:
                options["threads"] = str(threads)
            out_video = out_container.add_stream("libx264", rate=fps, options=options)
            out_video.width = width
            out_video.height = height
            out_video.pix_fmt = "yuv420p"
            if preset.crf is None:
                out_video.bit_rate = preset.video_bitrate

        if in_audio:
            out_audio = out_container.add_stream("aac", rate=in_audio.rate or 44100)
            out_audio.bit_rate = preset.audio_bitrate
            out_audio.layout = in_audio.layout.name if in_audio.layout else "mono"

        streams = [s for s in (in_video, in_audio) if s is not None]
        video_frame_count = 0
        for packet in in_container.demux(*streams):
            for frame in packet.decode():
                if packet.stream.type == "video" and out_video:
                    skip = stride > 1 and (video_frame_count % stride != 0)
                    video_frame_count += 1
                    if skip:
                        continue
                    reformatted = frame.reformat(
                        width=width, height=height, format="yuv420p"
                    )
                    for out_pkt in out_video.encode(reformatted):
                        out_container.mux(out_pkt)
                elif packet.stream.type == "audio" and out_audio:
                    for out_pkt in out_audio.encode(frame):
                        out_container.mux(out_pkt)

        if out_video:
            for out_pkt in out_video.encode():
                out_container.mux(out_pkt)

        if out_audio:
            for out_pkt in out_audio.encode():
                out_container.mux(out_pkt)
