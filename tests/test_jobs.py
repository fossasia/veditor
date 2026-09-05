from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app import models
from app.auth import get_client
from app.db import get_db
from app.main import app

client = TestClient(app)


def test_get_job_unauthorized():
    """Requesting without API key returns 401 Unauthorized."""
    response = client.get("/jobs/1")
    assert response.status_code == 401


def test_get_job_not_found():
    """Requesting non-existent job returns 404 Not Found."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = None

    response = client.get("/jobs/999", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"

    app.dependency_overrides.clear()


def test_get_job_unauthorized_event_returns_404():
    """Job belonging to talk in unowned event must return 404 to avoid leaking existence."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[2])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(id=1, event_id=1)
    mock_job = models.Job(
        id=5,
        talk_id=1,
        kind="detect",
        status="done",
        log_path="1/logs/detect.log",
    )
    mock_job.talk = mock_talk

    mock_db.query.return_value.filter.return_value.first.return_value = mock_job

    response = client.get("/jobs/5", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"

    app.dependency_overrides.clear()


def test_get_job_success():
    """Authorized client successfully gets job status and metadata with progress_pct field."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(id=10, event_id=1)
    mock_job = models.Job(
        id=42,
        talk_id=10,
        kind="transcode",
        status="running",
        log_path="10/logs/transcode.log",
    )
    mock_job.talk = mock_talk

    mock_db.query.return_value.filter.return_value.first.return_value = mock_job

    response = client.get("/jobs/42", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 42
    assert data["talk_id"] == 10
    assert data["kind"] == "transcode"
    assert data["status"] == "running"
    assert data["log_path"] == "10/logs/transcode.log"
    assert data["progress_pct"] is None

    app.dependency_overrides.clear()


def test_get_job_success_null_log_path():
    """Queued job with null log_path returns valid JobRead schema."""
    mock_db = MagicMock()
    mock_client = models.Client(id=1, event_ids=[1])

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_talk = models.Talk(id=10, event_id=1)
    mock_job = models.Job(
        id=43,
        talk_id=10,
        kind="cut",
        status="queued",
        log_path=None,
    )
    mock_job.talk = mock_talk

    mock_db.query.return_value.filter.return_value.first.return_value = mock_job

    response = client.get("/jobs/43", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 43
    assert data["status"] == "queued"
    assert data["log_path"] is None
    assert data["progress_pct"] is None

    app.dependency_overrides.clear()
