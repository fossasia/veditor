from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import get_client, verify_event_access
from app.db import get_db
from app.review_handlers import DECISION_HANDLERS
from app.storage import StorageBackend, get_storage_backend

router = APIRouter(
    prefix="/talks",
    tags=["reviews"],
    dependencies=[Depends(get_client)],
)


@router.post(
    "/{talk_id}/review",
    response_model=schemas.ReviewResponse,
    status_code=status.HTTP_200_OK,
)
def review_talk(
    talk_id: int,
    payload: schemas.ReviewRequest,
    client: Annotated[models.Client, Depends(get_client)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
):
    talk = (
        db.query(models.Talk)
        .filter(models.Talk.id == talk_id)
        .with_for_update()
        .first()
    )
    if not talk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Talk not found",
        )

    verify_event_access(talk.event_id, client)

    if talk.status != "preview":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot review talk in status '{talk.status}'; talk must be in 'preview'",
        )

    handler = DECISION_HANDLERS[payload.decision]
    return handler(talk, payload, db, storage=storage)
