FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install uv from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies using uv
COPY pyproject.toml uv.lock README.md ./
RUN uv venv && uv sync --no-dev --no-install-project
# Copy application files
COPY app/ app/
COPY migrations/ migrations/
COPY scripts/ scripts/
COPY alembic.ini .

# Expose the API port
EXPOSE 8000

# Default command
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
