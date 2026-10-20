"""Verify that Alembic migrations can create the test schema from scratch.

Alembic runs in-process via `alembic.command.upgrade` with a config that has
no ini file, so `migrations/env.py` skips its `logging.config.fileConfig()`
call (it guards on `config_file_name is not None`) and pytest's log capture is
left untouched. Under pytest, `migrations/env.py` targets the test database
automatically because `PYTEST_VERSION` is set in the environment.

This test drops the public schema first so migrations always start from an
empty database, which would break any other test sharing that database (under
pytest-xdist in particular). It is therefore marked `destructive` and
deselected by default; run it in isolation with:

    uv run pytest -m destructive tests/test_migrations.py
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect

from app.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_test_db() -> Generator[Engine]:
    """Drop and recreate the public schema in the test database."""
    engine = create_engine(settings.testing_database_url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("DROP SCHEMA public CASCADE"))
        conn.execute(sqlalchemy.text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


@pytest.mark.destructive
def test_alembic_migrations_create_schema(fresh_test_db: Engine) -> None:
    """Alembic migrations must build the full schema on an empty test DB."""
    config = Config()
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    command.upgrade(config, "head")

    inspector = inspect(fresh_test_db)
    tables = set(inspector.get_table_names())
    expected = {
        "alembic_version",
        "approved_cuts",
        "clients",
        "events",
        "jobs",
        "reviews",
        "system_settings",
        "talks",
        "users",
    }
    missing = expected - tables
    assert not missing, f"Missing tables after alembic upgrade head: {missing}"
