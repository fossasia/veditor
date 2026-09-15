"""Review decision handlers."""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app import models, schemas
from app.states import advance
from app.storage import StorageBackend, cleanup_intermediates
from app.tasks import dispatch_assembly

logger = logging.getLogger(__name__)


def _record_review_and_advance(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    target_state: str,
    db: Session,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    try:
        review = models.Review(
            talk_id=talk.id,
            decision=payload.decision.value,
            note=payload.note,
            user_id=user_id,
        )
        db.add(review)
        advance(talk, target_state)
        db.flush()
        response = schemas.ReviewResponse(
            talk=schemas.TalkRead.model_validate(talk),
            review=schemas.ReviewRead.model_validate(review),
        )
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def handle_approve(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
    storage: StorageBackend | None = None,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    response = _record_review_and_advance(
        talk, payload, "assembling", db, user_id=user_id
    )
    if storage is not None:
        cut_keys = storage.list_keys(f"{talk.id}/cut/")
        default_cut_key = f"{talk.id}/cut/cut.mp4"
        if cut_keys:
            cut_key = cut_keys[0]
        elif storage.exists(default_cut_key):
            cut_key = default_cut_key
        else:
            cut_key = default_cut_key  # fallback
        try:
            dispatch_assembly(talk.id, cut_key)
        except Exception as e:  # noqa: BLE001
            talk.status = "broken"
            db.commit()
            logger.error("Failed to dispatch assembly for talk %s: %s", talk.id, e)
    return response


def handle_needs_work(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
    storage: StorageBackend | None = None,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    return _record_review_and_advance(
        talk, payload, "pending_bounds", db, user_id=user_id
    )


def handle_reject(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
    storage: StorageBackend | None = None,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    talk.cut_start = None
    talk.cut_end = None
    response = _record_review_and_advance(
        talk, payload, "pending_bounds", db, user_id=user_id
    )
    if storage is not None:
        cleanup_intermediates(storage, talk.id)
    return response


DECISION_HANDLERS: dict[
    schemas.ReviewDecision,
    Callable[..., schemas.ReviewResponse],
] = {
    schemas.ReviewDecision.approve: handle_approve,
    schemas.ReviewDecision.needs_work: handle_needs_work,
    schemas.ReviewDecision.reject: handle_reject,
}
