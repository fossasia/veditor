import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.exceptions import RedisError
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob
from rq.job import JobStatus
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.db import get_db
from app.queue import QUEUES, priority_queue, redis_conn
from app.tasks import STAGE_CONFIG

logger = logging.getLogger(__name__)

# Queues whose pending jobs an admin may move into the priority queue.
PRIORITIZABLE_QUEUES = ("light", "heavy")

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/users", response_model=list[schemas.UserRead])
def list_users(
    db: Annotated[Session, Depends(get_db)],
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    return (
        db.query(models.User)
        .order_by(models.User.id.asc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.post("/users/{id}/promote", response_model=schemas.UserRead)
def promote_user(
    id: int,
    payload: schemas.UserPromoteRequest,
    db: Annotated[Session, Depends(get_db)],
):
    target = db.query(models.User).filter(models.User.id == id).first()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target.role == "admin" and target.is_active and payload.role != "admin":
        admin_ids = lock_active_admins(db)
        db.refresh(target)
        if target.role == "admin" and target.is_active and len(admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot demote user; operation would leave zero active administrators",
            )

    target.role = payload.role
    db.commit()
    db.refresh(target)
    return target


@router.post("/users/{id}/deactivate", response_model=schemas.UserRead)
def deactivate_user(
    id: int,
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if current_user.user_id is not None and current_user.user_id == id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )

    target = db.query(models.User).filter(models.User.id == id).first()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if target.role == "admin" and target.is_active:
        admin_ids = lock_active_admins(db)
        db.refresh(target)
        if target.role == "admin" and target.is_active and len(admin_ids) <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last active administrator",
            )

    target.is_active = False
    db.commit()
    db.refresh(target)
    return target


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _rq_job_row(rq_job: RQJob, queue_name: str) -> schemas.AdminJobRead:
    func_name = rq_job.func_name or ""
    kind = func_name.rsplit(".", 1)[-1].removeprefix("job_") or "unknown"
    talk_id = (
        rq_job.args[0] if rq_job.args and isinstance(rq_job.args[0], int) else None
    )
    return schemas.AdminJobRead(
        source="queue",
        rq_job_id=rq_job.id,
        talk_id=talk_id,
        kind=kind,
        status="queued",
        queue=queue_name,
        created_at=_aware(rq_job.created_at),
        updated_at=_aware(rq_job.enqueued_at),
        can_prioritize=queue_name in PRIORITIZABLE_QUEUES,
    )


def _db_job_row(job: models.Job) -> schemas.AdminJobRead:
    stage = STAGE_CONFIG.get(job.kind)
    return schemas.AdminJobRead(
        source="database",
        job_id=job.id,
        talk_id=job.talk_id,
        kind=job.kind,
        status=job.status,
        queue=str(stage["queue"]) if stage else None,
        progress_pct=job.progress_pct,
        created_at=_aware(job.started_at or job.updated_at),
        updated_at=_aware(job.updated_at),
    )


@router.get("/jobs", response_model=list[schemas.AdminJobRead])
def list_jobs(
    db: Annotated[Session, Depends(get_db)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    queue: Annotated[str | None, Query()] = None,
    sort: Literal["created_at", "status", "queue"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(100, ge=1, le=500),
):
    """
    Lists pending jobs from the RQ queues together with the most recent jobs
    recorded in the `jobs` table (running, done, failed, ...).

    Pending jobs only exist in Redis until a worker picks them up, so both
    sources are merged into a single table. If Redis is unreachable the
    database jobs are still returned.
    """
    if queue is not None and queue not in QUEUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown queue '{queue}'. Must be one of {list(QUEUES)}",
        )

    rows: list[schemas.AdminJobRead] = []

    if status_filter in (None, "queued"):
        for name, q in QUEUES.items():
            if queue is not None and name != queue:
                continue
            try:
                rows.extend(_rq_job_row(j, name) for j in q.get_jobs(0, limit))
            except RedisError as exc:
                logger.warning(
                    "Failed to read pending jobs from queue %s: %s", name, exc
                )

    query = db.query(models.Job)
    if status_filter is not None:
        query = query.filter(models.Job.status == status_filter)
    if queue is not None:
        kinds = [kind for kind, cfg in STAGE_CONFIG.items() if cfg["queue"] == queue]
        query = query.filter(models.Job.kind.in_(kinds))
    db_jobs = (
        query.order_by(
            func.coalesce(models.Job.started_at, models.Job.updated_at)
            .desc()
            .nulls_last(),
            models.Job.id.desc(),
        )
        .limit(limit)
        .all()
    )
    rows.extend(_db_job_row(job) for job in db_jobs)

    # Sort present values in the requested order, always keeping missing values last.
    present = [row for row in rows if getattr(row, sort) is not None]
    missing = [row for row in rows if getattr(row, sort) is None]
    present.sort(key=lambda row: getattr(row, sort), reverse=order == "desc")
    return (present + missing)[:limit]


@router.post(
    "/jobs/{rq_job_id}/prioritize",
    response_model=schemas.AdminJobRead,
)
def prioritize_job(rq_job_id: str):
    """
    Moves a pending job from its standard queue into the `priority` queue so
    workers pick it up before other pending jobs.

    The job is removed from its current queue with an atomic LREM; if a worker
    has already dequeued it in the meantime nothing is removed and 409 is
    returned, so a job can never run twice. Running jobs cannot be prioritized.
    """
    try:
        rq_job = RQJob.fetch(rq_job_id, connection=redis_conn)
    except NoSuchJobError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Job not found"
        ) from exc

    if rq_job.origin == priority_queue.name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is already in the priority queue",
        )

    source_name, source_queue = next(
        ((name, q) for name, q in QUEUES.items() if q.name == rq_job.origin),
        (None, None),
    )
    if (
        source_queue is None
        or source_name not in PRIORITIZABLE_QUEUES
        or rq_job.get_status() != JobStatus.QUEUED
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only pending jobs can be prioritized",
        )

    if not source_queue.remove(rq_job):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job is no longer pending; a worker has already picked it up",
        )

    priority_queue.enqueue_job(rq_job)
    logger.info("Admin moved job %s from queue %s to priority", rq_job.id, source_name)
    return _rq_job_row(rq_job, "priority")
