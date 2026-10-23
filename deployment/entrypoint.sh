#!/bin/sh
set -eu

if [ "${WAIT_FOR_SERVICES:-true}" = "true" ]; then
    python - <<'PY'
import os
import socket
import sys
import time
from urllib.parse import urlparse

timeout = float(os.environ.get("SERVICE_WAIT_TIMEOUT", "120"))
deadline = time.monotonic() + timeout

services = [
    ("PostgreSQL", os.environ.get("POSTGRES_HOST", "postgres"),
     int(os.environ.get("POSTGRES_PORT", "5432"))),
]
redis = urlparse(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
services.append(("Redis", redis.hostname or "redis", redis.port or 6379))

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
PY
fi

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    uv run alembic upgrade head
fi

if [ "${WAIT_FOR_API:-false}" = "true" ]; then
    python - <<'PY'
import os
import sys
import time
import urllib.error
import urllib.request

timeout = float(os.environ.get("SERVICE_WAIT_TIMEOUT", "120"))
deadline = time.monotonic() + timeout
url = os.environ.get("API_HEALTH_URL", "http://api:8000/health")

while True:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if 200 <= response.status < 300:
                break
    except (OSError, urllib.error.URLError):
        pass

    if time.monotonic() >= deadline:
        print(f"Timed out waiting for API health at {url}", file=sys.stderr)
        sys.exit(1)
    print(f"Waiting for API health at {url}...")
    time.sleep(2)
PY
fi

exec "$@"
