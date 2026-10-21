"""Verify that Alembic migrations can create the test schema from scratch.

This test intentionally runs `alembic upgrade head` in a subprocess so that
`migrations/env.py`'s `logging.config.fileConfig()` call does not interfere
with pytest's log capture in the main test process.

Under pytest, `migrations/env.py` targets the test database automatically
(because `PYTEST_VERSION` is set). This test drops the public schema first
so migrations always start from an empty database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy import create_engine, inspect

from app.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_test_db():
    """Drop and recreate the public schema in the test database."""
    engine = create_engine(settings.testing_database_url)
    with engine.begin() as conn:
        conn.execute(sqlalchemy.text("DROP SCHEMA public CASCADE"))
        conn.execute(sqlalchemy.text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_alembic_migrations_create_schema(fresh_test_db):
    """Alembic migrations must build the full schema on an empty test DB."""
    env = os.environ.copy()
    # Simulate pytest so migrations/env.py picks the test database URL.
    env.setdefault("PYTEST_VERSION", "test")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

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
