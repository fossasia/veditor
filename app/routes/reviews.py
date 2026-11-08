from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import (
    CurrentUser,
    check_event_access,
    check_talk_access,
    get_current_user,
)
from ..consts import AuthRole, TalkStatus
from ..db import get_db
from ..review_handlers import DECISION_HANDLERS
from ..schemas import ReviewResponse
from ..storage import StorageBackend, get_storage_backend


router = APIRouter(
    prefix='/talks',
    tags=['reviews'],
)


@router.post(
    '/{talk_id}/review',
    response_model=schemas.ReviewResponse,
    status_code=HTTPStatus.OK,
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
            status_code=HTTPStatus.NOT_FOUND,
            detail='Talk not found',
        )

    if user.source == 'sso' or user.role == AuthRole.SPEAKER:
        check_talk_access(talk, user, db)
    else:
        if (
            user.role not in (AuthRole.ORGANIZER, AuthRole.ADMIN)
            and not user.is_machine
        ):
            raise HTTPException(
                status_code=HTTPStatus.FORBIDDEN,
                detail="Operation requires minimum role 'organizer'",
            )
        check_event_access(talk.event_id, user, db)

    if talk.status != TalkStatus.PREVIEW:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=f"Cannot review talk in status '{talk.status}'; talk must be in 'preview'",
        )

    user_id = user.user_id if (not user.is_machine and not user.is_sso) else None
    handler = DECISION_HANDLERS[payload.decision]
    return handler(talk, payload, db, storage=storage, user_id=user_id)
