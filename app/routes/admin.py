import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    CurrentUser,
    get_current_user,
    lock_active_admins,
    require_admin,
)
from app.db import get_db
from app.ui.templating import templates

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


EVENTS_PAGE_SIZE = 50

# Talk states in which a worker is actively processing the recording.
PROCESSING_STATUSES = (
    "detecting",
    "cutting",
    "generating_previews",
    "assembling",
    "transcoding",
    "uploading",
)


def _event_talk_stats_query():
    """One grouped query: per-event talk totals broken down by pipeline outcome.

    Every other state (waiting for files or awaiting a review decision) counts
    as pending. Events without talks are kept by the outer join.
    """
    talk = models.Talk
    return (
        select(
            models.Event.id,
            models.Event.name,
            models.User.email.label("owner_email"),
            func.count(talk.id).label("total"),
            func.count(talk.id).filter(talk.status == "done").label("done"),
            func.count(talk.id).filter(talk.status == "broken").label("broken"),
            func.count(talk.id).filter(talk.status == "rejected").label("rejected"),
            func.count(talk.id)
            .filter(talk.status.in_(PROCESSING_STATUSES))
            .label("processing"),
        )
        .outerjoin(talk, talk.event_id == models.Event.id)
        .outerjoin(models.User, models.User.id == models.Event.created_by_user_id)
        .group_by(models.Event.id, models.User.email)
    )


def _with_derived_stats(row) -> dict:
    stats = dict(row._mapping)
    stats["pending"] = (
        stats["total"]
        - stats["done"]
        - stats["broken"]
        - stats["rejected"]
        - stats["processing"]
    )
    return stats


@router.get("/events", response_class=HTMLResponse)
def admin_events_overview(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = Query(1, ge=1),
):
    total_events = db.scalar(select(func.count(models.Event.id))) or 0
    total_pages = max(1, math.ceil(total_events / EVENTS_PAGE_SIZE))
    page = min(page, total_pages)

    # Pick the page's events first so only their talks are aggregated.
    page_event_ids = (
        select(models.Event.id)
        .order_by(models.Event.id.desc())
        .offset((page - 1) * EVENTS_PAGE_SIZE)
        .limit(EVENTS_PAGE_SIZE)
    )
    rows = db.execute(
        _event_talk_stats_query()
        .where(models.Event.id.in_(page_event_ids.scalar_subquery()))
        .order_by(models.Event.id.desc())
    ).all()

    return templates.TemplateResponse(
        request,
        "admin_events.html.jinja",
        {
            "events": [_with_derived_stats(row) for row in rows],
            "total_events": total_events,
            "page": page,
            "total_pages": total_pages,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def admin_event_detail(
    event_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    row = db.execute(
        _event_talk_stats_query().where(models.Event.id == event_id)
    ).first()
    if row is None:
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

    return templates.TemplateResponse(
        request,
        "admin_event_detail.html.jinja",
        {
            "event": _with_derived_stats(row),
            "talks": talks,
        },
        headers={"Cache-Control": "no-store"},
    )
