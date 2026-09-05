"""Review decision handlers."""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app import models, schemas
from app.states import advance


def _record_review_and_advance(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    target_state: str,
    db: Session,
) -> schemas.ReviewResponse:
    try:
        review = models.Review(
            talk_id=talk.id,
            decision=payload.decision.value,
            note=payload.note,
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
) -> schemas.ReviewResponse:
    return _record_review_and_advance(talk, payload, "transcoding", db)


def handle_needs_work(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
) -> schemas.ReviewResponse:
    return _record_review_and_advance(talk, payload, "pending_bounds", db)


def handle_reject(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
) -> schemas.ReviewResponse:
    return _record_review_and_advance(talk, payload, "pending_bounds", db)


DECISION_HANDLERS: dict[
    schemas.ReviewDecision,
    Callable[[models.Talk, schemas.ReviewRequest, Session], schemas.ReviewResponse],
] = {
    schemas.ReviewDecision.approve: handle_approve,
    schemas.ReviewDecision.needs_work: handle_needs_work,
    schemas.ReviewDecision.reject: handle_reject,
}
