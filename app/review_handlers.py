"""Review decision handlers."""

import logging
import traceback
from collections.abc import Callable
from http import HTTPStatus

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import models, schemas
from . import tasks as app_tasks
from .consts import JobKind, JobStatus, TalkStatus
from .states import advance
from .storage import StorageBackend, cleanup_intermediates
from .webhook import dispatch_talk_webhook


logger = logging.getLogger(__name__)


def _record_review_and_advance(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    target_state: TalkStatus,
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

        if payload.decision == schemas.ReviewDecision.approve:
            if talk.cut_start is None or talk.cut_end is None:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail='Cannot approve a talk without cut bounds.',
                )
            db.add(
                models.ApprovedCut(
                    talk_id=talk.id,
                    cut_start=talk.cut_start,
                    cut_end=talk.cut_end,
                    review=review,
                )
            )

        advance(talk, target_state)
        db.flush()
        response = schemas.ReviewResponse(
            talk=schemas.TalkRead.model_validate(talk),
            review=schemas.ReviewRead.model_validate(review),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    if target_state == TalkStatus.PENDING_BOUNDS:
        try:
            dispatch_talk_webhook('talk.bounds_pending', talk, db)
        except (SQLAlchemyError, RedisError) as webhook_err:
            logger.warning(
                'Failed to dispatch talk.bounds_pending webhook for talk %s: %s',
                talk.id,
                webhook_err,
            )

    return response


def handle_approve(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
    storage: StorageBackend | None = None,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    cut_keys = storage.list_keys(f'{talk.id}/cut/') if storage else []
    cut_key = cut_keys[0] if cut_keys else f'{talk.id}/cut/cut.mp4'

    response = _record_review_and_advance(
        talk, payload, TalkStatus.ASSEMBLING, db, user_id=user_id
    )

    try:
        app_tasks.dispatch_assembly(talk.id, cut_key)
    except Exception:
        advance(talk, TalkStatus.BROKEN)
        log_key: str | None = None
        if storage is not None:
            log_key = f'{talk.id}/logs/assembly.log'
            try:
                storage.put(log_key, traceback.format_exc().encode('utf-8'))
            except (OSError, ValueError, TypeError) as log_err:
                logger.warning(
                    'Failed to persist dispatch failure log to storage: %s', log_err
                )
                log_key = None
        db.add(
            models.Job(
                talk_id=talk.id,
                kind=JobKind.ASSEMBLY,
                status=JobStatus.FAILED,
                log_path=log_key,
            )
        )
        db.commit()
        raise

    return response


def handle_needs_work(
    talk: models.Talk,
    payload: schemas.ReviewRequest,
    db: Session,
    storage: StorageBackend | None = None,
    user_id: int | None = None,
) -> schemas.ReviewResponse:
    if talk.status == TalkStatus.DONE:
        talk.final_cleaned_at = None
        # Custom bumper files are permanently deleted by cleanup_bumpers() during
        # job_transcode, so they no longer exist in storage or on disk. Reset to
        # 'generated' so job_intro / job_outro regenerate title slates from talk
        # metadata on re-assembly, preventing StorageKeyNotFoundError in job_concat.
        if talk.intro_source == 'custom':
            talk.intro_source = 'generated'
            talk.custom_intro_path = None
        if talk.outro_source == 'custom':
            talk.outro_source = 'generated'
            talk.custom_outro_path = None
        db.execute(
            delete(models.Job).where(
                models.Job.talk_id == talk.id,
                models.Job.kind.in_(
                    [
                        JobKind.INTRO,
                        JobKind.OUTRO,
                        JobKind.CONCAT,
                        JobKind.LOUDNESS,
                        JobKind.TRANSCODE,
                        JobKind.PUBLISH,
                    ]
                ),
            )
        )
    return _record_review_and_advance(
        talk, payload, TalkStatus.PENDING_BOUNDS, db, user_id=user_id
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
        talk, payload, TalkStatus.PENDING_BOUNDS, db, user_id=user_id
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
