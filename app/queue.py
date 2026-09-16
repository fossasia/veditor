import redis
from rq import Queue

from app.config import settings

redis_conn = redis.from_url(settings.redis_url)

priority_queue = Queue("priority", connection=redis_conn)
light_queue = Queue("light", connection=redis_conn)
heavy_queue = Queue("heavy", connection=redis_conn)

# Queues in the order workers should drain them: admin-prioritized jobs first.
QUEUES: dict[str, Queue] = {
    q.name: q for q in (priority_queue, light_queue, heavy_queue)
}
