from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client
from app.db import get_db

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(get_client)],
)


@router.get("/{job_id}", response_model=schemas.JobRead)
def get_job(
    job_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Retrieves job status and metadata for polling clients.

    Returns 404 if the job does not exist or the job's talk is not authorized
    under caller's event_ids.
    """
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job or not job.talk or job.talk.event_id not in (client.event_ids or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        )

    return job
