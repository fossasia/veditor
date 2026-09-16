"""RQ Worker entrypoint for VEditor.

Starts an RQ worker process listening on the specified queue(s), reading
Redis configuration from app.config.settings and eagerly importing task modules.
"""

import argparse
import multiprocessing
import sys

import redis
import redis.exceptions
from rq import Worker

from app.config import settings

# RQ drains queues in the order given, so priority queues must come first.
DEFAULT_QUEUES = ["priority_light", "priority_heavy", "light", "heavy"]


def _run_single_worker(
    queues: list[str], redis_url: str, name: str | None, burst: bool
) -> None:
    # Eagerly import task modules in the worker process so job code is loaded
    # once at worker boot rather than re-imported per job fork.
    import app.tasks  # noqa: F401
    from app.db import engine

    engine.dispose(close=False)
    redis_conn = redis.from_url(redis_url)
    worker = Worker(queues, connection=redis_conn, name=name)
    worker.work(burst=burst)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run an RQ worker for VEditor.",
    )
    parser.add_argument(
        "queues",
        nargs="*",
        default=DEFAULT_QUEUES,
        help="Queue names to listen on, in priority order (default: priority_light priority_heavy light heavy)",
    )
    parser.add_argument(
        "--burst",
        action="store_true",
        help="Run in burst mode (quit after all current jobs are processed)",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Custom worker name",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Number of worker processes to spawn (default: 1)",
    )

    args = parser.parse_args(argv)

    queues = args.queues if args.queues else DEFAULT_QUEUES

    if not settings.redis_url or not settings.redis_url.strip():
        print("Error: REDIS_URL is unset.", file=sys.stderr)
        sys.exit(1)

    try:
        redis_conn = redis.from_url(settings.redis_url)
        redis_conn.ping()
        redis_conn.close()
    except (redis.exceptions.RedisError, ValueError) as exc:
        print(
            f"Error: Could not connect to Redis at {settings.redis_url}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.concurrency > 1:
        ctx = multiprocessing.get_context("spawn")
        processes = []
        for i in range(args.concurrency):
            worker_name = f"{args.name}-{i + 1}" if args.name else None
            p = ctx.Process(
                target=_run_single_worker,
                args=(queues, settings.redis_url, worker_name, args.burst),
            )
            p.start()
            processes.append(p)
        for p in processes:
            p.join()
    else:
        _run_single_worker(queues, settings.redis_url, args.name, args.burst)


if __name__ == "__main__":
    main()
