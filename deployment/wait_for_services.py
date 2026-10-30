"""Wait for PostgreSQL and Redis to accept TCP connections before startup."""

import os
import socket
import sys
import time
from urllib.parse import urlparse

timeout = float(os.environ.get("SERVICE_WAIT_TIMEOUT", "120"))
deadline = time.monotonic() + timeout

redis = urlparse(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
services = [
    (
        "PostgreSQL",
        os.environ.get("POSTGRES_HOST", "postgres"),
        int(os.environ.get("POSTGRES_PORT") or 5432),
    ),
    ("Redis", redis.hostname or "redis", redis.port or 6379),
]

while True:
    unavailable = []
    for name, host, port in services:
        try:
            with socket.create_connection((host, port), timeout=2):
                pass
        except OSError:
            unavailable.append(f"{name} ({host}:{port})")

    if not unavailable:
        break
    if time.monotonic() >= deadline:
        print(f"Timed out waiting for {', '.join(unavailable)}", file=sys.stderr)
        sys.exit(1)
    print(f"Waiting for {', '.join(unavailable)}...")
    time.sleep(2)
