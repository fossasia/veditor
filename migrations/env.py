from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

import os

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402
import app.models  # noqa: E402, F401

config.set_main_option("sqlalchemy.url", settings.database_url.render_as_string())

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


# ---------------------------------------------------------------------------
# Sequential revision IDs (0001, 0002, …)
# ---------------------------------------------------------------------------


def _next_rev_id() -> str:
    """Return the next zero-padded revision number based on existing files."""
    versions_dir = os.path.join(os.path.dirname(__file__), "versions")
    max_num = 0
    if os.path.isdir(versions_dir):
        for name in os.listdir(versions_dir):
            if name.endswith(".py") and not name.startswith("__"):
                try:
                    num = int(name.split("_", 1)[0])
                    max_num = max(max_num, num)
                except ValueError:
                    pass
    return f"{max_num + 1:04d}"


def process_revision_directives(context, revision, directives):
    """Replace the random hex revision ID with a sequential number."""
    if directives:
        script = directives[0]
        script.rev_id = _next_rev_id()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        process_revision_directives=process_revision_directives,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
