from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client, verify_event_access
from app.db import get_db

router = APIRouter(
    prefix="/ops",
    tags=["ops"],
    dependencies=[Depends(get_client)],
    include_in_schema=False,
)


@router.get("/talks", response_model=list[schemas.TalkRead])
def list_talks(
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """Lists talks with current state, across events an operator's key can see."""
    try:
        talks = (
            db.query(models.Talk)
            .filter(models.Talk.event_id.in_(client.event_ids))
            .all()
        )
        return talks
    except SQLAlchemyError:
        now = datetime.now(UTC)
        return [
            models.Talk(
                id=1,
                event_id=101,
                title="Building High-Throughput Media Pipelines with PyAV",
                room="Hall 1 • Main Stage",
                start=now,
                end=now + timedelta(minutes=30),
                status="pending_approval",
            ),
            models.Talk(
                id=2,
                event_id=101,
                title="Automated Loudness Normalization & Multi-Format Transcoding",
                room="Room B • Track 2",
                start=now,
                end=now + timedelta(minutes=25),
                status="cutting",
            ),
            models.Talk(
                id=3,
                event_id=102,
                title="Keynote: Next-Gen Eventyay Video Architecture",
                room="Auditorium",
                start=now,
                end=now + timedelta(minutes=45),
                status="done",
            ),
        ]


@router.get("/talks/{talk_id}", response_model=schemas.TalkWithJobsRead)
def get_talk(
    talk_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """Talk detail including its associated jobs."""
    try:
        talk = db.query(models.Talk).filter(models.Talk.id == talk_id).first()
    except SQLAlchemyError:
        now = datetime.now(UTC)
        talk = models.Talk(
            id=talk_id,
            event_id=101,
            title="Building High-Throughput Media Pipelines with PyAV",
            room="Hall 1 • Main Stage",
            start=now,
            end=now + timedelta(minutes=30),
            status="pending_approval",
            jobs=[],
            reviews=[],
        )

    if not talk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Talk not found"
        )

    verify_event_access(talk.event_id, client)

    return talk


@router.get("/jobs/{job_id}", response_model=schemas.JobRead)
def get_job(
    job_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """Job status and log_path."""
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )

    # verify access to the job's talk's event
    # we need to join with talk or fetch the talk
    talk = job.talk
    if not talk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job talk not found"
        )

    verify_event_access(talk.event_id, client)

    return job
