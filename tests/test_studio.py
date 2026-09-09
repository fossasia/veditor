import io
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import hash_api_key
from app.db import SessionLocal, get_db
from app.main import app
from app.storage import StorageBackend, get_storage_backend


@pytest.fixture
def client():
    return TestClient(app)


def test_root_redirect(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/studio"


def test_static_assets(client: TestClient):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert "text/css" in css.headers.get("content-type", "")

    dash_js = client.get("/static/js/dashboard.js")
    assert dash_js.status_code == 200

    studio_js = client.get("/static/js/studio.js")
    assert studio_js.status_code == 200


@pytest.fixture
def db_session():
    from sqlalchemy import inspect

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
        try:
            db.rollback()

            for obj in created:
                try:
                    insp = inspect(obj)
                    if insp and insp.has_identity and insp.identity:
                        obj_id = insp.identity[0]
                        if isinstance(obj, models.Talk):
                            db.query(models.Job).filter(
                                models.Job.talk_id == obj_id
                            ).delete()
                            db.query(models.Review).filter(
                                models.Review.talk_id == obj_id
                            ).delete()
                            db.query(models.Talk).filter(
                                models.Talk.id == obj_id
                            ).delete()
                        elif isinstance(obj, models.Client):
                            db.query(models.Client).filter(
                                models.Client.id == obj_id
                            ).delete()
                        elif isinstance(obj, models.Event):
                            db.query(models.Event).filter(
                                models.Event.id == obj_id
                            ).delete()
                except Exception:  # noqa: BLE001, S110
                    pass
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.close()



def test_dashboard_page(client: TestClient, db_session):
    event = models.Event(name="Test Studio Dashboard Event")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Test Studio Dashboard Talk",
        room="Auditorium",
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    response = client.get("/studio")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Test Studio Dashboard Talk" in response.text
    assert "Auditorium" in response.text


def test_talk_studio_page(client: TestClient, db_session):
    event = models.Event(name="Test Studio Page Event")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Test Studio Detail Talk",
        room="Room 101",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    response = client.get(f"/studio/talks/{talk.id}")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Test Studio Detail Talk" in response.text
    assert "Room 101" in response.text


def test_talk_studio_not_found(client: TestClient):
    response = client.get("/studio/talks/999999")
    assert response.status_code == 404


def test_media_serving(client: TestClient, tmp_path):
    from tests.conftest import generate_clip

    clip = generate_clip(0.5, output_dir=tmp_path)
    storage: StorageBackend = app.dependency_overrides.get(
        get_storage_backend, get_storage_backend()
    )
    storage.put("999/preview/preview.mp4", clip)

    response = client.get("/studio/media/999/preview.mp4")
    assert response.status_code == 200
    assert "video/mp4" in response.headers.get("content-type", "")

    not_found = client.get("/studio/media/999/missing.mp4")
    assert not_found.status_code == 404


def test_talk_patch_metadata(client: TestClient, db_session):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Original Title",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # Patch talk title & room
    res = client.patch(
        f"/talks/{talk.id}",
        json={"title": "Updated Title", "room": "Room B"},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "Updated Title"
    assert data["room"] == "Room B"


def test_talk_delete_single(client: TestClient, db_session):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Delete Talk",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    res = client.delete(
        f"/talks/{talk.id}",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    assert res.json()["deleted_id"] == talk.id

    # Verify talk is deleted
    assert (
        db_session.query(models.Talk).filter(models.Talk.id == talk.id).first() is None
    )


def test_talk_bulk_delete(client: TestClient, db_session):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk1 = models.Talk(
        event_id=event.id,
        title="Bulk Talk 1",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    talk2 = models.Talk(
        event_id=event.id,
        title="Bulk Talk 2",
        room="Room B",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk1)
    db_session.add(talk2)
    db_session.commit()
    db_session.refresh(talk1)
    db_session.refresh(talk2)

    res = client.post(
        "/talks/bulk-delete",
        json={"talk_ids": [talk1.id, talk2.id]},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    assert res.json()["deleted_count"] == 2


def test_talk_upload_recording(client: TestClient, db_session):
    from unittest.mock import patch

    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Upload Recording Talk",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    fake_file = io.BytesIO(b"fake mp4 video bytes")
    with patch("app.routes.talks.light_queue") as mock_queue:
        res = client.post(
            f"/talks/{talk.id}/upload",
            files={"file": ("recording.mp4", fake_file, "video/mp4")},
            headers={"X-API-Key": api_key},
        )
        assert res.status_code == 202
        assert res.json()["status"] == "detecting"
        mock_queue.enqueue.assert_called_once()


def test_import_schedule_json_list(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    schedule_payload = [
        {
            "event_id": 180,
            "title": "Opening Keynote: Open Source AI Frontiers",
            "room": "Hall 1",
            "start": "2026-09-05T09:00:00Z",
            "end": "2026-09-05T09:45:00Z",
        },
        {
            "event_id": 180,
            "title": "Building Scalable Video Pipelines with PyAV",
            "room": "Hall 1",
            "start": "2026-09-05T10:00:00Z",
            "end": "2026-09-05T10:45:00Z",
        },
    ]

    res = client.post(
        "/talks/schedule/import",
        json=schedule_payload,
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["imported_count"] == 2


def test_get_talk_jobs_endpoint(client: TestClient, db_session):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(
        hashed_key=hash_api_key(api_key), event_ids=[event.id]
    )
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Job Polling Test Talk",
        room="Hall 3",
        start=now,
        end=now + timedelta(minutes=30),
        status="transcoding",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    job = models.Job(
        talk_id=talk.id,
        kind="transcode",
        status="running",
        progress_pct=60.0,
        started_at=now - timedelta(seconds=15),
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # 1. Unauthenticated request must be rejected
    unauth_res = client.get(f"/talks/{talk.id}/jobs")
    assert unauth_res.status_code == 401

    # 2. Authenticated with wrong event scope must return 404
    other_key = f"other_key_{uuid.uuid4().hex}"
    other_client = models.Client(
        hashed_key=hash_api_key(other_key), event_ids=[999999]
    )
    db_session.add(other_client)
    db_session.commit()
    scope_res = client.get(
        f"/talks/{talk.id}/jobs",
        headers={"X-API-Key": other_key},
    )
    assert scope_res.status_code == 404

    # 3. Authenticated authorized request succeeds
    res = client.get(
        f"/talks/{talk.id}/jobs",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "transcoding"
    jobs = data["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["kind"] == "transcode"
    assert jobs[0]["status"] == "running"
    assert jobs[0]["progress_pct"] == 60.0
    assert jobs[0]["elapsed_time"] is not None
    assert jobs[0]["estimated_remaining"] is not None


def test_dashboard_and_studio_render_active_job_progress(
    client: TestClient, db_session
):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Progress Render Talk",
        room="Auditorium C",
        start=now,
        end=now + timedelta(minutes=45),
        status="transcoding",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    job = models.Job(
        talk_id=talk.id,
        kind="transcode",
        status="running",
        progress_pct=72.0,
        started_at=now - timedelta(seconds=30),
        updated_at=now,
    )
    db_session.add(job)
    db_session.commit()

    # 1. Dashboard should render the progress percentage badge and track
    dash_res = client.get("/studio")
    assert dash_res.status_code == 200
    assert "72%" in dash_res.text
    assert "job-progress-fill" in dash_res.text

    # 2. Studio should render the job card with progress and timing
    studio_res = client.get(f"/studio/talks/{talk.id}")
    assert studio_res.status_code == 200
    assert "72%" in studio_res.text
    assert "job-card" in studio_res.text
    assert "pipeline-progress-wrap" in studio_res.text

