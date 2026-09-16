import redis
from rq import Queue

from app.config import settings

redis_conn = redis.from_url(settings.redis_url)

light_queue = Queue("light", connection=redis_conn)
heavy_queue = Queue("heavy", connection=redis_conn)

# Admin-prioritized jobs. Each worker class has its own priority queue so a
# prioritized heavy job (e.g. transcode) can never run on the light worker pool.
priority_light_queue = Queue("priority_light", connection=redis_conn)
priority_heavy_queue = Queue("priority_heavy", connection=redis_conn)

QUEUES: dict[str, Queue] = {
    q.name: q
    for q in (priority_light_queue, priority_heavy_queue, light_queue, heavy_queue)
}

# Standard queue name -> priority queue name its jobs are moved into.
PRIORITY_QUEUE_FOR: dict[str, str] = {
    "light": "priority_light",
    "heavy": "priority_heavy",
}
