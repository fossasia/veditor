import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import models
from app.db import SessionLocal
from app.states import advance

# Determine API base URL from environment or defaults
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def clean_db():
    """Starts from a clean DB by deleting existing records."""
    db_name = os.getenv("POSTGRES_DB", "")
    if not (db_name.endswith("_test") or os.getenv("ALLOW_SMOKE_DB_WIPE") == "1"):
        pytest.fail(
            f"Refusing to wipe database '{db_name}'. Set ALLOW_SMOKE_DB_WIPE=1 or use a *_test database."
        )

    session = SessionLocal()
    try:
        session.query(models.Review).delete()
        session.query(models.Job).delete()
        session.query(models.Talk).delete()
        session.query(models.Client).delete()
        session.query(models.Event).delete()
        session.commit()
    finally:
        session.close()


def run_bootstrap_cli():
    """Runs the bootstrap CLI to get an Event, Client, and raw API key."""
    # Run the CLI using the current python executable so it runs in the same environment.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "admin",
            "create-client",
            "--event-name",
            "SmokeEvent",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    output = result.stdout

    api_key_match = re.search(r"API Key: (\S+)", output)
    event_id_match = re.search(r"Created Event '.*' with ID (\d+)", output)

    assert api_key_match, "Failed to parse API Key from CLI output"
    assert event_id_match, "Failed to parse Event ID from CLI output"

    return api_key_match.group(1), int(event_id_match.group(1))


def test_end_to_end_smoke(clean_db):
    # 1. Clean DB is handled by the fixture

    # 2. Run bootstrap CLI
    api_key, event_id = run_bootstrap_cli()
    assert api_key
    assert event_id > 0

    # 3. Hit GET /health
    health_resp = httpx.get(f"{API_BASE_URL}/health", timeout=10.0)
    assert health_resp.status_code == 200, f"Health check failed: {health_resp.text}"
    assert health_resp.json().get("status") == "ok"

    # 4. Create a Talk via direct DB fixture
    session = SessionLocal()
    try:
        start_time = datetime.now(UTC)
        end_time = start_time + timedelta(hours=1)

        talk = models.Talk(
            event_id=event_id,
            title="Smoke Test Talk",
            room="Main Hall",
            start=start_time,
            end=end_time,
            status="waiting_for_files",
        )
        session.add(talk)
        session.commit()
        session.refresh(talk)

        talk_id = talk.id
        assert talk_id > 0
    finally:
        session.close()

    # 5. Call the ops endpoints with the API key and confirm the Talk is visible
    headers = {"X-API-Key": api_key}
    talks_resp = httpx.get(f"{API_BASE_URL}/ops/talks", headers=headers)
    assert talks_resp.status_code == 200, f"Ops talks failed: {talks_resp.text}"

    talks_data = talks_resp.json()
    assert len(talks_data) == 1
    assert talks_data[0]["id"] == talk_id
    assert talks_data[0]["title"] == "Smoke Test Talk"

    # 6. Exercise advance() through a happy-path sequence of transitions
    states = [
        "detecting",
        "pending_approval",
        "pending_bounds",
        "cutting",
        "generating_previews",
        "preview",
        "pending_intro_outro",
        "transcoding",
        "uploading",
        "done",
    ]

    for state in states:
        session = SessionLocal()
        try:
            talk = session.query(models.Talk).filter(models.Talk.id == talk_id).first()
            assert talk is not None

            # Advance state and commit
            talk = advance(talk, state)
            session.commit()
        finally:
            session.close()

        # Verify via ops endpoint
        talk_resp = httpx.get(f"{API_BASE_URL}/ops/talks/{talk_id}", headers=headers)
        assert talk_resp.status_code == 200, f"Ops talk fetch failed: {talk_resp.text}"
        assert talk_resp.json()["status"] == state
