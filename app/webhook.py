"""Outbound webhook delivery dispatcher and client discovery."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app import models
from app.queue import light_queue
from app.tasks import job_deliver_webhook

logger = logging.getLogger(__name__)


def _matches_event(client: models.Client, event_id: int) -> bool:
    return bool(getattr(client, "is_platform", False)) or (
        isinstance(getattr(client, "event_ids", None), list)
        and event_id in client.event_ids
    )


def get_candidate_clients(
    db: Session,
    event_id: int,
    client_id: int | None = None,
) -> list[models.Client]:
    """Find all clients configured to receive webhooks for event_id or matching client_id."""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        candidates = (
            db.query(models.Client)
            .filter(
                (
                    models.Client.event_ids.any(event_id)
                    | (models.Client.is_platform.is_(True))
                ),
                models.Client.webhook_url.is_not(None),
            )
            .all()
        )
    else:
        # Dialect fallback (e.g. SQLite tests without postgres ARRAY support)
        candidates = (
            db.query(models.Client).filter(models.Client.webhook_url.is_not(None)).all()
        )

    candidates = [c for c in candidates if _matches_event(c, event_id)]

    seen_ids = {c.id for c in candidates}

    if client_id and client_id not in seen_ids:
        client_record = (
            db.query(models.Client)
            .filter(
                models.Client.id == client_id,
                models.Client.webhook_url.is_not(None),
            )
            .first()
        )
        if client_record and _matches_event(client_record, event_id):
            candidates.append(client_record)
            seen_ids.add(client_record.id)

    # Only return clients that have both webhook_url and webhook_secret configured
    return [c for c in candidates if c.webhook_url and c.webhook_secret]


def dispatch_talk_webhook(
    event_name: str,
    talk: models.Talk,
    db: Session,
    *,
    extra_payload: dict[str, Any] | None = None,
    client_id: int | None = None,
) -> None:
    """Dispatch signed webhook notifications to candidate clients for a talk lifecycle event."""
    try:
        candidates = get_candidate_clients(db, talk.event_id, client_id=client_id)
        if not candidates:
            return

        payload_data: dict[str, Any] = {
            "event": event_name,
            "talk_id": talk.id,
            "event_id": talk.event_id,
            "external_id": talk.external_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if extra_payload:
            payload_data.update(extra_payload)

        for client in candidates:
            try:
                light_queue.enqueue(
                    job_deliver_webhook,
                    client.webhook_url,
                    client.webhook_secret,
                    payload_data,
                    job_timeout=30,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to enqueue %s webhook for talk %d to client %s: %s",
                    event_name,
                    talk.id,
                    getattr(client, "id", None),
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to dispatch %s webhook for talk %d: %s",
            event_name,
            talk.id,
            exc,
        )
