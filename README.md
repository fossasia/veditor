# VEditor

Standalone, reusable video-review/transcode pipeline using FastAPI and RQ.

## Development Setup

There are two primary ways to run the project locally: fully containerized via Docker, or completely native on your host machine.

### Option 1: Fully Containerized (Docker)

To run the entire stack (API, Worker, Postgres, Redis) in Docker:

1. Copy the environment variables:
   ```bash
   cp .env.example .env
   ```

2. Start the services:
   ```bash
   docker compose up -d --build
   ```

3. Check the API health:
   ```bash
   curl http://localhost:8000/health
   ```

#### Upgrading Existing Deployments (PostgreSQL Volume Migration)

Earlier versions did not declare a named volume for PostgreSQL, storing database files in an anonymous volume. To retain existing data when upgrading to the named `postgres_data` volume:

- **Option A (Backup & Restore - Recommended)**:
  ```bash
  # 1. Back up database before upgrading:
  docker compose exec -T postgres pg_dump -U ${POSTGRES_USER:-veditor} -d ${POSTGRES_DB:-veditor} > veditor_backup.sql

  # 2. Stop running application services:
  docker compose stop api worker-light worker-heavy

  # 3. Start PostgreSQL with the new volume:
  docker compose up -d postgres

  # 4. Restore data into the named volume:
  docker compose exec -T postgres psql -U ${POSTGRES_USER:-veditor} -d ${POSTGRES_DB:-veditor} < veditor_backup.sql

  # 5. Start all services:
  docker compose up -d
  ```

- **Option B (Direct Volume Copy)**:
  Stop the stack and copy the database directory from the old anonymous volume into the project's named volume before starting:
  ```bash
  docker compose down
  # Replace <project_name> with your compose project (defaults to folder name, e.g. veditor_postgres_data)
  docker run --rm -v <old_anonymous_volume>:/from -v <project_name>_postgres_data:/to alpine cp -a /from/. /to/
  docker compose up -d
  ```

### Option 2: Full Native Setup (Recommended for Dev)

If you prefer to run the entire stack locally without Docker (for faster reloading and easier debugging), you must install the application, `ffmpeg`, Postgres, and Redis directly on your host machine.

### Linux

It is recommended to use a relatively new Linux distribution equivalent to Ubuntu 26.04+, as it includes Python 3.14 by default.

1. Install PostgreSQL and Redis using your OS package manager. For example, on Ubuntu / Debian:

```
sudo apt install postgresql redis-server ffmpeg
```

(For other distributions, adapt the command to match your package manager and package names.)

2. Setting up the database

The VEditor project leverages the Linux ecosystem to let you set up a database in a quick and simple way.

- Create a PostgreSQL user with the same name as your Linux user:

  ```sh
  sudo -u postgres createuser -s $USER
  ```

  (`-s` means *superuser*)

- Create a database named `veditor` for development, and `test_veditor` for tests.

  ```sh
  createdb veditor
  createdb test_veditor
  ```

  Our convention is to prefix the main database name with `test_` when running tests, so `veditor` becomes `test_veditor`.
  We do this to make test databases easier to spot and harder to confuse with the main database, especially in autocomplete and shell history.
  (As a *superuser*, you can create databases without switching to the `postgres` system user.)

That's all you need to get started — no password required and no fixed PostgreSQL username needed.

If you accidentally corrupt your data later, you can delete the database with:

```sh
dropdb veditor
```

Then recreate it with `createdb veditor`.

No need to create a *.env* file if you installed PostgreSQL and Redis via your OS package manager (`apt` / `dnf` / `pacman`).
If you installed them via Docker or other methods, you may need to copy the *.env.example* file to *.env* and adjust the `POSTGRES_*` and `REDIS_URL` variables.

3. Install [`uv`][uv] and use it to create a Python virtual environment and install dependencies:

```sh
uv sync --all-groups
```

`uv` creates a Python virtual environment in the *.venv* folder. Activate it to use Python and the tools installed there:

```bash
. .venv/bin/activate
```

or

```fish
. .venv/bin/activate.fish
```

4. Optional: Install additional development tools:

- [`just`][just]: A command runner for shortcuts (see the *justfile*).
- [`fish`][fish]: A user-friendly shell with rich autocomplete. It can autocomplete the commands defined in our *justfile*.

5. Create database tables

```sh
alembic upgrade head
```

or

```sh
just db-migrate
```

6. Start the FastAPI server:

```sh
fastapi dev
```

or

```sh
just serve
```

7. Run RQ worker in another Terminal tab

```sh
./scripts/run_worker.py light heavy
```

or

```sh
just rq-worker
```

8. Before modifying source code, install `git` hooks to ensure code style compliance:

```sh
pre-commit install
```

9. Run unit tests

```sh
pytest
```

Tests that wipe or rebuild state shared with other tests (currently the
Alembic migration check) are marked `destructive` and skipped by default.
Run them on their own, never alongside the rest of the suite:

```bash
pytest -m destructive
```

### Other OSes

1. Install System Dependencies (macOS via Homebrew)
   ```bash
   brew install ffmpeg postgresql redis
   ```

2. Start background services:
   ```bash
   brew services start postgresql
   brew services start redis
   ```

3. Create the database and user (if not already set up):
   ```bash
   psql postgres -c "CREATE USER veditor WITH PASSWORD 'password';"
   psql postgres -c "CREATE DATABASE veditor OWNER veditor;"
   ```

4. Copy the environment variables:
   ```bash
   cp .env.example .env
   ```
   *(Ensure the `POSTGRES_*` and `REDIS_URL` variables point to your local native instances).*

5. Create a virtual environment and install dependencies using [uv](https://github.com/astral-sh/uv):
   ```bash
   uv venv
   source .venv/bin/activate
   uv sync --all-groups
   uv run pre-commit install
   ```

6. Start the FastAPI server natively:
   ```bash
   uv run uvicorn app.main:app --reload
   ```

7. Start the RQ worker natively (in a separate terminal):
   ```bash
   uv run python scripts/run_worker.py light heavy
   ```

8. Run the tests natively:
   ```bash
   uv run pytest
   ```

## Database Naming Convention

`POSTGRES_DB` configures the **main** database name (default: `veditor`). The application and API always connect to this database.

Tests never use `POSTGRES_DB` directly. Instead, the test database name is **derived automatically** by prefixing the main name with `test_` (see `app/config.py`: `Settings.testing_db_name`):

- `POSTGRES_DB=veditor` → tests use `test_veditor`
- `POSTGRES_DB=mydb` → tests use `test_mydb`

This means you should **not** set `POSTGRES_DB=test_veditor` yourself. Doing so would:

- point the application at the test database, and
- make the tests resolve to `test_test_veditor` (double prefix), which does not exist.

You only need to create the derived test database once (`createdb test_veditor`); the test suite creates its tables automatically. The prefix convention makes test databases easy to spot and harder to confuse with the main one in autocomplete and shell history.

## End-to-End Smoke Test

The project includes an end-to-end smoke test that runs through the complete state flow of a Talk, covering the core requirements of Phase 1.

**Note**: The smoke test expects a live API server and will wipe the database configured by your `POSTGRES_*` env vars (the database named by `POSTGRES_DB`, not the separate `test_<name>` database used by the unit-test suite) to ensure a clean state.

The whole test is **skipped by default** unless `ALLOW_SMOKE_DB_WIPE=1` is set. When it runs, it additionally refuses to wipe any database whose name does not start with `test_` unless that same variable is set. So a developer run needs either a `test_*` database plus `ALLOW_SMOKE_DB_WIPE=1`, or any database name with `ALLOW_SMOKE_DB_WIPE=1`. CI sets `ALLOW_SMOKE_DB_WIPE=1` in `.github/workflows/smoke-test.yml`.

### Running against the Docker Stack
1. Make sure `.env` exists (`cp .env.example .env`) and set `POSTGRES_DB` to a disposable database (any name is fine when `ALLOW_SMOKE_DB_WIPE=1`; otherwise prefix it with `test_`, e.g. `test_disposable`). The Postgres service auto-creates the database on first init from this variable.
2. Start the complete stack:
   ```bash
   docker compose up -d --build
   ```
3. Wait for the API to be healthy (`curl http://localhost:8000/health`).
4. Run the database migrations against the containerized database:
   ```bash
   uv run alembic upgrade head
   ```
5. Run the smoke test:
   ```bash
   ALLOW_SMOKE_DB_WIPE=1 uv run pytest tests/test_smoke.py
   ```

### Running Native

1. Ensure your local Postgres and Redis are running and configured in `.env` (typical Linux setup will not need `.env`). Set `POSTGRES_DB` to a disposable database (any name is fine when `ALLOW_SMOKE_DB_WIPE=1`; otherwise prefix it with `test_`, e.g. `test_disposable`) and `createdb` it.

2. Start the API server:
   ```bash
   uv run fastapi run
   ```
3. In a separate terminal, run the smoke test:
   ```bash
   ALLOW_SMOKE_DB_WIPE=1 uv run pytest tests/test_smoke.py
   ```


[uv]: https://github.com/astral-sh/uv
[just]: https://just.systems/
[fish]: https://fishshell.com/
