import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    CurrentUser,
    check_event_access,
    get_client,
    hash_api_key,
    require_role,
)
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
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to create events",
        )
    created_by = user.user_id if not user.is_machine else None
    event = models.Event(
        name=payload.name,
        source=payload.source,
        external_id=payload.external_id,
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
    if user.is_sso:
        if user.scope_type == "event" and user.scope_id:
            return db.query(models.Event).filter(models.Event.id == user.scope_id).all()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO session is not authorized to list events",
        )
    if user.is_machine:
        if getattr(user, "is_platform", False):
            return db.query(models.Event).all()
        return db.query(models.Event).filter(models.Event.id.in_(user.event_ids)).all()
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
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to modify events",
        )
    event = check_event_access(event_id, user, db)

    if payload.name is not None:
        clean_name = payload.name.strip()
        if not clean_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Event name cannot be empty",
            )
        event.name = clean_name

    if payload.source is not None:
        event.source = payload.source

    if payload.external_id is not None:
        event.external_id = payload.external_id

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
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to delete events",
        )
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
    "/{event_identifier}/sso-token",
    response_model=schemas.SSOTokenResponse,
    status_code=status.HTTP_200_OK,
)
def create_event_sso_token(
    event_identifier: str,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    payload: schemas.EventSSOTokenRequest | None = None,
):
    """
    Issues a short-lived, event-scoped SSO token carrying role=organizer.
    Resolves event by integer ID or external slug (external_id=event_slug).
    Requires caller to be authenticated via X-API-Key only.
    """
    event = None
    if event_identifier.isdigit():
        event = (
            db.query(models.Event)
            .filter(models.Event.id == int(event_identifier))
            .first()
        )
    if not event:
        event_filters = [models.Event.external_id == event_identifier]
        if not getattr(client, "is_platform", False) and client.event_ids:
            event_filters.append(models.Event.id.in_(client.event_ids))
        event = db.query(models.Event).filter(*event_filters).first()
    if not event:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )
    if not getattr(client, "is_platform", False) and event.id not in (
        client.event_ids or []
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is not authorized to mint an SSO token for this event",
        )

    target_role = payload.role if payload and payload.role else "organizer"
    target_email = payload.email if payload else None
    target_display_name = payload.display_name if payload else None

    token = create_sso_token(
        scope_type="event",
        scope_id=event.id,
        role=target_role,
        expires_in_seconds=settings.sso_token_expire_seconds,
        email=target_email,
        display_name=target_display_name,
    )
    return schemas.SSOTokenResponse(
        token=token,
        token_type="bearer",
        scope_type="event",
        scope_id=event.id,
        role=target_role,
        expires_in_seconds=settings.sso_token_expire_seconds,
        url=f"/studio?event_id={event.id}&sso_token={token}",
    )


@router.get(
    "/{event_id}/api-keys",
    response_model=list[schemas.ApiKeyRead],
    status_code=status.HTTP_200_OK,
)
def list_event_api_keys(
    event_id: int,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
):
    """List all active API keys scoped to this event."""
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to manage API keys",
        )
    check_event_access(event_id, user, db)
    event_clients = (
        db.query(models.Client)
        .filter(
            models.Client.is_platform.is_(False),
            models.Client.event_ids.any(event_id),
        )
        .all()
    )
    event_clients = [c for c in event_clients if event_id in (c.event_ids or [])]

    results = []
    for c in event_clients:
        masked = f"client_{c.id}_{c.hashed_key[:8]}..."
        results.append(
            schemas.ApiKeyRead(
                id=c.id,
                name=c.name or f"API Key #{c.id}",
                masked_key=masked,
                event_ids=list(c.event_ids or []),
                webhook_url=c.webhook_url,
                created_at=getattr(c, "created_at", None),
                last_used_at=getattr(c, "last_used_at", None),
            )
        )
    return results


@router.post(
    "/{event_id}/api-keys",
    response_model=schemas.ApiKeyCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_event_api_key(
    event_id: int,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
    payload: schemas.ApiKeyCreate | None = None,
):
    """Generate a new event-scoped API key and return the unhashed key once."""
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to manage API keys",
        )
    check_event_access(event_id, user, db)

    raw_api_key = secrets.token_urlsafe(32)
    hashed_key = hash_api_key(raw_api_key)
    name = (
        payload.name.strip()
        if (payload and payload.name and payload.name.strip())
        else f"Event #{event_id} API Key"
    )
    webhook_url = payload.webhook_url if payload else None

    # Enforce at most 1 active API key per event by revoking previous key(s)
    existing_clients = (
        db.query(models.Client)
        .filter(
            models.Client.is_platform.is_(False),
            models.Client.event_ids.any(event_id),
        )
        .all()
    )
    for existing_c in existing_clients:
        remaining = [eid for eid in (existing_c.event_ids or []) if eid != event_id]
        if remaining:
            existing_c.event_ids = remaining
        else:
            db.delete(existing_c)
    db.flush()

    client = models.Client(
        hashed_key=hashed_key,
        event_ids=[event_id],
        name=name,
        webhook_url=webhook_url,
    )
    db.add(client)
    db.commit()
    db.refresh(client)

    return schemas.ApiKeyCreatedResponse(
        id=client.id,
        name=client.name,
        api_key=raw_api_key,
        event_id=event_id,
        created_at=client.created_at,
    )


@router.delete(
    "/{event_id}/api-keys/{client_id}",
    status_code=status.HTTP_200_OK,
)
def revoke_event_api_key(
    event_id: int,
    client_id: int,
    user: Annotated[CurrentUser, Depends(require_role("organizer"))],
    db: Annotated[Session, Depends(get_db)],
):
    """Revoke an API key scoped to this event."""
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SSO sessions are not permitted to manage API keys",
        )
    check_event_access(event_id, user, db)

    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client or client.is_platform or event_id not in (client.event_ids or []):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found for this event",
        )

    remaining = [eid for eid in (client.event_ids or []) if eid != event_id]
    if remaining:
        client.event_ids = remaining
    else:
        db.delete(client)
    db.commit()
    return {"status": "ok", "deleted_id": client_id}
