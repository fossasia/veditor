#!/bin/sh
set -eu

if [ "${WAIT_FOR_SERVICES:-true}" = "true" ]; then
    python /app/deployment/wait_for_services.py
fi

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    uv run alembic upgrade head
fi

exec "$@"
