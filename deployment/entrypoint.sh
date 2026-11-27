#!/bin/sh
set -eu

log() {
    printf '%s [entrypoint] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
}

log "Booting (ENVIRONMENT=${ENVIRONMENT:-development}, RUN_MIGRATIONS=${RUN_MIGRATIONS:-false}, POSTGRES_HOST=${POSTGRES_HOST:-localhost}, DATA_DIR=${DATA_DIR:-data})"

if [ "${RUN_MIGRATIONS:-false}" = "true" ]; then
    log "Running database migrations (alembic upgrade head)..."
    if alembic upgrade head; then
        log "Database migrations completed."
    else
        status=$?
        log "ERROR: Database migrations failed (exit ${status}). Check POSTGRES_* settings and database connectivity."
        exit "$status"
    fi
fi

log "Starting command: $*"
exec "$@"
