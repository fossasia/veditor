import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import CurrentUser, check_event_access, get_client, require_role
from app.config import settings
from app.db import get_db
from app.routes.talks import _cancel_talk_jobs
from app.security import create_sso_token
from app.storage import StorageBackend, get_storage_backend

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/events",
    tags=["events"],
)


@router.post("", response_model=schemas.EventRead, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: schemas.EventCreate,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
):
    created_by = user.user_id if not user.is_machine else None
    event = models.Event(
        name=payload.name,
        retention_overrides=payload.retention_overrides,
        created_by_user_id=created_by,
    )
    db.add(event)
    db.flush()
    if user.is_machine:
        if event.id not in user.event_ids:
            user.event_ids.append(event.id)
        if user.client_id:
            client_record = (
                db.query(models.Client)
                .filter(models.Client.id == user.client_id)
                .first()
            )
            if client_record and event.id not in (client_record.event_ids or []):
                client_record.event_ids = list(
                    set((client_record.event_ids or []) + [event.id])
                )
    db.commit()
    db.refresh(event)
    return event


@router.get("", response_model=list[schemas.EventRead], status_code=status.HTTP_200_OK)
def list_events(
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
):
    if user.is_machine:
        return db.query(models.Event).filter(models.Event.id.in_(user.event_ids)).all()
    if user.role == "admin":
        return db.query(models.Event).all()
    return (
        db.query(models.Event)
        .filter(models.Event.created_by_user_id == user.user_id)
        .all()
    )


@router.patch("/{event_id}", response_model=schemas.EventRead)
def update_event(
    event_id: int,
    payload: schemas.EventUpdate,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
):
    event = check_event_access(event_id, user, db)

    if payload.name is not None:
        clean_name = payload.name.strip()
        if not clean_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event name cannot be empty",
            )
        event.name = clean_name

    if payload.retention_overrides is not None:
        event.retention_overrides = payload.retention_overrides

    db.commit()
    db.refresh(event)
    return event


@router.delete("/{event_id}")
def delete_event(
    event_id: int,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    check_event_access(event_id, user, db)
    event = (
        db.query(models.Event)
        .filter(models.Event.id == event_id)
        .with_for_update()
        .first()
    )
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Event not found"
        )

    talks = list(event.talks)
    for talk in talks:
        _cancel_talk_jobs(talk.id, storage)

    for talk in talks:
        db.query(models.Review).filter(models.Review.talk_id == talk.id).delete()
        db.query(models.Job).filter(models.Job.talk_id == talk.id).delete()
        db.delete(talk)

    db.delete(event)
    db.commit()
    return {"status": "ok", "deleted_id": event_id}


@router.post(
    "/{event_id}/sso-token",
    response_model=schemas.SSOTokenResponse,
    status_code=status.HTTP_200_OK,
)
def create_event_sso_token(
    event_id: int,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Issues a short-lived, event-scoped SSO token carrying role=organizer.
    Requires caller to be authenticated via X-API-Key only.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )
    if event_id not in (client.event_ids or []):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is not authorized to mint an SSO token for this event",
        )

    token = create_sso_token(
        scope_type="event",
        scope_id=event_id,
        role="organizer",
        expires_in_seconds=settings.sso_token_expire_seconds,
    )
    return schemas.SSOTokenResponse(
        token=token,
        token_type="bearer",
        scope_type="event",
        scope_id=event_id,
        role="organizer",
        expires_in_seconds=settings.sso_token_expire_seconds,
        url=f"/studio?event_id={event_id}&sso_token={token}",
    )
