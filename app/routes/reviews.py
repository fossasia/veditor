from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import (
    CurrentUser,
    check_event_access,
    check_talk_access,
    get_current_user,
)
from app.consts import AuthRole
from app.db import get_db
from app.review_handlers import DECISION_HANDLERS
from app.schemas import ReviewResponse
from app.storage import StorageBackend, get_storage_backend

from ..consts import TalkStatus

router = APIRouter(
    prefix="/talks",
    tags=["reviews"],
)


@router.post(
    "/{talk_id}/review",
    response_model=schemas.ReviewResponse,
    status_code=status.HTTP_200_OK,
)
def review_talk(
    talk_id: int,
    payload: schemas.ReviewRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    storage: Annotated[StorageBackend, Depends(get_storage_backend)],
) -> ReviewResponse:
    talk = db.scalar(
        select(models.Talk).where(models.Talk.id == talk_id).with_for_update()
    )
    if not talk:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Talk not found",
        )

    if user.source == "sso" or user.role == AuthRole.SPEAKER:
        check_talk_access(talk, user, db)
    else:
        if (
            user.role not in (AuthRole.ORGANIZER, AuthRole.ADMIN)
            and not user.is_machine
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operation requires minimum role 'organizer'",
            )
        check_event_access(talk.event_id, user, db)

    if talk.status != TalkStatus.PREVIEW:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot review talk in status '{talk.status}'; talk must be in 'preview'",
        )

    user_id = user.user_id if (not user.is_machine and not user.is_sso) else None
    handler = DECISION_HANDLERS[payload.decision]
    return handler(talk, payload, db, storage=storage, user_id=user_id)
