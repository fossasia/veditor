from dataclasses import dataclass
from typing import Any, Final, Literal

DEFAULT_FINAL_RETENTION_DAYS: Final[int] = 14
INDEFINITE: Final = "indefinite"

RetentionDays = int | Literal["indefinite"]


def validate_final_retention_days(value: Any) -> RetentionDays:
    """Validate final_retention_days is a non-negative int or 'indefinite'."""
    if value == INDEFINITE:
        return INDEFINITE
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError(
        f"final_retention_days must be a non-negative integer or '{INDEFINITE}', got {value!r}"
    )


def validate_retention_overrides(overrides: Any) -> dict[str, Any] | None:
    """Validate retention overrides dictionary or None."""
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise TypeError("retention_overrides must be a dictionary or None")

    for key in overrides:
        if not isinstance(key, str):
            raise TypeError(
                f"All keys in retention_overrides must be strings, got {type(key).__name__}"
            )

    if "final_retention_days" in overrides:
        validate_final_retention_days(overrides["final_retention_days"])

    return overrides


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention configuration for intermediate and final media stages.

    Note: preview/ and cut/ deletion is lifecycle event-driven (e.g. done/rejected)
    and does not expose a day-count knob.
    """

    final_retention_days: RetentionDays = DEFAULT_FINAL_RETENTION_DAYS

    def __post_init__(self) -> None:
        validate_final_retention_days(self.final_retention_days)

    @property
    def is_final_indefinite(self) -> bool:
        """Whether final artifacts are retained indefinitely."""
        return self.final_retention_days == INDEFINITE


def get_retention(event: Any | None = None) -> RetentionPolicy:
    """Resolve retention policy for an event or talk, falling back to defaults."""
    if event is None:
        return RetentionPolicy()

    if isinstance(event, dict):
        overrides = event.get("retention_overrides", event)
    else:
        overrides = getattr(event, "retention_overrides", None)
        if overrides is None and hasattr(event, "event"):
            overrides = getattr(event.event, "retention_overrides", None)

    if not overrides or not isinstance(overrides, dict):
        return RetentionPolicy()

    final_days = overrides.get("final_retention_days")
    if final_days is None:
        return RetentionPolicy()

    return RetentionPolicy(final_retention_days=final_days)


RETENTION_SWEEP_JOB_ID: Final[str] = "retention_sweep"


def run_retention_sweep(
    db: Any | None = None,
    storage: Any | None = None,
) -> list[int]:
    """Sweep and delete final/ storage for done talks past their retention window.

    - Queries talks in 'done' whose updated_at + resolved final_retention_days has elapsed.
    - Re-resolves RetentionPolicy per event on each run (no caching).
    - Honors 'indefinite' retention overrides (skips deletion).
    - Idempotent: missing final/ paths are treated as already clean without error.
    - Excludes 'rejected' talks and intermediate/raw storage.
    - Returns list of talk IDs whose final/ storage was deleted.
    """
    import logging
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import joinedload

    from app.db import SessionLocal
    from app.models import Talk
    from app.storage import get_storage_backend

    logger = logging.getLogger(__name__)

    if db is None:
        with SessionLocal() as session:
            return run_retention_sweep(db=session, storage=storage)

    if storage is None:
        storage = get_storage_backend()

    now = datetime.now(UTC)
    swept_talk_ids: list[int] = []

    talks = (
        db.query(Talk)
        .options(joinedload(Talk.event))
        .filter(Talk.status == "done", Talk.final_cleaned_at.is_(None))
        .order_by(Talk.id.asc())
        .all()
    )

    for talk in talks:
        if talk.updated_at is None:
            continue

        updated_at = (
            talk.updated_at
            if talk.updated_at.tzinfo is not None
            else talk.updated_at.replace(tzinfo=UTC)
        )

        event_obj = getattr(talk, "event", None)
        policy = get_retention(event_obj if event_obj is not None else talk)
        if policy.is_final_indefinite:
            continue

        retention_delta = timedelta(days=policy.final_retention_days)
        if now < updated_at + retention_delta:
            continue

        final_prefix = f"{talk.id}/final"
        talk_id = talk.id
        existing_keys = None
        try:
            if hasattr(storage, "list_keys"):
                existing_keys = storage.list_keys(final_prefix)
                if not existing_keys:
                    talk.final_cleaned_at = now
                    try:
                        db.flush()
                    except SQLAlchemyError:
                        db.rollback()
                        raise
                    continue

            storage.delete(final_prefix)
            talk.final_cleaned_at = now
            swept_talk_ids.append(talk_id)
            try:
                db.flush()
            except SQLAlchemyError:
                db.rollback()
                raise
            logger.info(
                "Deleted final storage for talk %s (%s keys removed)",
                talk_id,
                len(existing_keys) if existing_keys is not None else "unknown",
            )
        except SQLAlchemyError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to process final storage for talk %s: %s",
                talk_id,
                exc,
            )

    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    return swept_talk_ids


def enqueue_retention_sweep(queue: Any | None = None) -> Any:
    """Enqueue run_retention_sweep to the RQ light queue."""
    from app.queue import light_queue

    target_queue = queue or light_queue
    return target_queue.enqueue(
        run_retention_sweep,
        job_timeout=600,
        description="Retention sweep for expired final artifacts",
    )


def register_periodic_retention_sweep(
    queue: Any | None = None,
    interval_seconds: int | None = None,
    times: int | None = None,
    retry: Any | None = None,
) -> Any:
    """Register a scheduled periodic retention sweep job on the RQ queue."""
    import logging
    from datetime import timedelta

    from rq import Retry
    from rq.job import Job
    from rq.repeat import Repeat

    from app.config import settings
    from app.queue import light_queue

    logger = logging.getLogger(__name__)
    target_queue = queue or light_queue
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.retention_sweep_interval_seconds
    )
    if interval <= 0:
        raise ValueError("retention sweep interval must be positive")

    try:
        existing_job = Job.fetch(
            RETENTION_SWEEP_JOB_ID, connection=target_queue.connection
        )
        if existing_job and existing_job.get_status() in (
            "queued",
            "scheduled",
            "started",
        ):
            logger.info(
                "Periodic retention sweep job already registered (%s)",
                existing_job.id,
            )
            return existing_job
        elif existing_job:
            # Delete stale/finished/failed job so enqueue_in does not raise JobAlreadyExistsError
            existing_job.delete()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Periodic retention sweep job not found or connection failed: %s", exc
        )

    repeat_times = times if times is not None else 1_000_000
    if repeat_times <= 0:
        raise ValueError("times must be positive")

    repeat = Repeat(
        times=repeat_times,
        interval=interval,
    )
    retry_policy = retry if retry is not None else Retry(max=3, interval=[60, 180, 300])
    try:
        return target_queue.enqueue_in(
            timedelta(seconds=interval),
            run_retention_sweep,
            job_id=RETENTION_SWEEP_JOB_ID,
            job_timeout=600,
            repeat=repeat,
            retry=retry_policy,
            description="Periodic retention sweep for expired final artifacts",
        )
    except Exception as exc:
        if "already exists" in str(exc).lower():
            try:
                return Job.fetch(
                    RETENTION_SWEEP_JOB_ID, connection=target_queue.connection
                )
            except Exception as fetch_exc:  # noqa: BLE001
                logger.debug(
                    "Failed to fetch existing job after collision: %s", fetch_exc
                )
        raise
