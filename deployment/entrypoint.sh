#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    alembic upgrade head
fi

exec "$@"
