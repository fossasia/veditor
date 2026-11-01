set default-list := true

# Developers are expected to always activate Python environment, so `uv run` is not necessary.

# Run dev server
serve:
    fastapi dev

# Migrate DB forward
db-migrate:
    alembic upgrade head

# Run RQ worker
rq-worker:
    scripts/run_worker.py light heavy

type-check:
    mypy app migrations scripts tests
