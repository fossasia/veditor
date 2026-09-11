import json
import logging
import math
import tempfile
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import av
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from rq.command import send_stop_job_command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client, verify_event_access
from app.config import settings
from app.db import get_db
from app.ingest import (
    IngestPathRejectedError,
    InsufficientStorageError,
    stage_custom_clip,
    stage_recording,
)
from app.queue import heavy_queue, light_queue
from app.states import advance
from app.storage import StorageBackend, cleanup_intermediates, get_storage_backend
from app.tasks import STAGE_CONFIG, dispatch_assembly, job_cut, job_detect

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/talks",
    tags=["talks"],
    dependencies=[Depends(get_client)],
)


@router.post("", response_model=schemas.TalkRead, status_code=status.HTTP_201_CREATED)
def create_or_update_talk(
    payload: schemas.TalkCreate,
    response: Response,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Creates or updates a Talk row scoped to the caller's authorized event.
    Idempotent on the natural key (event_id, title, start).
    Returns 201 Created on insert, 200 OK on update (preserving existing talk status).
    """
    verify_event_access(payload.event_id, client)

    talk = (
        db.query(models.Talk)
        .filter(
            models.Talk.event_id == payload.event_id,
            models.Talk.title == payload.title,
            models.Talk.start == payload.start,
        )
        .first()
    )

    if talk:
        talk.room = payload.room
        talk.end = payload.end
        db.commit()
        db.refresh(talk)
        response.status_code = status.HTTP_200_OK
        return talk

    talk = models.Talk(
        event_id=payload.event_id,
        title=payload.title,
        room=payload.room,
        start=payload.start,
        end=payload.end,
        status="waiting_for_files",
    )
    db.add(talk)
    try:
        db.commit()
        db.refresh(talk)
        response.status_code = status.HTTP_201_CREATED
        return talk
    except IntegrityError:
        db.rollback()
        talk = (
            db.query(models.Talk)
            .filter(
                models.Talk.event_id == payload.event_id,
                models.Talk.title == payload.title,
                models.Talk.start == payload.start,
            )
            .first()
        )
        if not talk:
            raise
        talk.room = payload.room
        talk.end = payload.end
        db.commit()
        db.refresh(talk)
        response.status_code = status.HTTP_200_OK
        return talk


@router.get("/{talk_id}", response_model=schemas.TalkWithJobsRead)
def get_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Retrieves talk metadata, current status, associated jobs with progress/timing, and preview URLs.
    Returns 404 if the talk does not exist or is not authorized under caller's event_ids.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    candidate_keys = [
        f"{talk.id}/preview/{name}.mp4" for name in settings.preview_presets
    ]
    candidate_keys.append(f"{talk.id}/preview/preview.mp4")

    preview_urls = [storage.url(key) for key in candidate_keys if storage.exists(key)]
    preview_urls = list(dict.fromkeys(preview_urls))

    talk_data = schemas.TalkWithJobsRead.model_validate(talk)
    talk_data.preview_urls = preview_urls
    return talk_data


@router.get("/{talk_id}/jobs", response_model=schemas.TalkJobsResponse)
def get_talk_jobs(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """Returns the current talk status along with recent jobs and active progress."""
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )
    jobs = (
        db.query(models.Job)
        .filter(models.Job.talk_id == talk_id)
        .order_by(models.Job.id.desc())
        .limit(10)
        .all()
    )
    return {"status": talk.status, "jobs": jobs}


@router.post(
    "/{talk_id}/recordings",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_507_INSUFFICIENT_STORAGE: {
            "description": "Insufficient storage to ingest recording"
        }
    },
)
def ingest_recording(
    talk_id: int,
    payload: schemas.RecordingIngestRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Ingests a recording file for the given talk and queues the detect job.
    Returns 404 if talk not found or not in caller's event_ids.
    Returns 409 if talk status is not 'waiting_for_files'.
    Returns 400 if ingest path validation fails.
    Returns 507 if storage space is insufficient.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if talk.status != "waiting_for_files":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot ingest recording for talk in status '{talk.status}'",
        )

    try:
        raw_key = stage_recording(talk.id, payload, storage)
    except IngestPathRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except InsufficientStorageError as exc:
        logger.warning(
            "Insufficient storage to ingest recording for talk %s: required %s bytes, available %s bytes",
            talk_id,
            exc.required_bytes,
            exc.available_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
            detail=str(exc),
        ) from exc

    advance(talk, "detecting")
    db.commit()
    db.refresh(talk)

    light_queue.enqueue(
        job_detect,
        talk.id,
        raw_key,
        job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
    )

    return schemas.TalkRead.model_validate(talk)


@router.post(
    "/{talk_id}/approve",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_200_OK,
)
def approve_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
    payload: schemas.ApproveRequest | None = None,
):
    """
    Approves or rejects a talk in pending_approval state.
    - decision=approve (default): transitions to pending_bounds. Human must submit cut bounds next.
    - decision=reject: transitions to rejected (terminal). No downstream jobs.
    Returns 404 if talk not found or not in caller's event_ids.
    Returns 409 if talk status is not 'pending_approval'.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if talk.status != "pending_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve/reject talk in status '{talk.status}'",
        )

    decision = payload.decision if payload else "approve"

    if decision == "reject":
        advance(talk, "rejected")
    else:
        advance(talk, "pending_bounds")

    db.commit()
    db.refresh(talk)

    if decision == "reject":
        cleanup_intermediates(storage, talk_id)

    return schemas.TalkRead.model_validate(talk)


RAW_PREVIEW_ALLOWED_STATES = frozenset(
    {
        "pending_bounds",
        "cutting",
        "generating_previews",
        "preview",
        "needs_work",
        "pending_intro_outro",
        "assembling",
        "transcoding",
        "uploading",
        "done",
    }
)


@router.get(
    "/{talk_id}/raw-preview",
    status_code=status.HTTP_200_OK,
)
def raw_preview(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Returns the raw-file storage URL for human review.
    Only accessible once the talk has been approved (pending_bounds or later).
    Returns 403 for any state before pending_bounds.
    Returns 404 if talk not found or no raw file exists.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if talk.status not in RAW_PREVIEW_ALLOWED_STATES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Raw preview not available in current state",
        )

    raw_keys = storage.list_keys(f"{talk.id}/raw/")
    if not raw_keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No raw recording found"
        )

    return {"url": storage.url(raw_keys[0])}


@router.post(
    "/{talk_id}/cut",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_cut_bounds(
    talk_id: int,
    payload: schemas.CutBoundsRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Submits file-relative cut bounds for a talk in pending_bounds state.
    Validates cut_end > cut_start and both within the detected file duration.
    Persists bounds on Talk, advances state to cutting, enqueues job_cut.
    Returns 409 if not in pending_bounds. Returns 422 if bounds are invalid.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if talk.status != "pending_bounds":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot submit cut bounds for talk in status '{talk.status}'",
        )

    cut_start_s, cut_end_s = payload.parsed_seconds()

    if talk.raw_duration_seconds is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Talk has no detected raw duration; cannot validate cut bounds",
        )

    if (
        cut_start_s >= talk.raw_duration_seconds
        or cut_end_s > talk.raw_duration_seconds
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Cut bounds [{cut_start_s}s, {cut_end_s}s] exceed "
                f"raw file duration {talk.raw_duration_seconds:.3f}s"
            ),
        )

    raw_keys = storage.list_keys(f"{talk.id}/raw/")
    if not raw_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No raw recording found for talk",
        )
    raw_key = raw_keys[0]

    talk.cut_start = cut_start_s
    talk.cut_end = cut_end_s
    advance(talk, "cutting")
    db.commit()
    db.refresh(talk)

    light_queue.enqueue(
        job_cut,
        talk.id,
        raw_key,
        job_timeout=STAGE_CONFIG["cut"]["job_timeout"],
    )

    return schemas.TalkRead.model_validate(talk)


@router.post(
    "/{talk_id}/assemble",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def configure_assembly(
    talk_id: int,
    payload: schemas.IntroOutroRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Configures intro and outro selection for a talk in pending_intro_outro state.
    - Validates custom clip paths against allowed ingest roots and video format.
    - Stages custom media into storage under {talk_id}/intro/ or {talk_id}/outro/.
    - Persists selection on Talk row and advances status to 'assembling'.
    - Dispatches background assembly pipeline (intro -> outro -> concat -> transcode).
    Returns 404 if talk not found or unauthorized for caller's events.
    Returns 409 if talk status is not 'pending_intro_outro'.
    Returns 400 if custom path validation fails.
    """
    talk = (
        db.query(models.Talk)
        .filter(models.Talk.id == talk_id)
        .with_for_update()
        .first()
    )
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if talk.status != "pending_intro_outro":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot configure intro/outro for talk in status '{talk.status}'; talk must be in 'pending_intro_outro'",
        )

    cut_keys = storage.list_keys(f"{talk.id}/cut/")
    default_cut_key = f"{talk.id}/cut/cut.mp4"
    if cut_keys:
        cut_key = cut_keys[0]
    elif storage.exists(default_cut_key):
        cut_key = default_cut_key
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No cut recording found for talk",
        )

    # Validate and stage custom clips before any DB mutation
    if payload.include_intro and payload.intro_source == "custom":
        try:
            stage_custom_clip(talk.id, payload.custom_intro_path, "intro", storage)
        except IngestPathRejectedError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Intro clip rejected: {exc}",
            ) from exc

    if payload.include_outro and payload.outro_source == "custom":
        try:
            stage_custom_clip(talk.id, payload.custom_outro_path, "outro", storage)
        except IngestPathRejectedError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Outro clip rejected: {exc}",
            ) from exc

    talk.include_intro = payload.include_intro
    talk.include_outro = payload.include_outro
    talk.intro_source = payload.intro_source if payload.include_intro else None
    talk.outro_source = payload.outro_source if payload.include_outro else None
    talk.custom_intro_path = (
        payload.custom_intro_path
        if payload.include_intro and payload.intro_source == "custom"
        else None
    )
    talk.custom_outro_path = (
        payload.custom_outro_path
        if payload.include_outro and payload.outro_source == "custom"
        else None
    )

    advance(talk, "assembling")
    db.commit()
    db.refresh(talk)

    try:
        dispatch_assembly(talk.id, cut_key)
    except Exception:
        advance(talk, "broken")
        log_key = f"{talk.id}/logs/assembly.log"
        log_content = traceback.format_exc()
        try:
            storage.put(log_key, log_content.encode("utf-8"))
        except Exception as log_err:  # noqa: BLE001
            logger.warning(
                "Failed to persist dispatch failure log to storage: %s", log_err
            )
            log_key = None
        job = models.Job(
            talk_id=talk.id,
            kind="assembly",
            status="failed",
            log_path=log_key,
        )
        db.add(job)
        db.commit()
        raise

    return schemas.TalkRead.model_validate(talk)


def _cancel_talk_jobs(talk_id: int, storage: StorageBackend | None = None) -> None:
    """
    Cancels queued and active RQ jobs for the given talk across all queues,
    and removes its artifacts from storage if storage is provided.
    Attempts all cleanup operations, collecting any failures, and raises
    an aggregate RuntimeError before callers delete or reset database records.
    """
    errors: list[str] = []
    try:
        from rq.registry import StartedJobRegistry

        for q in (light_queue, heavy_queue):
            # 1. Cancel queued jobs
            for job_id in list(q.job_ids):
                try:
                    rq_job = q.fetch_job(job_id)
                    if rq_job and rq_job.args and rq_job.args[0] == talk_id:
                        rq_job.cancel()
                        rq_job.delete()
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Failed cancelling queued job {job_id}: {exc}")

            # 2. Stop running/started/deferred/scheduled jobs
            registries = [
                getattr(q, "started_job_registry", None),
                getattr(q, "deferred_job_registry", None),
                getattr(q, "scheduled_job_registry", None),
            ]
            for reg in registries:
                if reg is None:
                    continue
                try:
                    for job_id in reg.get_job_ids():
                        try:
                            rq_job = q.fetch_job(job_id)
                            if rq_job and rq_job.args and rq_job.args[0] == talk_id:
                                send_stop_job_command(q.connection, job_id)
                                rq_job.cancel()
                                rq_job.delete()
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"Failed stopping job {job_id}: {exc}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"Failed accessing queue registry: {exc}")

            try:
                registry = StartedJobRegistry(queue=q)
                for job_id in registry.get_job_ids():
                    try:
                        rq_job = q.fetch_job(job_id)
                        if rq_job and rq_job.args and rq_job.args[0] == talk_id:
                            send_stop_job_command(q.connection, job_id)
                            rq_job.cancel()
                            rq_job.delete()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"Failed stopping started job {job_id}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Failed accessing StartedJobRegistry: {exc}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Error cancelling RQ jobs for talk {talk_id}: {exc}")

    if storage:
        try:
            storage.delete(str(talk_id))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Error deleting storage for talk {talk_id}: {exc}")

    if errors:
        msg = f"Cleanup failed for talk {talk_id}: " + "; ".join(errors)
        logger.warning(msg)
        raise RuntimeError(msg)


@router.post(
    "/{talk_id}/abort",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_200_OK,
)
def abort_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Aborts in-flight pipeline operations for a talk, cancels RQ jobs,
    clears DB jobs and reviews, and resets talk status back to 'waiting_for_files'.
    Returns 404 if talk is not found or not authorized for caller's events.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    _cancel_talk_jobs(talk.id, storage)

    # Clear DB jobs and reviews
    db.query(models.Job).filter(models.Job.talk_id == talk.id).delete()
    db.query(models.Review).filter(models.Review.talk_id == talk.id).delete()

    # Reset talk state and bounds back to waiting_for_files
    talk.status = "waiting_for_files"
    talk.raw_duration_seconds = None
    talk.cut_start = None
    talk.cut_end = None
    talk.include_intro = False
    talk.include_outro = False
    talk.intro_source = None
    talk.outro_source = None
    talk.custom_intro_path = None
    talk.custom_outro_path = None
    db.commit()
    db.refresh(talk)

    return schemas.TalkRead.model_validate(talk)


@router.patch("/{talk_id}", response_model=schemas.TalkRead)
def update_talk(
    talk_id: int,
    payload: schemas.TalkUpdate,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Updates editable talk metadata (title, room).
    Returns 404 if talk is not found or not in caller's event_ids.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    if payload.title is not None:
        title = payload.title.strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Talk title cannot be empty",
            )
        talk.title = title
    if payload.room is not None:
        talk.room = payload.room

    new_start = payload.start if payload.start is not None else talk.start
    new_end = payload.end if payload.end is not None else talk.end
    if new_start and new_end and new_end <= new_start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Talk end time must be after start time",
        )

    if payload.start is not None:
        talk.start = payload.start
    if payload.end is not None:
        talk.end = payload.end

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A talk with this title and start time already exists for this event",
        )
    db.refresh(talk)
    return talk


@router.delete("/{talk_id}", status_code=status.HTTP_200_OK)
def delete_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Deletes a talk and all associated storage artifacts, jobs, and reviews.
    Returns 404 if talk is not found or not in caller's event_ids.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    _cancel_talk_jobs(talk_id, storage)
    db.query(models.Review).filter(models.Review.talk_id == talk_id).delete()
    db.query(models.Job).filter(models.Job.talk_id == talk_id).delete()
    db.delete(talk)
    db.commit()

    return {"status": "ok", "deleted_id": talk_id}


@router.post("/bulk-delete", response_model=schemas.BulkDeleteResponse)
def bulk_delete_talks(
    payload: schemas.BulkDeleteRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Deletes multiple talks and cleans up their storage, jobs, and reviews.
    Only talks belonging to the caller's authorized event_ids are deleted.
    """
    if not payload.talk_ids:
        return {"status": "ok", "deleted_count": 0}

    valid_talks = (
        db.query(models.Talk)
        .filter(
            models.Talk.id.in_(payload.talk_ids),
            models.Talk.event_id.in_(client.event_ids),
        )
        .all()
    )

    deleted_count = 0
    for talk in valid_talks:
        _cancel_talk_jobs(talk.id, storage)
        db.query(models.Review).filter(models.Review.talk_id == talk.id).delete()
        db.query(models.Job).filter(models.Job.talk_id == talk.id).delete()
        db.delete(talk)
        deleted_count += 1

    db.commit()
    return {"status": "ok", "deleted_count": deleted_count}


@router.post(
    "/{talk_id}/upload",
    response_model=schemas.TalkRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_recording(
    talk_id: int,
    file: Annotated[UploadFile, File()],
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Uploads a video recording file directly via multipart form, saves to storage,
    advances status to 'detecting', and enqueues the detect job.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    # Atomically reserve talk from waiting_for_files to detecting before the first await
    updated = (
        db.query(models.Talk)
        .filter(models.Talk.id == talk_id, models.Talk.status == "waiting_for_files")
        .update({"status": "detecting"})
    )
    if not updated:
        db.refresh(talk)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot ingest recording for talk in status '{talk.status}'",
        )
    db.commit()
    db.refresh(talk)

    raw_key = f"{talk_id}/raw/raw.mp4"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            safe_filename = (
                Path(file.filename or "recording.mp4").name or "recording.mp4"
            )
            tmp_path = Path(tmpdir) / safe_filename
            # storage-boundary-exempt: upload staging
            with open(tmp_path, "wb") as f_out:  # noqa: ASYNC230
                while chunk := await file.read(1024 * 1024):
                    f_out.write(chunk)

            # Validate uploaded media using av.open to confirm valid video stream exists
            try:
                with av.open(str(tmp_path)) as container:
                    if not container.streams.video:
                        raise ValueError("No video stream found in uploaded file")
            except Exception as vid_err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid video file: {vid_err}",
                ) from vid_err

            storage.put(raw_key, tmp_path)

        light_queue.enqueue(
            job_detect,
            talk.id,
            raw_key,
            job_timeout=STAGE_CONFIG["detect"]["job_timeout"],
        )
    except Exception:
        talk.status = "waiting_for_files"
        db.commit()
        raise

    return schemas.TalkRead.model_validate(talk)


def _parse_iso_datetime(val: str | None) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        dt = datetime.fromisoformat(val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError, TypeError:
        return None


def _parse_duration_seconds(val: str | float | None) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        sec = float(val)
        return sec if math.isfinite(sec) and sec > 0 else None
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        low = val.lower()

        # Check longer suffixes before bare 's' so 'mins' and 'hrs' parse correctly
        for suffixes, multiplier in (
            (("hrs", "hr", "h"), 3600.0),
            (("mins", "min", "m"), 60.0),
            (("secs", "sec", "s"), 1.0),
        ):
            for suffix in suffixes:
                if low.endswith(suffix):
                    try:
                        parsed = float(low[: -len(suffix)].strip()) * multiplier
                        return parsed if math.isfinite(parsed) and parsed > 0 else None
                    except ValueError, TypeError:
                        return None

        if ":" in val:
            parts = val.split(":")
            try:
                if len(parts) == 2:
                    # MM:SS
                    parsed = float(int(parts[0]) * 60 + float(parts[1]))
                    return parsed if math.isfinite(parsed) and parsed > 0 else None
                elif len(parts) == 3:
                    # HH:MM:SS
                    parsed = float(
                        int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                    )
                    return parsed if math.isfinite(parsed) and parsed > 0 else None
            except ValueError, TypeError:
                return None
        try:
            parsed = float(val)
            return parsed if math.isfinite(parsed) and parsed > 0 else None
        except ValueError, TypeError:
            return None
    return None


MAX_SCHEDULE_IMPORT_SIZE = 10 * 1024 * 1024  # 10MB


@router.post("/schedule/import", response_model=schemas.ScheduleImportResponse)
async def import_schedule(
    request: Request,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile | None, File()] = None,
):
    """
    Imports talks in bulk from Frab/Pretalx JSON or simple JSON lists.
    """
    data = None
    if file and file.filename:
        content_len = request.headers.get("content-length")
        if content_len:
            try:
                if int(content_len) > MAX_SCHEDULE_IMPORT_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Schedule file exceeds maximum allowed size (10MB)",
                    )
            except ValueError:
                pass
        content = bytearray()
        while chunk := await file.read(1024 * 1024):
            content.extend(chunk)
            if len(content) > MAX_SCHEDULE_IMPORT_SIZE:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="Schedule file exceeds maximum allowed size (10MB)",
                )
        try:
            data = json.loads(content.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON schedule file",
            ) from exc
    else:
        content_len = request.headers.get("content-length")
        if content_len:
            try:
                if int(content_len) > MAX_SCHEDULE_IMPORT_SIZE:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail="Schedule body exceeds maximum allowed size (10MB)",
                    )
            except ValueError:
                pass
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            ) from exc
        if isinstance(body, dict):
            data = body.get("schedule") or body.get("talks") or body
        else:
            data = body

    if not data and not isinstance(data, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty schedule data"
        )

    event_name = "Conference Event"
    talks_to_create = []

    if isinstance(data, dict):
        event_name = data.get("event_name") or event_name
        if "schedule" in data and "conference" in data["schedule"]:
            conf = data["schedule"]["conference"]
            event_name = conf.get("title") or event_name
            for day in conf.get("days", []):
                for room_name, room_talks in day.get("rooms", {}).items():
                    for t in room_talks:
                        talks_to_create.append(
                            {
                                "title": t.get("title", "Untitled Session"),
                                "room": room_name,
                                "start": t.get("date") or t.get("start"),
                                "end": t.get("end"),
                                "duration": t.get("duration"),
                            }
                        )
        elif "talks" in data:
            talks_to_create = data["talks"]
        elif "title" in data:
            talks_to_create = [data]
    elif isinstance(data, list):
        talks_to_create = data

    if not talks_to_create:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No sessions found to import",
        )

    target_event_id = None
    if isinstance(data, dict) and data.get("event_id"):
        target_event_id = data.get("event_id")
    elif (
        talks_to_create
        and isinstance(talks_to_create[0], dict)
        and talks_to_create[0].get("event_id")
    ):
        target_event_id = talks_to_create[0].get("event_id")

    event = None
    is_new_event = False
    if target_event_id:
        existing_event = (
            db.query(models.Event).filter(models.Event.id == target_event_id).first()
        )
        if existing_event:
            verify_event_access(target_event_id, client)
            event = existing_event
        else:
            event = models.Event(id=target_event_id, name=event_name)
            db.add(event)
            db.commit()
            db.refresh(event)
            is_new_event = True
            try:
                db.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence('events', 'id'), (SELECT MAX(id) FROM events))"
                    )
                )
                db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed coordinating event sequence: %s", exc)

    if not event:
        # Check if caller already has an event with matching name
        event = (
            db.query(models.Event)
            .filter(
                models.Event.name == event_name,
                models.Event.id.in_(client.event_ids),
            )
            .first()
        )

    if not event:
        event = models.Event(name=event_name)
        db.add(event)
        db.commit()
        db.refresh(event)
        is_new_event = True

    # Ensure client has access to this newly created event
    if is_new_event and event.id not in client.event_ids:
        client.event_ids = list(set(client.event_ids + [event.id]))
        db.commit()

    created_count = 0
    now = datetime.now(UTC)
    for t_info in talks_to_create:
        if not isinstance(t_info, dict):
            continue
        title = t_info.get("title") or "Untitled Talk"
        room = t_info.get("room") or "Main Hall"

        t_start = _parse_iso_datetime(t_info.get("start") or t_info.get("date"))
        t_end = _parse_iso_datetime(t_info.get("end"))

        # Explicit duration_seconds / duration_minutes vs generic duration
        t_dur_sec = None
        if "duration_seconds" in t_info and t_info["duration_seconds"] is not None:
            try:
                t_dur_sec = float(t_info["duration_seconds"])
            except ValueError, TypeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid duration_seconds for talk '{title}'",
                )
            if not math.isfinite(t_dur_sec) or t_dur_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid duration_seconds for talk '{title}': must be positive and finite",
                )
        elif "duration_minutes" in t_info and t_info["duration_minutes"] is not None:
            try:
                t_dur_sec = float(t_info["duration_minutes"]) * 60.0
            except ValueError, TypeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid duration_minutes for talk '{title}'",
                )
            if not math.isfinite(t_dur_sec) or t_dur_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid duration_minutes for talk '{title}': must be positive and finite",
                )
        elif "duration" in t_info and t_info["duration"] is not None:
            t_dur_sec = _parse_duration_seconds(t_info["duration"])
            if t_dur_sec is None or not math.isfinite(t_dur_sec) or t_dur_sec <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid duration for talk '{title}': must be positive and finite",
                )

        if t_start and t_end:
            if t_end <= t_start:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"End time must be after start time for talk '{title}'",
                )
            start_dt = t_start
            end_dt = t_end
        elif t_start and t_dur_sec is not None:
            start_dt = t_start
            end_dt = t_start + timedelta(seconds=t_dur_sec)
        elif t_start:
            start_dt = t_start
            end_dt = t_start + timedelta(minutes=45)
        else:
            dur_sec = t_dur_sec if t_dur_sec is not None else 2700.0
            start_dt = now + timedelta(seconds=created_count * dur_sec)
            end_dt = start_dt + timedelta(seconds=dur_sec)

        existing = (
            db.query(models.Talk)
            .filter(
                models.Talk.event_id == event.id,
                models.Talk.title == title,
                models.Talk.start == start_dt,
            )
            .first()
        )
        if existing:
            existing.room = room
            existing.end = end_dt
            created_count += 1
            continue

        talk = models.Talk(
            event_id=event.id,
            title=title,
            room=room,
            start=start_dt,
            end=end_dt,
            status="waiting_for_files",
        )
        db.add(talk)
        created_count += 1

    db.commit()
    return {
        "status": "ok",
        "event_id": event.id,
        "event_name": event.name,
        "imported_count": created_count,
    }
