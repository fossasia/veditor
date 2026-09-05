import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from rq.command import send_stop_job_command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client, verify_event_access
from app.config import settings
from app.db import get_db
from app.ingest import (
    IngestPathRejectedError,
    InsufficientStorageError,
    stage_recording,
)
from app.queue import heavy_queue, light_queue
from app.states import advance
from app.storage import StorageBackend, get_storage_backend
from app.tasks import STAGE_CONFIG, job_cut, job_detect

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


@router.get("/{talk_id}", response_model=schemas.TalkRead)
def get_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    """
    Retrieves talk metadata, current status, and preview URLs.
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

    talk_data = schemas.TalkRead.model_validate(talk)
    talk_data.preview_urls = preview_urls
    return talk_data


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
    return schemas.TalkRead.model_validate(talk)


RAW_PREVIEW_ALLOWED_STATES = frozenset(
    {
        "pending_bounds",
        "cutting",
        "generating_previews",
        "preview",
        "needs_work",
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
    Aborts all running/queued processes for the talk, cancels RQ jobs,
    deletes all associated storage files, clears DB jobs and reviews,
    and resets talk status back to 'waiting_for_files'.
    """
    talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    if not talk or talk.event_id not in client.event_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    # Cancel and remove any enqueued or active RQ jobs for this talk
    for q in (light_queue, heavy_queue):
        try:
            job_ids_to_check = set(q.job_ids)
            for reg in (
                q.started_job_registry,
                q.deferred_job_registry,
                q.scheduled_job_registry,
            ):
                try:
                    job_ids_to_check.update(reg.get_job_ids())
                except Exception:  # noqa: BLE001, S110
                    pass

            for job_id in job_ids_to_check:
                try:
                    job = q.fetch_job(job_id)
                    if (
                        job
                        and job.args
                        and len(job.args) > 0
                        and job.args[0] == talk.id
                    ):
                        try:
                            send_stop_job_command(q.connection, job.id)
                        except Exception:  # noqa: BLE001, S110
                            pass
                        job.cancel()
                        job.delete()
                except Exception:  # noqa: BLE001, S110
                    pass
        except Exception:  # noqa: BLE001, S110
            pass

    # Delete all storage artifacts for this talk
    storage.delete(str(talk.id))

    # Clear DB jobs and reviews
    db.query(models.Job).filter(models.Job.talk_id == talk.id).delete()
    db.query(models.Review).filter(models.Review.talk_id == talk.id).delete()

    # Reset talk state and bounds back to waiting_for_files
    talk.status = "waiting_for_files"
    talk.raw_duration_seconds = None
    talk.cut_start = None
    talk.cut_end = None
    db.commit()
    db.refresh(talk)

    return schemas.TalkRead.model_validate(talk)
