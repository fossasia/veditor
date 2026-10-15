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
sudo apt install postgresql redis
```

(For other distributions, adapt the command to match your package manager and package names.)

2. Setting up the database

The VEditor project leverages the Linux ecosystem to let you set up a database in a quick and simple way.

- Create a PostgreSQL user with the same name as your Linux user:

  ```sh
  sudo -u postgres createdb -s $USER
  ```

  (`-s` means *superuser*)

- Create a database named "veditor"

  ```sh
  createdb veditor
  ```

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
uvicorn app.main:app --reload
```

or

```sh
just serve
```

7. Before modifying source code, install `git` hooks to ensure code style compliance:

```sh
pre-commit install
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
   uv sync
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

## End-to-End Smoke Test

The project includes an end-to-end smoke test that runs through the complete state flow of a Talk, covering the core requirements of Phase 1.

**Note**: The smoke test expects a live API server and will wipe the database configured by your `POSTGRES_*` env vars to ensure a clean state.
If your database name does not end with `_test`, set `ALLOW_SMOKE_DB_WIPE=1` to run it.

### Running against the Docker Stack
1. Start the complete stack:
   ```bash
   docker compose up -d --build
   ```
2. Wait for the API to be healthy (`curl http://localhost:8000/health`).
3. Run the database migrations against the containerized database:
   ```bash
   uv run alembic upgrade head
   ```
4. Run the smoke test:
   ```bash
   uv run pytest tests/test_smoke.py
   ```

### Running Native
1. Ensure your local Postgres and Redis are running and configured in `.env`.
2. Start the API natively:
   ```bash
   uv run uvicorn app.main:app
   ```
3. In a separate terminal, run the smoke test:
   ```bash
   uv run pytest tests/test_smoke.py
   ```


[uv]: https://github.com/astral-sh/uv
[just]: https://just.systems/
[fish]: https://fishshell.com/
