"""Background tasks and RQ job functions for VEditor pipeline.

This module houses stage wrapper functions dispatched via RQ.
Worker processes eagerly import this module at boot to avoid per-job import overhead.
"""

from __future__ import annotations

import logging
import tempfile
import time
import traceback
from pathlib import Path

from app.config import PREVIEW_PRESETS, settings
from app.db import SessionLocal
from app.models import Job, Talk
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
from app.storage import get_storage_backend

logger = logging.getLogger(__name__)

# ponytail: timeouts are generous defaults; tune per deployment if jobs time out in production
STAGE_CONFIG: dict[str, dict[str, str | int]] = {
    "detect": {"queue": "light", "job_timeout": 300},
    "cut": {"queue": "light", "job_timeout": 900},
    "intro": {"queue": "light", "job_timeout": 300},
    "outro": {"queue": "light", "job_timeout": 300},
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
        talk = db.get(Talk, talk_id)
        if talk and talk.status not in (
            "waiting_for_files",
            "broken",
            "done",
            "rejected",
        ):
            advance(talk, "broken")
        db.commit()


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
            job = Job(talk_id=talk_id, kind="detect", status="running")
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
                return
            talk.raw_duration_seconds = result.actual_duration_seconds
            advance(talk, "pending_approval")
            job.status = "done"
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
            job = Job(talk_id=talk_id, kind="cut", status="running")
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
                return
            advance(talk, "generating_previews")
            job.status = "done"
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


def job_intro(talk_id: int, intro_key: str | None = None) -> None:
    intro_key = intro_key or f"{talk_id}/intro/intro.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(talk_id=talk_id, kind="intro", status="running")
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
            if not talk or not job:
                logger.info(
                    "Talk %s intro job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                return
            job.status = "done"
            db.commit()
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_outro(talk_id: int, outro_key: str | None = None) -> None:
    outro_key = outro_key or f"{talk_id}/outro/outro.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(talk_id=talk_id, kind="outro", status="running")
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
            if not talk or not job:
                logger.info(
                    "Talk %s outro job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                return
            job.status = "done"
            db.commit()
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
            job = Job(talk_id=talk_id, kind="preview", status="running")
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
                return
            advance(talk, "preview")
            job.status = "done"
            db.commit()
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise


def job_loudness(talk_id: int, cut_key: str, loud_key: str | None = None) -> None:
    loud_key = loud_key or f"{talk_id}/cut/cut_loud.mp4"
    job_id = None
    storage = get_storage_backend()
    try:
        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            if not talk:
                raise ValueError(f"Talk {talk_id} not found")
            job = Job(talk_id=talk_id, kind="loudness", status="running")
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id

        cut_path = storage.get(cut_key)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_out = Path(tmpdir) / "loudness.mp4"
            normalize(cut_path, tmp_out)
            storage.put(loud_key, tmp_out)

        with SessionLocal() as db:
            talk = db.get(Talk, talk_id)
            job = db.get(Job, job_id)
            if not talk or not job or talk.status != "transcoding":
                logger.info(
                    "Talk %s loudness job %s was aborted or state changed; discarding",
                    talk_id,
                    job_id,
                )
                return
            job.status = "done"
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
                return
            advance(talk, "uploading")
            job.status = "done"
            job.progress_pct = 100.0
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
            job = Job(talk_id=talk_id, kind="publish", status="running")
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
                return
            advance(talk, "done")
            job.status = "done"
            db.commit()
    except Exception as exc:
        _handle_failure(talk_id, job_id, exc, storage)
        raise
