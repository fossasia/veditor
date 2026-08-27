"""Interactive REST API endpoints for testing and running VEditor pipeline modules."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.config import PREVIEW_PRESETS
from app.pipeline.cut import cut
from app.pipeline.detect import detect
from app.pipeline.intro import generate_intro_clip, generate_outro_clip
from app.pipeline.loudness import normalize
from app.pipeline.preview import generate_preview
from app.pipeline.transcode import (
    PRESET_4K_MASTER,
    PRESET_720P,
    PRESET_1080P_DEFAULT,
    transcode,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

OUTPUT_DIR = Path("data/1/cut")
# storage-boundary-exempt: local preview directory initialization
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class DetectRequest(BaseModel):
    input_path: str = "data/1/raw/sample-3.mp4"
    scheduled_duration_seconds: float = 20.0


class CutRequest(BaseModel):
    input_path: str = "data/1/raw/sample-3.mp4"
    start_seconds: float = 2.0
    end_seconds: float = 10.0
    force_reencode: bool = False
    output_filename: str = "sample-cut.mp4"


class LoudnessRequest(BaseModel):
    input_path: str = "data/1/cut/sample-intro.mp4"
    target_lufs: float = -16.0
    output_filename: str = "sample-normalized.mp4"


class IntroRequest(BaseModel):
    title: str = "Open Source Video Editing & Transcoding Pipeline"
    speakers: list[str] = Field(default_factory=lambda: ["Saalim", "ViRUS-0-0"])
    event_name: str = "FOSSASIA Summit 2026"
    room_date: str = "Hall 1 • Main Stage"
    duration_seconds: float = 4.0
    output_filename: str = "sample-intro.mp4"


class OutroRequest(BaseModel):
    event_name: str = "FOSSASIA Summit 2026"
    thank_you_text: str = "Thank You For Watching!"
    website_or_links: str = "Watch more talks at eventyay.com • fossasia.org"
    duration_seconds: float = 3.5
    output_filename: str = "sample-outro.mp4"


class PreviewRequest(BaseModel):
    input_path: str = "data/1/raw/sample-3.mp4"
    preset_name: str = "small_video"
    output_filename: str = "sample-preview.mp4"


class TranscodeRequest(BaseModel):
    input_path: str = "data/1/raw/sample-3.mp4"
    preset_name: str = "1080p_default"
    output_filename: str = "sample-transcoded.mp4"


class FullPipelineRequest(BaseModel):
    input_path: str = "data/1/raw/sample-3.mp4"
    start_seconds: float = 2.0
    end_seconds: float = 8.0
    target_lufs: float = -16.0
    include_intro: bool = True
    intro_title: str = "Open Source Video Editing & Transcoding Pipeline"
    intro_speakers: list[str] = Field(default_factory=lambda: ["Saalim", "ViRUS-0-0"])
    intro_event: str = "FOSSASIA Summit 2026"
    intro_room: str = "Hall 1 • Main Stage"
    include_outro: bool = True
    outro_text: str = "Thank You For Watching!"
    outro_links: str = "Watch more talks at eventyay.com • fossasia.org"
    preset_name: str = "1080p_default"


def _concat_clips(
    clip_paths: list[Path],
    output_path: Path,
    fps: int = 25,
    resolution: tuple[int, int] = (1920, 1080),
) -> None:
    """Concatenates video & audio clips in order into a unified MP4."""
    with av.open(str(output_path), mode="w", format="mp4") as out_c:
        out_v = out_c.add_stream(
            "libx264", rate=fps, options={"crf": "22", "preset": "veryfast"}
        )
        out_v.width = resolution[0]
        out_v.height = resolution[1]
        out_v.pix_fmt = "yuv420p"

        sample_rate = 44100
        out_a = out_c.add_stream("aac", rate=sample_rate)
        out_a.layout = "stereo"

        video_pts = 0
        audio_pts = 0

        for clip_path in clip_paths:
            if not clip_path.is_file():
                continue
            with av.open(str(clip_path)) as in_c:
                if in_c.streams.video:
                    for frame in in_c.decode(in_c.streams.video[0]):
                        img = frame.to_image().resize(resolution)
                        v_frame = av.VideoFrame.from_image(img)
                        v_frame.pts = video_pts
                        v_frame.time_base = Fraction(1, fps)
                        video_pts += 1
                        for packet in out_v.encode(v_frame):
                            out_c.mux(packet)

                if in_c.streams.audio:
                    for frame in in_c.decode(in_c.streams.audio[0]):
                        frame.pts = audio_pts
                        frame.time_base = Fraction(1, sample_rate)
                        audio_pts += frame.samples
                        for packet in out_a.encode(frame):
                            out_c.mux(packet)

        for packet in out_v.encode():
            out_c.mux(packet)
        for packet in out_a.encode():
            out_c.mux(packet)


@router.post("/run-full-pipeline")
def run_full_pipeline(req: FullPipelineRequest) -> dict[str, Any]:
    """Orchestrates the complete pipeline:

    [Intro Slate] + [Cut & Normalized Talk Video] + [Outro Slate] -> Final Master -> Preview
    """
    in_file = Path(req.input_path)
    if not in_file.is_file():
        raise HTTPException(
            status_code=404, detail=f"Input file not found: {req.input_path}"
        )

    stage_clips: list[Path] = []

    # 1. Generate Intro Clip
    if req.include_intro:
        intro_file = OUTPUT_DIR / "pipeline_stage_intro.mp4"
        generate_intro_clip(
            output_path=intro_file,
            title=req.intro_title,
            speakers=req.intro_speakers,
            event_name=req.intro_event,
            room_date=req.intro_room,
            duration_seconds=3.5,
            resolution=(1920, 1080),
            fps=25,
        )
        stage_clips.append(intro_file)

    # 2. Cut talk video segment
    cut_file = OUTPUT_DIR / "pipeline_stage_cut.mp4"
    cut(
        input_path=in_file,
        output_path=cut_file,
        start_seconds=req.start_seconds,
        end_seconds=req.end_seconds,
        force_reencode=True,
    )
    stage_clips.append(cut_file)

    # 3. Generate Outro Clip
    if req.include_outro:
        outro_file = OUTPUT_DIR / "pipeline_stage_outro.mp4"
        generate_outro_clip(
            output_path=outro_file,
            event_name=req.intro_event,
            thank_you_text=req.outro_text,
            website_or_links=req.outro_links,
            duration_seconds=3.0,
            resolution=(1920, 1080),
            fps=25,
        )
        stage_clips.append(outro_file)

    # 4. Stitch Assembly
    stitched_file = OUTPUT_DIR / "pipeline_stage_stitched.mp4"
    _concat_clips(stage_clips, stitched_file, fps=25, resolution=(1920, 1080))

    # 5. Final Master Transcode
    final_master_file = OUTPUT_DIR / "final_talk_presentation.mp4"
    presets = {
        "1080p_default": PRESET_1080P_DEFAULT,
        "720p": PRESET_720P,
        "4k_master": PRESET_4K_MASTER,
    }
    transcode(
        input_path=stitched_file,
        output_path=final_master_file,
        preset=presets.get(req.preset_name, PRESET_1080P_DEFAULT),
    )

    # 6. Generate Fast Preview Proxy
    preview_file = OUTPUT_DIR / "final_talk_preview.mp4"
    preset_obj = PREVIEW_PRESETS.get(
        "small_video", next(iter(PREVIEW_PRESETS.values()))
    )
    generate_preview(
        input_path=final_master_file,
        output_path=preview_file,
        preset=preset_obj,
    )

    return {
        "status": "success",
        "message": "Full end-to-end pipeline execution complete!",
        "master_url": f"/{final_master_file.as_posix()}",
        "preview_url": f"/{preview_file.as_posix()}",
        "assembled_stages": len(stage_clips),
    }


@router.post("/detect")
def run_detect(req: DetectRequest) -> dict[str, Any]:
    in_file = Path(req.input_path)
    if not in_file.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.input_path}")

    try:
        now = datetime.now(UTC)
        res = detect(
            file_path=in_file,
            scheduled_start=now,
            scheduled_end=now + timedelta(seconds=req.scheduled_duration_seconds),
            tolerance_seconds=15.0,
        )
        return {
            "status": "success",
            "passed": res.passed,
            "actual_duration_seconds": res.actual_duration_seconds,
            "has_video": res.has_video,
            "has_audio": res.has_audio,
            "reason": res.reason,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/cut")
def run_cut(req: CutRequest) -> dict[str, Any]:
    in_file = Path(req.input_path)
    if not in_file.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.input_path}")

    out_file = OUTPUT_DIR / req.output_filename
    try:
        strategy = cut(
            input_path=in_file,
            output_path=out_file,
            start_seconds=req.start_seconds,
            end_seconds=req.end_seconds,
            force_reencode=req.force_reencode,
        )
        return {
            "status": "success",
            "strategy_used": strategy,
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/loudness")
def run_loudness(req: LoudnessRequest) -> dict[str, Any]:
    in_file = Path(req.input_path)
    if not in_file.is_file():
        intro_file = OUTPUT_DIR / "sample-intro.mp4"
        if intro_file.is_file():
            in_file = intro_file
        else:
            raise HTTPException(
                status_code=404, detail=f"File not found: {req.input_path}"
            )

    out_file = OUTPUT_DIR / req.output_filename
    try:
        normalize(
            input_path=in_file,
            output_path=out_file,
            target_lufs=req.target_lufs,
        )
        return {
            "status": "success",
            "target_lufs": req.target_lufs,
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/intro")
def run_intro(req: IntroRequest) -> dict[str, Any]:
    out_file = OUTPUT_DIR / req.output_filename
    try:
        generate_intro_clip(
            output_path=out_file,
            title=req.title,
            speakers=req.speakers,
            event_name=req.event_name,
            room_date=req.room_date,
            duration_seconds=req.duration_seconds,
            resolution=(1920, 1080),
            fps=25,
        )
        return {
            "status": "success",
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/outro")
def run_outro(req: OutroRequest) -> dict[str, Any]:
    out_file = OUTPUT_DIR / req.output_filename
    try:
        generate_outro_clip(
            output_path=out_file,
            event_name=req.event_name,
            thank_you_text=req.thank_you_text,
            website_or_links=req.website_or_links,
            duration_seconds=req.duration_seconds,
            resolution=(1920, 1080),
            fps=25,
        )
        return {
            "status": "success",
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/preview")
def run_preview(req: PreviewRequest) -> dict[str, Any]:
    in_file = Path(req.input_path)
    if not in_file.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.input_path}")

    out_file = OUTPUT_DIR / req.output_filename
    default_preset = next(iter(PREVIEW_PRESETS.values()))
    preset_obj = PREVIEW_PRESETS.get(
        req.preset_name, PREVIEW_PRESETS.get("small_video", default_preset)
    )
    try:
        generate_preview(
            input_path=in_file,
            output_path=out_file,
            preset=preset_obj,
        )
        return {
            "status": "success",
            "preset": req.preset_name,
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/transcode")
def run_transcode(req: TranscodeRequest) -> dict[str, Any]:
    in_file = Path(req.input_path)
    if not in_file.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {req.input_path}")

    out_file = OUTPUT_DIR / req.output_filename

    presets = {
        "1080p_default": PRESET_1080P_DEFAULT,
        "720p": PRESET_720P,
        "4k_master": PRESET_4K_MASTER,
    }
    selected_preset = presets.get(req.preset_name, PRESET_1080P_DEFAULT)

    try:
        transcode(
            input_path=in_file,
            output_path=out_file,
            preset=selected_preset,
        )
        return {
            "status": "success",
            "preset": req.preset_name,
            "output_path": f"/{out_file.as_posix()}",
            "filename": req.output_filename,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
