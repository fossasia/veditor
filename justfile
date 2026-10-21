set default-list := true

# Run dev server
serve:
    fastapi dev

# Migrate DB forward
db-migrate:
    uv run alembic upgrade head

# Run RQ worker
rq-worker:
    uv run scripts/run_worker.py light heavy
