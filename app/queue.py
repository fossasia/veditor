import redis
from rq import Queue

from app.config import settings

redis_conn = redis.from_url(settings.redis_url)

light_queue = Queue("light", connection=redis_conn)
heavy_queue = Queue("heavy", connection=redis_conn)
