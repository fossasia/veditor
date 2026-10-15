set default-list := true

# Run dev server
serve:
    uvicorn app.main:app --reload

# Migrate DB forward
db-migrate:
    uv run alembic upgrade head
