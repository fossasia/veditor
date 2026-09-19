from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from rq import Worker
from sqlalchemy import case, func, text
from sqlalchemy.orm import Session, selectinload

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.db import get_db
from app.queue import redis_conn
from app.storage import StorageBackend, get_storage_backend
from app.ui.templating import templates

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    db_status = "Down"
    try:
        db.execute(text("SELECT 1"))
        db_status = "Healthy"
    except Exception:  # noqa: BLE001, S110
        pass

    redis_status = "Down"
    try:
        if redis_conn.ping():
            redis_status = "Healthy"
    except Exception:  # noqa: BLE001, S110
        pass

    light_workers = 0
    heavy_workers = 0
    try:
        all_workers = Worker.all(connection=redis_conn)
        for w in all_workers:
            queue_names = [q.name for q in w.queues]
            if "light" in queue_names:
                light_workers += 1
            if "heavy" in queue_names:
                heavy_workers += 1
    except Exception:  # noqa: BLE001, S110
        pass

    storage_status = "Available"
    free_bytes = 0
    total_bytes = 0
    used_bytes = 0
    try:
        free_bytes = storage.free_bytes()
        total_bytes = storage.total_bytes()
        used_bytes = total_bytes - free_bytes
    except OSError:
        storage_status = "Unavailable"

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html.jinja",
        {
            "db_status": db_status,
            "redis_status": redis_status,
            "light_workers": light_workers,
            "heavy_workers": heavy_workers,
            "storage_status": storage_status,
            "free_bytes": free_bytes,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
        },
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


PROCESSING_STATUSES = (
    "detecting",
    "cutting",
    "generating_previews",
    "assembling",
    "transcoding",
    "uploading",
)
PENDING_STATUSES = (
    "pending_approval",
    "pending_bounds",
    "needs_work",
    "pending_intro_outro",
)


@router.get("/events", response_class=HTMLResponse)
def admin_events(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    total_events = db.query(func.count(models.Event.id)).scalar() or 0
    macro_row = db.query(
        func.count(models.Talk.id).label("total_talks"),
        func.count(case((models.Talk.status == "done", 1))).label("total_done"),
        func.count(case((models.Talk.status.in_(["broken", "rejected"]), 1))).label(
            "total_broken"
        ),
        func.count(case((models.Talk.status.in_(PROCESSING_STATUSES), 1))).label(
            "total_processing"
        ),
    ).first()

    macro_stats = {
        "total_events": total_events,
        "total_talks": 0,
        "total_done": 0,
        "total_processing": 0,
        "total_broken": 0,
        **({k: v or 0 for k, v in macro_row._asdict().items()} if macro_row else {}),
    }

    total_pages = max(1, (total_events + limit - 1) // limit) if total_events > 0 else 1
    if page > total_pages:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Page not found",
        )
    offset = (page - 1) * limit

    rows = (
        db.query(
            models.Event.id,
            models.Event.name,
            models.Event.created_by_user_id,
            models.User.email.label("creator_email"),
            func.count(models.Talk.id).label("total_talks"),
            func.count(case((models.Talk.status == "done", 1))).label("done_count"),
            func.count(case((models.Talk.status.in_(["broken", "rejected"]), 1))).label(
                "broken_count"
            ),
            func.count(case((models.Talk.status.in_(PROCESSING_STATUSES), 1))).label(
                "processing_count"
            ),
            func.count(case((models.Talk.status.in_(PENDING_STATUSES), 1))).label(
                "pending_count"
            ),
            func.count(case((models.Talk.status == "preview", 1))).label(
                "preview_count"
            ),
            func.count(case((models.Talk.status == "waiting_for_files", 1))).label(
                "waiting_count"
            ),
        )
        .outerjoin(models.Talk, models.Talk.event_id == models.Event.id)
        .outerjoin(models.User, models.Event.created_by_user_id == models.User.id)
        .group_by(
            models.Event.id,
            models.Event.name,
            models.Event.created_by_user_id,
            models.User.email,
        )
        .order_by(models.Event.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    events = [
        {
            **r._asdict(),
            "progress_pct": round(((r.done_count or 0) / r.total_talks) * 100, 1)
            if r.total_talks
            else 0,
        }
        for r in rows
    ]

    pagination = {
        "page": page,
        "limit": limit,
        "total_items": total_events,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": page - 1,
        "next_page": page + 1,
        "start_item": offset + 1 if total_events > 0 and offset < total_events else 0,
        "end_item": min(offset + limit, total_events) if total_events > 0 else 0,
    }

    return templates.TemplateResponse(
        request,
        "admin_events.html.jinja",
        {
            "events": events,
            "macro_stats": macro_stats,
            "pagination": pagination,
        },
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def admin_event_detail(
    request: Request,
    event_id: int,
    db: Annotated[Session, Depends(get_db)],
):
    event = (
        db.query(models.Event)
        .options(selectinload(models.Event.created_by_user))
        .filter(models.Event.id == event_id)
        .first()
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    talks = (
        db.query(models.Talk)
        .filter(models.Talk.event_id == event_id)
        .order_by(models.Talk.start.asc(), models.Talk.id.asc())
        .all()
    )

    counts = Counter(t.status for t in talks)
    total = len(talks)
    done_count = counts["done"]
    progress_pct = round((done_count / total) * 100, 1) if total > 0 else 0

    event_stats = {
        "total": total,
        "done": done_count,
        "broken": counts["broken"] + counts["rejected"],
        "processing": sum(counts[s] for s in PROCESSING_STATUSES),
        "pending": sum(counts[s] for s in PENDING_STATUSES),
        "preview": counts["preview"],
        "waiting": counts["waiting_for_files"],
    }

    return templates.TemplateResponse(
        request,
        "admin_event_detail.html.jinja",
        {
            "event": event,
            "talks": talks,
            "event_stats": event_stats,
            "progress_pct": progress_pct,
        },
    )
