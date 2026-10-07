import logging

import redis
from rq import Queue

from app.config import settings

logger = logging.getLogger(__name__)

redis_conn = redis.from_url(settings.redis_url)

light_queue = Queue("light", connection=redis_conn)
heavy_queue = Queue("heavy", connection=redis_conn)
priority_light_queue = Queue("priority_light", connection=redis_conn)
priority_heavy_queue = Queue("priority_heavy", connection=redis_conn)


def get_queue_for_stage(stage_kind: str, is_priority: bool = False) -> Queue:
    """Return the appropriate RQ queue for a given stage kind and priority status."""
    is_heavy = stage_kind in ("transcode",)
    if is_heavy:
        return priority_heavy_queue if is_priority else heavy_queue
    return priority_light_queue if is_priority else light_queue


def relocate_waiting_talk_job(talk_id: int, to_priority: bool) -> bool:
    """Move waiting jobs for a talk between regular and priority queues."""
    pairs = (
        [(light_queue, priority_light_queue), (heavy_queue, priority_heavy_queue)]
        if to_priority
        else [(priority_light_queue, light_queue), (priority_heavy_queue, heavy_queue)]
    )

    moved = False
    for src, dst in pairs:
        try:
            job_ids = src.get_job_ids()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to read job ids from queue %s: %s", src.name, exc)
            continue
        for jid in job_ids:
            try:
                job = src.fetch_job(jid)
                if (
                    job
                    and job.get_status() == "queued"
                    and job.args
                    and job.args[0] == talk_id
                ):
                    src.remove(jid)
                    dst.enqueue_job(job, at_front=to_priority)
                    moved = True
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to relocate job %s: %s", jid, exc)
                continue
    return moved
