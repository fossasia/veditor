from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.auth import get_client
from app.main import app

client = TestClient(app)


def _create_event(db: Session, name: str = "Jobs Test Event") -> models.Event:
    event = models.Event(name=name)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db: Session, event_id: int, title: str = "Jobs Test Talk"
) -> models.Talk:
    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event_id,
        title=title,
        start=now,
        end=now,
        status="waiting_for_files",
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def _create_job(
    db: Session,
    talk_id: int,
    *,
    kind: str = "transcode",
    status: str = "queued",
    **kwargs: Any,
) -> models.Job:
    job = models.Job(talk_id=talk_id, kind=kind, status=status, **kwargs)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_get_job_unauthorized() -> None:
    """Requesting without API key returns 401 Unauthorized."""
    response = client.get("/jobs/1")
    assert response.status_code == 401


def test_get_job_not_found(db_session: Session) -> None:
    """Requesting non-existent job returns 404 Not Found."""
    app.dependency_overrides[get_client] = lambda: models.Client(id=1, event_ids=[1])

    missing_id = (db_session.scalar(select(func.max(models.Job.id))) or 0) + 1
    response = client.get(f"/jobs/{missing_id}", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_get_job_unauthorized_event_returns_404(db_session: Session) -> None:
    """Job belonging to talk in unowned event must return 404 to avoid leaking existence."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    job = _create_job(
        db_session,
        talk.id,
        kind="detect",
        status="done",
        log_path=f"{talk.id}/logs/detect.log",
    )

    unowned_client = models.Client(id=1, event_ids=[event.id + 1])
    app.dependency_overrides[get_client] = lambda: unowned_client

    response = client.get(f"/jobs/{job.id}", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_get_job_success(db_session: Session) -> None:
    """Authorized client successfully gets job status and metadata with progress_pct field."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    job = _create_job(
        db_session,
        talk.id,
        kind="transcode",
        status="running",
        log_path=f"{talk.id}/logs/transcode.log",
    )

    authorized_client = models.Client(id=1, event_ids=[event.id])
    app.dependency_overrides[get_client] = lambda: authorized_client

    response = client.get(f"/jobs/{job.id}", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job.id
    assert data["talk_id"] == talk.id
    assert data["kind"] == "transcode"
    assert data["status"] == "running"
    assert data["log_path"] == f"{talk.id}/logs/transcode.log"
    assert data["progress_pct"] is None


def test_get_job_success_null_log_path(db_session: Session) -> None:
    """Queued job with null log_path returns valid JobRead schema."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    job = _create_job(db_session, talk.id, kind="cut", status="queued")

    authorized_client = models.Client(id=1, event_ids=[event.id])
    app.dependency_overrides[get_client] = lambda: authorized_client

    response = client.get(f"/jobs/{job.id}", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job.id
    assert data["status"] == "queued"
    assert data["log_path"] is None
    assert data["progress_pct"] is None
    assert data["elapsed_time"] is None
    assert data["estimated_remaining"] is None


def test_get_job_progress_and_timing_calculations(db_session: Session) -> None:
    """Verify started_at, updated_at, elapsed_time, and estimated_remaining calculation."""
    event = _create_event(db_session)
    talk = _create_talk(db_session, event.id)
    started = datetime.now(UTC) - timedelta(seconds=20)
    updated = datetime.now(UTC) - timedelta(seconds=5)

    job = _create_job(
        db_session,
        talk.id,
        kind="transcode",
        status="running",
        log_path=f"{talk.id}/logs/transcode.log",
        progress_pct=50.0,
        started_at=started,
        updated_at=updated,
    )

    authorized_client = models.Client(id=1, event_ids=[event.id])
    app.dependency_overrides[get_client] = lambda: authorized_client

    response = client.get(f"/jobs/{job.id}", headers={"X-API-Key": "valid_key"})
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job.id
    assert data["progress_pct"] == 50.0
    assert data["started_at"] is not None
    assert data["updated_at"] is not None
    assert isinstance(data["elapsed_time"], float)
    assert 19.0 <= data["elapsed_time"] <= 25.0
    assert isinstance(data["estimated_remaining"], float)
    # At 50%, remaining should be approximately equal to elapsed_time
    assert abs(data["estimated_remaining"] - data["elapsed_time"]) < 0.1


def test_get_job_estimated_remaining_edge_cases() -> None:
    """Verify estimated_remaining behavior for 0%, 100%, and null progress/started_at."""
    from datetime import UTC, datetime, timedelta

    from app.schemas import JobRead

    now = datetime.now(UTC)
    started = now - timedelta(seconds=10)

    # 1. Null started_at -> elapsed_time & estimated_remaining are None
    job_no_start = JobRead(
        id=1,
        talk_id=1,
        kind="cut",
        status="queued",
        started_at=None,
        progress_pct=50.0,
    )
    assert job_no_start.elapsed_time is None
    assert job_no_start.estimated_remaining is None

    # 2. progress_pct is None -> estimated_remaining is None
    job_none_pct = JobRead(
        id=2,
        talk_id=1,
        kind="transcode",
        status="running",
        started_at=started,
        progress_pct=None,
    )
    assert job_none_pct.elapsed_time is not None
    assert job_none_pct.estimated_remaining is None

    # 3. progress_pct is 0.0 -> estimated_remaining is None
    job_zero_pct = JobRead(
        id=3,
        talk_id=1,
        kind="transcode",
        status="running",
        started_at=started,
        progress_pct=0.0,
    )
    assert job_zero_pct.elapsed_time is not None
    assert job_zero_pct.estimated_remaining is None

    # 4. progress_pct is 100.0 -> estimated_remaining is 0.0
    job_100_pct = JobRead(
        id=4,
        talk_id=1,
        kind="transcode",
        status="done",
        started_at=started,
        updated_at=now,
        progress_pct=100.0,
    )
    assert job_100_pct.elapsed_time is not None
    assert job_100_pct.estimated_remaining == 0.0


def test_linear_extrapolation_accuracy() -> None:
    """Verify linear extrapolation accuracy across various progress percentages."""
    from datetime import UTC, datetime, timedelta

    from app.schemas import JobRead

    now = datetime.now(UTC)

    # At 25% progress and 10s elapsed: remaining = 10 / 0.25 * 0.75 = 30s
    started_25 = now - timedelta(seconds=10)
    job_25 = JobRead(
        id=10,
        talk_id=1,
        kind="transcode",
        status="running",
        started_at=started_25,
        progress_pct=25.0,
    )
    remaining_25 = job_25.estimated_remaining
    assert remaining_25 is not None
    assert 29.5 <= remaining_25 <= 30.5

    # At 75% progress and 30s elapsed: remaining = 30 / 0.75 * 0.25 = 10s
    started_75 = now - timedelta(seconds=30)
    job_75 = JobRead(
        id=11,
        talk_id=1,
        kind="transcode",
        status="running",
        started_at=started_75,
        progress_pct=75.0,
    )
    remaining_75 = job_75.estimated_remaining
    assert remaining_75 is not None
    assert 9.5 <= remaining_75 <= 10.5


def test_terminal_job_elapsed_time_uses_updated_at() -> None:
    """Verify done and failed jobs calculate elapsed_time using updated_at instead of current time."""
    from datetime import UTC, datetime

    from app.schemas import JobRead

    started = datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)
    updated = datetime(2026, 9, 7, 10, 2, 30, tzinfo=UTC)  # 150 seconds later

    # 1. Done status with updated_at
    done_job = JobRead(
        id=20,
        talk_id=1,
        kind="cut",
        status="done",
        started_at=started,
        updated_at=updated,
        progress_pct=100.0,
    )
    assert done_job.elapsed_time == 150.0
    assert done_job.estimated_remaining == 0.0

    # 2. Failed status with updated_at
    failed_job = JobRead(
        id=21,
        talk_id=1,
        kind="transcode",
        status="failed",
        started_at=started,
        updated_at=updated,
        progress_pct=40.0,
    )
    assert failed_job.elapsed_time == 150.0
    # Failed jobs are terminal and must not report remaining time
    assert failed_job.estimated_remaining is None

    # 3. Terminal job with missing updated_at returns None (no drift)
    terminal_missing_updated = JobRead(
        id=22,
        talk_id=1,
        kind="transcode",
        status="failed",
        started_at=started,
        updated_at=None,
        progress_pct=40.0,
    )
    assert terminal_missing_updated.elapsed_time is None
    assert terminal_missing_updated.estimated_remaining is None

    # 4. Model level elapsed_time property and started_at default
    db_queued_job = models.Job(
        talk_id=1,
        kind="cut",
        status="queued",
    )
    assert db_queued_job.started_at is None
    assert db_queued_job.elapsed_time is None
    assert db_queued_job.estimated_remaining is None

    db_done_job = models.Job(
        id=23,
        talk_id=1,
        kind="cut",
        status="done",
        started_at=started,
        updated_at=updated,
        progress_pct=100.0,
    )
    assert db_done_job.elapsed_time == 150.0

    db_broken_no_updated = models.Job(
        id=24,
        talk_id=1,
        kind="cut",
        status="broken",
        started_at=started,
        updated_at=None,
    )
    assert db_broken_no_updated.elapsed_time is None
    assert db_broken_no_updated.estimated_remaining is None
