"""Background tasks and RQ job functions for VEditor pipeline.

This module houses stage wrapper functions dispatched via RQ.
Worker processes eagerly import this module at boot to avoid per-job import overhead.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from app.config import PREVIEW_PRESETS, settings
from app.db import SessionLocal
from app.ingest import validate_media_file
from app.models import Job, Talk
from app.pipeline.concat import concat
from app.pipeline.cut import cut
from app.pipeline.detect import detect
from app.pipeline.intro import generate_intro_clip
from app.pipeline.loudness import normalize
from app.pipeline.outro import generate_outro_clip
from app.pipeline.preview import generate_preview
from app.pipeline.publish import publish
from app.pipeline.transcode import transcode
from app.queue import heavy_queue, light_queue
from app.states import advance
from app.storage import cleanup_intermediates, get_storage_backend

logger = logging.getLogger(__name__)

# ponytail: timeouts are generous defaults; tune per deployment if jobs time out in production
STAGE_CONFIG: dict[str, dict[str, str | int]] = {
    "ingest": {"queue": "light", "job_timeout": 600},
    "detect": {"queue": "light", "job_timeout": 300},
    "cut": {"queue": "light", "job_timeout": 900},
    "intro": {"queue": "light", "job_timeout": 300},
    "outro": {"queue": "light", "job_timeout": 300},
    "concat": {"queue": "light", "job_timeout": 1800},
    "preview": {"queue": "light", "job_timeout": 1800},
    "loudness": {"queue": "light", "job_timeout": 900},
    "transcode": {"queue": "heavy", "job_timeout": 14400},
    "publish": {"queue": "light", "job_timeout": 300},
}


def _handle_failure(talk_id: int, job_id: int | None, exc: Exception, storage) -> None:
    log_text = traceback.format_exc()
    log_key = f"{talk_id}/logs/job_{job_id if job_id is not None else 'unknown'}.log"
    try:
        storage.put(log_key, log_text.encode("utf-8"))
    except Exception as log_err:  # noqa: BLE001
        logger.warning("Failed to persist job log to storage: %s", log_err)

    with SessionLocal() as db:
        if job_id is not None:
            job = db.get(Job, job_id)
            if not job:
                return
            job.status = "failed"
            job.log_path = log_key
            job.updated_at = datetime.now(UTC)
        talk = db.get(Talk, talk_id)
        if talk and talk.status not in (
            "waiting_for_files",
            "broken",
            "done",
            "rejected",
        ):
            advance(talk, "broken")
        db.commit()


def job_ingest(talk_id: int, staged_path: str, raw_key: str | None = None) -> None:
    raw_key = raw_key or f"{talk_id}/raw/raw.mp4"
    job_id = None
    storage = get_storage_backend()
    staged = Path(staged_path)
    claimed = False
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk or talk.status != "waiting_for_files":
                logger.info(
                    "Talk %s ingest job was aborted or state changed prior to start; discarding",
                    talk_id,
                )
                return

            # Atomically claim the talk by transitioning from waiting_for_files to detecting
            updated = (
                db.query(Talk)
                .filter(Talk.id == talk_id, Talk.status == "waiting_for_files")
                .update({"status": "detecting"})
            )
            if not updated:
                logger.info(
                    "Talk %s was already claimed by another ingest job; discarding",
                    talk_id,
                )
                return

            job = Job(
                talk_id=talk_id,
                kind="ingest",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            claimed = True

        if not staged.is_file():
            raise FileNotFoundError(f"Staged upload file not found: {staged_path}")

        # In-worker PyAV container & stream inspection
        validate_media_file(staged)

        # Persist to destination storage backend
        storage.put(raw_key, staged)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "detecting":
                logger.info(
                    "Talk %s ingest job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return

            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        light_queue.enqueue(
            job_detect,
            talk_id,
            raw_key,
            job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
        )
    except Exception as exc:
        if claimed:
            with SessionLocal() as db:
                talk = db.get(Talk, talk_id)
                if talk and talk.status == "detecting":
                    talk.status = "waiting_for_files"
                    db.commit()
            _handle_failure(talk_id, job_id, exc, storage)
        raise
    finally:
        # storage-boundary-exempt: upload staging cleanup
        staged.unlink(missing_ok=True)


def job_detect(talk_id: int, raw_key: str) -> None:
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk or talk.status != "detecting":
                logger.info(
                    "Talk %s detect job was aborted or state changed prior to start; discarding",
                    talk_id,
                )
                return
            job = Job(
                talk_id=talk_id,
                kind="detect",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            scheduled_start = talk.start
            scheduled_end = talk.end

        raw_path = storage.get(raw_key)
        result = detect(
            raw_path,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
        )
        if not result.passed:
            raise ValueError(f"Detection failed: {result.reason}")

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "detecting":
                logger.info(
                    "Talk %s detect job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            talk.raw_duration_seconds = result.actual_duration_seconds
            advance(talk, "pending_approval")
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_cut(talk_id: int, raw_key: str, cut_key: str | None = None) -> None:
    cut_key = cut_key or f"{talk_id}/cut/cut.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            if talk.cut_start is None or talk.cut_end is None:
                raise ValueError(f"Talk {talk_id} has no cut bounds set")
            job = Job(
                talk_id=talk_id,
                kind="cut",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            start_seconds = talk.cut_start
            end_seconds = talk.cut_end

        raw_path = storage.get(raw_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "cut.mp4"
            cut(raw_path, tmp_out, start_seconds, end_seconds)
            storage.put(cut_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "cutting":
                logger.info(
                    "Talk %s cut job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            advance(talk, "generating_previews")
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        preview_key = f"{talk_id}/preview/preview.mp4"
        light_queue.enqueue(
            job_preview,
            talk_id,
            cut_key,
            preview_key,
            job_timeout=STAGE_CONFIG["preview"]["job_timeout"],
        )
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def dispatch_assembly(talk_id: int, cut_key: str) -> None:
    """Dispatches the next background stage in the assembly workflow."""
    with SessionLocal() as db:
        talk = db.get(Talk, talk_id)
        if not talk or talk.status not in ("assembling", "generating_previews"):
            return

        # 1. Check if generated intro is required and not yet completed
        intro_needed = talk.include_intro and talk.intro_source == "generated"
        intro_done = (
            db.query(Job)
            .filter(Job.talk_id == talk_id, Job.kind == "intro", Job.status == "done")
            .first()
            is not None
        )
        if intro_needed and not intro_done:
            light_queue.enqueue(
                job_intro,
                talk_id,
                cut_key,
                f"{talk_id}/intro/intro.mp4",
                job_timeout=STAGE_CONFIG["intro"]["job_timeout"],
            )
            return

        # 2. Check if generated outro is required and not yet completed
        outro_needed = talk.include_outro and talk.outro_source == "generated"
        outro_done = (
            db.query(Job)
            .filter(Job.talk_id == talk_id, Job.kind == "outro", Job.status == "done")
            .first()
            is not None
        )
        if outro_needed and not outro_done:
            light_queue.enqueue(
                job_outro,
                talk_id,
                cut_key,
                f"{talk_id}/outro/outro.mp4",
                job_timeout=STAGE_CONFIG["outro"]["job_timeout"],
            )
            return

        # 3. All input slates are ready (or skipped) -> enqueue concat
        intro_key = f"{talk_id}/intro/intro.mp4" if talk.include_intro else None
        outro_key = f"{talk_id}/outro/outro.mp4" if talk.include_outro else None
        concat_key = f"{talk_id}/assemble/assemble.mp4"
        light_queue.enqueue(
            job_concat,
            talk_id,
            cut_key,
            intro_key,
            outro_key,
            concat_key,
            job_timeout=STAGE_CONFIG["concat"]["job_timeout"],
        )


def job_intro(
    talk_id: int,
    cut_key: str | None = None,
    intro_key: str | None = None,
) -> None:
    if cut_key and "/intro/" in cut_key and intro_key is None:
        intro_key = cut_key
        cut_key = None
    cut_key = cut_key or f"{talk_id}/cut/cut.mp4"
    intro_key = intro_key or f"{talk_id}/intro/intro.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="intro",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            title = talk.title
            event_name = talk.event.name if talk.event else ""
            if talk.room and talk.start:
                room_date = f"{talk.room} • {talk.start.strftime('%Y-%m-%d')}"
            elif talk.start:
                room_date = talk.start.strftime("%Y-%m-%d")
            elif talk.room:
                room_date = talk.room
            else:
                room_date = ""

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "intro.mp4"
            generate_intro_clip(
                tmp_out,
                title=title,
                event_name=event_name,
                room_date=room_date,
            )
            storage.put(intro_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if (
                not talk
                or not job
                or talk.status not in ("assembling", "generating_previews")
            ):
                logger.info(
                    "Talk %s intro job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            talk_status = talk.status
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        if talk_status == "assembling":
            dispatch_assembly(talk_id, cut_key)
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_outro(
    talk_id: int,
    cut_key: str | None = None,
    outro_key: str | None = None,
) -> None:
    if cut_key and "/outro/" in cut_key and outro_key is None:
        outro_key = cut_key
        cut_key = None
    cut_key = cut_key or f"{talk_id}/cut/cut.mp4"
    outro_key = outro_key or f"{talk_id}/outro/outro.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="outro",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
            event_name = talk.event.name if talk.event else ""

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "outro.mp4"
            generate_outro_clip(
                tmp_out,
                event_name=event_name,
            )
            storage.put(outro_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if (
                not talk
                or not job
                or talk.status not in ("assembling", "generating_previews")
            ):
                logger.info(
                    "Talk %s outro job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            talk_status = talk.status
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        if talk_status == "assembling":
            dispatch_assembly(talk_id, cut_key)
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_concat(
    talk_id: int,
    cut_key: str,
    intro_key: str | None = None,
    outro_key: str | None = None,
    concat_key: str | None = None,
) -> None:
    concat_key = concat_key or f"{talk_id}/assemble/assemble.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="concat",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        cut_path = storage.get(cut_key)
        intro_path = storage.get(intro_key) if intro_key else None
        outro_path = storage.get(outro_key) if outro_key else None

        concat(
            cut_path=cut_path,
            intro_path=intro_path,
            outro_path=outro_path,
            output_path=concat_key,
            backend=storage,
        )

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "assembling":
                logger.info(
                    "Talk %s concat job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        loud_key = f"{talk_id}/assemble/assemble_loud.mp4"
        light_queue.enqueue(
            job_loudness,
            talk_id,
            concat_key,
            loud_key,
            job_timeout=STAGE_CONFIG["loudness"]["job_timeout"],
        )
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_preview(talk_id: int, cut_key: str, preview_key: str | None = None) -> None:
    preview_key = preview_key or f"{talk_id}/preview/preview.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="preview",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        cut_path = storage.get(cut_key)
        preset = (
            settings.preview_presets.get("small_video")
            or PREVIEW_PRESETS["small_video"]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "preview.mp4"
            generate_preview(cut_path, tmp_out, preset=preset)
            storage.put(preview_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "generating_previews":
                logger.info(
                    "Talk %s preview job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            advance(talk, "preview")
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_loudness(talk_id: int, cut_key: str, loud_key: str | None = None) -> None:
    if loud_key is None:
        loud_key = (
            f"{talk_id}/cut/cut_loud.mp4"
            if "/cut/" in cut_key
            else f"{talk_id}/assemble/assemble_loud.mp4"
        )
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            if talk.status != "assembling":
                logger.info(
                    "Talk %s loudness job: talk status %s != assembling; discarding",
                    talk_id,
                    talk.status,
                )
                return
            job = Job(
                talk_id=talk_id,
                kind="loudness",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        cut_path = storage.get(cut_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "loudness.mp4"
            try:
                normalize(cut_path, tmp_out)
            except ValueError as val_err:
                if "No audio stream found" in str(val_err):
                    logger.warning(
                        "Talk %s has no audio stream; synthesizing silent audio track",
                        talk_id,
                    )
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(cut_path),
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=channel_layout=stereo:sample_rate=44100",
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-shortest",
                        str(tmp_out),
                    ]
                    # storage-boundary-exempt: silent audio track synthesis
                    res = subprocess.run(
                        cmd, capture_output=True, text=True, check=False
                    )
                    if res.returncode != 0:
                        logger.warning(
                            "ffmpeg silent audio synthesis failed (%s); falling back to direct copy: %s",
                            res.returncode,
                            res.stderr,
                        )
                        # storage-boundary-exempt: fallback copy on silent audio synthesis failure
                        shutil.copy2(cut_path, tmp_out)
                else:
                    raise
            storage.put(loud_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "assembling":
                logger.info(
                    "Talk %s loudness job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            advance(talk, "transcoding")
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        final_key = f"{talk_id}/final/final.mp4"
        heavy_queue.enqueue(
            job_transcode,
            talk_id,
            loud_key,
            final_key,
            job_timeout=STAGE_CONFIG["transcode"]["job_timeout"],
        )
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_transcode(
    talk_id: int,
    loud_key: str,
    final_key: str | None = None,
    progress_throttle_s: float = 5.0,
) -> None:
    final_key = final_key or f"{talk_id}/final/final.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="transcode",
                status="running",
                progress_pct=None,
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        loud_path = storage.get(loud_key)
        last_update_time = [0.0]

        def _on_progress(pct: float) -> None:
            now = time.monotonic()
            if 0.0 <= pct < 1.0 and (now - last_update_time[0]) >= progress_throttle_s:
                last_update_time[0] = now
                try:
                    with SessionLocal() as progress_db:
                        j = progress_db.get(Job, job_id)
                        if j and j.status == "running":
                            j.progress_pct = round(pct * 100.0, 2)
                            j.updated_at = datetime.now(UTC)
                            progress_db.commit()
                except Exception as progress_err:  # noqa: BLE001
                    logger.warning(
                        "Failed to update transcode progress in DB: %s",
                        progress_err,
                    )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "final.mp4"
            transcode(loud_path, tmp_out, on_progress=_on_progress)
            storage.put(final_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "transcoding":
                logger.info(
                    "Talk %s transcode job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            advance(talk, "uploading")
            job.status = "done"
            job.progress_pct = 100.0
            job.updated_at = datetime.now(UTC)
            db.commit()

        light_queue.enqueue(
            job_publish,
            talk_id,
            final_key,
            job_timeout=STAGE_CONFIG["publish"]["job_timeout"],
        )
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_publish(talk_id: int, final_key: str) -> None:
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(
                talk_id=talk_id,
                kind="publish",
                status="running",
                started_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        final_path = storage.get(final_key)
        publish(final_path, talk_id=talk_id, backend=storage)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "uploading":
                logger.info(
                    "Talk %s publish job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                if job:
                    job.status = "cancelled"
                    job.updated_at = datetime.now(UTC)
                    db.commit()
                return
            advance(talk, "done")
            job.status = "done"
            job.updated_at = datetime.now(UTC)
            db.commit()

        cleanup_intermediates(storage, talk_id)
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise
