from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from rq import Queue

from app import models
from app.db import Base, SessionLocal, engine, get_db
from app.main import app
from app.queue import redis_conn
from app.security import create_session_token, hash_password


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db_session():
    db = SessionLocal()
    app.dependency_overrides[get_db] = lambda: db
    created = []
    orig_add = db.add

    def track_add(instance):
        created.append(instance)
        return orig_add(instance)

    db.add = track_add
    try:
        yield db
    finally:
        db.rollback()
        for obj in reversed(created):
            try:
                db.delete(obj)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        app.dependency_overrides.pop(get_db, None)
        db.close()


@pytest.fixture
def queues():
    """Isolated queues, keyed by their real names, patched into the admin routes."""
    suffix = uuid4().hex
    mapping = {
        name: Queue(f"test_{name}_{suffix}", connection=redis_conn)
        for name in ("priority_light", "priority_heavy", "light", "heavy")
    }
    with patch("app.routes.admin.QUEUES", mapping):
        yield mapping
    for q in mapping.values():
        q.empty()
        q.delete(delete_jobs=True)


def _login(client: TestClient, db, role: str) -> models.User:
    user = models.User(
        email=f"{uuid4().hex}@jobs-test.com",
        hashed_password=hash_password("Password123!"),
        role=role,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    client.cookies.set("veditor_session", create_session_token(user.id, user.role))
    return user


def _create_talk(db) -> models.Talk:
    event = models.Event(name=f"Jobs Event {uuid4().hex}")
    db.add(event)
    db.commit()
    start = datetime.now(UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Jobs Talk",
        start=start,
        end=start + timedelta(hours=1),
        status="detecting",
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def test_admin_jobs_requires_admin(db_session):
    client = TestClient(app)
    assert client.get("/admin/jobs").status_code == 401
    assert client.post("/admin/jobs/abc/prioritize").status_code == 401

    _login(client, db_session, "organizer")
    assert client.get("/admin/jobs").status_code == 403
    assert client.post("/admin/jobs/abc/prioritize").status_code == 403


def test_list_jobs_merges_pending_and_database_jobs(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")
    talk = _create_talk(db_session)

    now = datetime.now(UTC)
    running = models.Job(
        talk_id=talk.id,
        kind="transcode",
        status="running",
        progress_pct=42.0,
        started_at=now - timedelta(minutes=5),
        updated_at=now,
    )
    failed = models.Job(
        talk_id=talk.id,
        kind="detect",
        status="failed",
        started_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )
    db_session.add(running)
    db_session.add(failed)
    db_session.commit()

    pending = queues["light"].enqueue("app.tasks.job_detect", talk.id, "raw.mp4")

    res = client.get("/admin/jobs")
    assert res.status_code == 200
    rows = res.json()

    queued_row = next(r for r in rows if r["rq_job_id"] == pending.id)
    assert queued_row["source"] == "queue"
    assert queued_row["status"] == "queued"
    assert queued_row["kind"] == "detect"
    assert queued_row["talk_id"] == talk.id
    assert queued_row["can_prioritize"] is True

    running_row = next(r for r in rows if r["job_id"] == running.id)
    assert running_row["source"] == "database"
    assert running_row["queue"] == "heavy"
    assert running_row["progress_pct"] == 42.0
    assert running_row["can_prioritize"] is False

    # Default sort is newest first.
    created = [r["created_at"] for r in rows if r["created_at"] is not None]
    assert created == sorted(created, reverse=True)


def test_list_jobs_filters_and_sorting(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")
    talk = _create_talk(db_session)
    db_session.add(
        models.Job(
            talk_id=talk.id,
            kind="detect",
            status="failed",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    queues["heavy"].enqueue("app.tasks.job_transcode", talk.id)

    rows = client.get("/admin/jobs?status=queued").json()
    assert rows and all(r["status"] == "queued" for r in rows)

    rows = client.get("/admin/jobs?status=failed").json()
    assert rows and all(r["status"] == "failed" for r in rows)

    rows = client.get("/admin/jobs?queue=heavy").json()
    assert rows and all(r["queue"] == "heavy" for r in rows)

    rows = client.get("/admin/jobs?sort=status&order=asc").json()
    statuses = [r["status"] for r in rows]
    assert statuses == sorted(statuses)

    assert client.get("/admin/jobs?queue=bogus").status_code == 400
    assert client.get("/admin/jobs?sort=bogus").status_code == 422


def test_list_jobs_limit_keeps_newest_queued_jobs(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")

    jobs = [
        queues["light"].enqueue("app.tasks.job_detect", i, "x.mp4") for i in range(3)
    ]
    rows = client.get("/admin/jobs?status=queued&queue=light&limit=2").json()
    assert {r["rq_job_id"] for r in rows} == {jobs[1].id, jobs[2].id}

    rows = client.get("/admin/jobs?status=queued&queue=light&limit=2&order=asc").json()
    assert {r["rq_job_id"] for r in rows} == {jobs[0].id, jobs[1].id}


def test_list_jobs_limit_applies_requested_sort_to_database_jobs(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")
    talk = _create_talk(db_session)
    oldest = models.Job(
        talk_id=talk.id,
        kind="detect",
        status="zzz-sort-test",
        started_at=datetime(2000, 1, 1, tzinfo=UTC),
    )
    db_session.add(oldest)
    db_session.add(
        models.Job(
            talk_id=talk.id,
            kind="detect",
            status="done",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    rows = client.get("/admin/jobs?sort=status&order=desc&limit=1").json()
    assert [r["job_id"] for r in rows] == [oldest.id]


def test_prioritize_moves_pending_job_to_priority_queue(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")

    first = queues["light"].enqueue("app.tasks.job_detect", 1, "a.mp4")
    target = queues["light"].enqueue("app.tasks.job_detect", 2, "b.mp4")

    res = client.post(f"/admin/jobs/{target.id}/prioritize")
    assert res.status_code == 200
    body = res.json()
    assert body["queue"] == "priority_light"
    assert body["can_prioritize"] is False

    assert queues["light"].job_ids == [first.id]
    assert queues["priority_light"].job_ids == [target.id]
    assert queues["priority_heavy"].job_ids == []
    target.refresh()
    assert target.origin == queues["priority_light"].name
    assert target.get_status() == "queued"

    # A second click must not enqueue the job twice.
    res = client.post(f"/admin/jobs/{target.id}/prioritize")
    assert res.status_code == 409
    assert queues["priority_light"].job_ids == [target.id]


def test_prioritize_keeps_heavy_jobs_on_heavy_workers(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")

    job = queues["heavy"].enqueue("app.tasks.job_transcode", 5)
    res = client.post(f"/admin/jobs/{job.id}/prioritize")
    assert res.status_code == 200
    assert res.json()["queue"] == "priority_heavy"
    assert queues["priority_heavy"].job_ids == [job.id]
    assert queues["priority_light"].job_ids == []


def test_prioritize_failed_enqueue_leaves_job_in_source_queue(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")

    job = queues["light"].enqueue("app.tasks.job_detect", 6, "c.mp4")
    with (
        patch.object(
            queues["priority_light"], "enqueue_job", side_effect=RuntimeError("boom")
        ),
        pytest.raises(RuntimeError),
    ):
        client.post(f"/admin/jobs/{job.id}/prioritize")

    # Removal and enqueue share one transaction, so the job was not lost.
    assert queues["light"].job_ids == [job.id]
    assert queues["priority_light"].job_ids == []


def test_prioritize_rejects_missing_and_already_dequeued_jobs(db_session, queues):
    client = TestClient(app)
    _login(client, db_session, "admin")

    assert client.post(f"/admin/jobs/{uuid4().hex}/prioritize").status_code == 404

    job = queues["heavy"].enqueue("app.tasks.job_transcode", 3)
    # Simulate a worker popping the job between listing and clicking.
    queues["heavy"].remove(job)

    res = client.post(f"/admin/jobs/{job.id}/prioritize")
    assert res.status_code == 409
    assert queues["priority_heavy"].job_ids == []


def test_admin_jobs_page_access(db_session):
    client = TestClient(app)
    res = client.get("/studio/admin/jobs", follow_redirects=False)
    assert res.status_code == 302

    _login(client, db_session, "organizer")
    assert client.get("/studio/admin/jobs").status_code == 403

    _login(client, db_session, "admin")
    res = client.get("/studio/admin/jobs")
    assert res.status_code == 200
    assert "Jobs Monitor" in res.text
    assert 'id="nav-admin-jobs-link"' in res.text
