#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    uv run alembic upgrade head
fi

exec "$@"
