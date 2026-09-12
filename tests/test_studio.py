import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import hash_api_key
from app.db import SessionLocal, get_db
from app.main import app
from app.storage import INTERMEDIATE_STAGES, StorageBackend, get_storage_backend


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

    dash_css = client.get("/static/css/dashboard.css")
    assert dash_css.status_code == 200
    assert "text/css" in dash_css.headers.get("content-type", "")

    studio_css = client.get("/static/css/studio.css")
    assert studio_css.status_code == 200
    assert "text/css" in studio_css.headers.get("content-type", "")

    auth_css = client.get("/static/css/auth.css")
    assert auth_css.status_code == 200
    assert "text/css" in auth_css.headers.get("content-type", "")

    dash_js = client.get("/static/js/dashboard.js")
    assert dash_js.status_code == 200

    studio_js = client.get("/static/js/studio.js")
    assert studio_js.status_code == 200

    auth_js = client.get("/static/js/auth.js")
    assert auth_js.status_code == 200

    theme_js = client.get("/static/js/theme.js")
    assert theme_js.status_code == 200


def test_templates_have_no_inline_css_or_js():
    import re
    from pathlib import Path

    templates_dir = Path(__file__).parent.parent / "app" / "ui" / "templates"
    assert templates_dir.is_dir()

    style_attr_pattern = re.compile(r'\bstyle=["\']', re.IGNORECASE)
    style_tag_pattern = re.compile(r"<style\b", re.IGNORECASE)
    inline_script_pattern = re.compile(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
    )
    event_handler_pattern = re.compile(r'\bon[a-z]+=["\']', re.IGNORECASE)

    jinja_files = list(templates_dir.glob("*.html.jinja"))
    assert len(jinja_files) >= 5, "Expected at least 5 .html.jinja template files"

    for html_file in jinja_files:
        content = html_file.read_text(encoding="utf-8")
        assert not style_attr_pattern.search(content), (
            f"Inline style attribute found in {html_file.name}"
        )
        assert not style_tag_pattern.search(content), (
            f"<style> tag found in {html_file.name}"
        )
        assert not event_handler_pattern.search(content), (
            f"Inline event handler found in {html_file.name}"
        )

        for match in inline_script_pattern.finditer(content):
            inline_body = match.group(1).strip()
            assert not inline_body, (
                f"Inline script body found in {html_file.name}: {inline_body[:50]}..."
            )


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
    assert response.headers.get("cache-control") == "no-store"
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

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    # Unauthenticated returns 401 without query parameter guidance
    unauth = client.get(f"/studio/talks/{talk.id}")
    assert unauth.status_code == 401
    assert "api_key query param" not in unauth.text

    # Query param fallback is rejected (returns 401)
    query_param_res = client.get(f"/studio/talks/{talk.id}?api_key={api_key}")
    assert query_param_res.status_code == 401

    # Cookie auth is accepted
    client.cookies.set("veditor_api_key", api_key)
    cookie_res = client.get(f"/studio/talks/{talk.id}")
    assert cookie_res.status_code == 200
    client.cookies.clear()

    response = client.get(f"/studio/talks/{talk.id}", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    assert "text/html" in response.headers.get("content-type", "")
    assert "Test Studio Detail Talk" in response.text
    assert "Room 101" in response.text


def test_talk_studio_human_session_user_access(client: TestClient, db_session):
    from app.security import create_session_token

    org = models.User(
        email=f"org_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="hash",
        role="organizer",
    )
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    event = models.Event(name=f"Event {uuid.uuid4().hex}", created_by_user_id=org.id)
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Human Session Talk",
        room="Main Hall",
        start=now,
        end=now + timedelta(minutes=45),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # 1. Organizer owning event can access talk via session cookie
    token = create_session_token(org.id, org.role)
    client.cookies.set("veditor_session", token)
    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200
    assert "Human Session Talk" in res.text
    assert "Main Hall" in res.text

    # 2. Admin can access talk via session cookie even if created by someone else
    admin = models.User(
        email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="hash",
        role="admin",
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)

    admin_token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", admin_token)
    res_admin = client.get(f"/studio/talks/{talk.id}")
    assert res_admin.status_code == 200

    # 3. Another user who does not own the event gets 404
    other_org = models.User(
        email=f"other_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="hash",
        role="organizer",
    )
    db_session.add(other_org)
    db_session.commit()
    db_session.refresh(other_org)

    other_token = create_session_token(other_org.id, other_org.role)
    client.cookies.set("veditor_session", other_token)
    res_other = client.get(f"/studio/talks/{talk.id}")
    assert res_other.status_code == 404


def test_talk_studio_not_found(client: TestClient, db_session):
    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    # Unauthenticated returns 401
    assert client.get("/studio/talks/999999").status_code == 401

    response = client.get("/studio/talks/999999", headers={"X-API-Key": api_key})
    assert response.status_code == 404


def test_media_serving(client: TestClient, db_session, tmp_path):
    from tests.conftest import generate_clip

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
        title="Media Talk",
        room="Hall 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    clip = generate_clip(0.5, output_dir=tmp_path)
    storage: StorageBackend = app.dependency_overrides.get(
        get_storage_backend, get_storage_backend()
    )
    storage.put(f"{talk.id}/preview/preview.mp4", clip)

    # Unauthenticated returns 401
    assert client.get(f"/studio/media/{talk.id}/preview.mp4").status_code == 401

    response = client.get(
        f"/studio/media/{talk.id}/preview.mp4", headers={"X-API-Key": api_key}
    )
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    assert "video/mp4" in response.headers.get("content-type", "")

    # Categorized media route also includes no-store
    response_cat = client.get(
        f"/studio/media/{talk.id}/preview/preview.mp4", headers={"X-API-Key": api_key}
    )
    assert response_cat.status_code == 200
    assert response_cat.headers.get("cache-control") == "no-store"

    not_found = client.get(
        f"/studio/media/{talk.id}/missing.mp4", headers={"X-API-Key": api_key}
    )
    assert not_found.status_code == 404

    # Disallowed category returns 404
    disallowed = client.get(
        f"/studio/media/{talk.id}/logs/worker.log", headers={"X-API-Key": api_key}
    )
    assert disallowed.status_code == 404


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

    from tests.conftest import generate_clip

    clip = generate_clip(0.5)
    with patch("app.routes.talks.light_queue") as mock_queue, open(clip, "rb") as f_vid:
        res = client.post(
            f"/talks/{talk.id}/upload",
            files={"file": ("recording.mp4", f_vid, "video/mp4")},
            headers={"X-API-Key": api_key},
        )
        assert res.status_code == 202
        assert res.json()["status"] == "waiting_for_files"
        mock_queue.enqueue.assert_called_once()
        enqueued_func = mock_queue.enqueue.call_args[0][0]
        assert enqueued_func.__name__ == "job_ingest"


def test_import_schedule_json_list(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    test_event_id = 98765
    db_session.query(models.Talk).filter(models.Talk.event_id == test_event_id).delete()
    db_session.query(models.Event).filter(models.Event.id == test_event_id).delete()
    db_session.commit()

    schedule_payload = [
        {
            "event_id": test_event_id,
            "title": "Opening Keynote: Open Source AI Frontiers",
            "room": "Hall 1",
            "start": "2026-09-05T09:00:00Z",
            "end": "2026-09-05T09:45:00Z",
        },
        {
            "event_id": test_event_id,
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
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
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
    other_client = models.Client(hashed_key=hash_api_key(other_key), event_ids=[999999])
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

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

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
    studio_res = client.get(f"/studio/talks/{talk.id}", headers={"X-API-Key": api_key})
    assert studio_res.status_code == 200
    assert "72%" in studio_res.text
    assert "job-card" in studio_res.text
    assert "pipeline-progress-wrap" in studio_res.text


def test_import_schedule_mm_ss_duration(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    res = client.post(
        "/talks/schedule/import",
        json={
            "event_name": "Lightning Talks",
            "title": "Short Demo",
            "room": "Demo Pod",
            "duration": "01:35",
            "start": "2026-09-09T17:15:00Z",
        },
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["imported_count"] == 1

    talk = (
        db_session.query(models.Talk).filter(models.Talk.title == "Short Demo").first()
    )
    assert talk is not None
    assert (talk.end - talk.start).total_seconds() == 95.0


def test_import_schedule_suffix_units(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    res = client.post(
        "/talks/schedule/import",
        json={
            "event_name": "Lightning Talks",
            "title": "30s Clip",
            "room": "Demo Pod",
            "duration": "30s",
            "start": "2026-09-09T17:15:00Z",
        },
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    talk = db_session.query(models.Talk).filter(models.Talk.title == "30s Clip").first()
    assert talk is not None
    assert (talk.end - talk.start).total_seconds() == 30.0


def test_ui_reject_talk_cleans_up_intermediates(
    client: TestClient, db_session, fake_storage
):
    """Rejecting a talk in review removes cut/ and preview/ while preserving raw/."""
    import uuid

    from app.auth import hash_api_key

    event = models.Event(name=f"Reject Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"reject_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Talk To Reject",
        room="Hall A",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    fake_storage.put(f"{talk.id}/raw/video.mp4", b"raw video")
    for stage in INTERMEDIATE_STAGES:
        fake_storage.put(f"{talk.id}/{stage}/{stage}.mp4", f"{stage} video".encode())

    app.dependency_overrides[get_storage_backend] = lambda: fake_storage
    try:
        res = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "reject", "note": "Not acceptable"},
            headers={"X-API-Key": api_key},
        )
        assert res.status_code == 200
        assert res.json()["talk"]["status"] == "rejected"

        db_session.refresh(talk)
        assert talk.status == "rejected"

        assert fake_storage.exists(f"{talk.id}/raw/video.mp4")
        for stage in INTERMEDIATE_STAGES:
            assert not fake_storage.exists(f"{talk.id}/{stage}/{stage}.mp4")
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_ui_reject_talk_storage_delete_resilient(client: TestClient, db_session):
    """Storage deletion failure on talk rejection does not raise 500."""
    import uuid
    from unittest.mock import MagicMock

    from app.auth import hash_api_key

    event = models.Event(name=f"Reject Err Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"reject_err_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Talk To Reject Error",
        room="Hall A",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = RuntimeError("Disk unavailable")

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    try:
        res = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "reject", "note": "Rejected despite storage error"},
            headers={"X-API-Key": api_key},
        )
        assert res.status_code == 200
        assert res.json()["talk"]["status"] == "rejected"

        db_session.refresh(talk)
        assert talk.status == "rejected"
        assert mock_storage.delete.call_count == 5
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_update_talk_validations(client: TestClient, db_session):
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
        title="Valid Title",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # Blank title should fail with 400
    res_empty_title = client.patch(
        f"/talks/{talk.id}",
        json={"title": "   "},
        headers={"X-API-Key": api_key},
    )
    assert res_empty_title.status_code == 400
    assert "cannot be empty" in res_empty_title.json()["detail"]

    # End <= start should fail with 400
    res_bad_interval = client.patch(
        f"/talks/{talk.id}",
        json={"end": (now - timedelta(minutes=10)).isoformat()},
        headers={"X-API-Key": api_key},
    )
    assert res_bad_interval.status_code == 400
    assert "after start time" in res_bad_interval.json()["detail"]


def test_import_schedule_malformed_json_and_size_limit(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    # Malformed JSON body returns 400
    res_bad_json = client.post(
        "/talks/schedule/import",
        content=b"{bad json",
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    assert res_bad_json.status_code == 400

    # Oversized content returns 413
    oversized = b"x" * (11 * 1024 * 1024)
    res_oversized = client.post(
        "/talks/schedule/import",
        content=oversized,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(oversized)),
            "X-API-Key": api_key,
        },
    )
    assert res_oversized.status_code == 413


def test_import_schedule_duration_validations(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    # Negative duration in string format returns 400
    res_neg = client.post(
        "/talks/schedule/import",
        json={
            "title": "Negative Talk",
            "duration": "-30s",
        },
        headers={"X-API-Key": api_key},
    )
    assert res_neg.status_code == 400
    assert "must be positive and finite" in res_neg.json()["detail"]

    # Negative duration_seconds returns 400
    res_neg_sec = client.post(
        "/talks/schedule/import",
        json={
            "title": "Negative Sec Talk",
            "duration_seconds": -100,
        },
        headers={"X-API-Key": api_key},
    )
    assert res_neg_sec.status_code == 400
    assert "must be positive and finite" in res_neg_sec.json()["detail"]

    # End before start returns 400
    res_bad_range = client.post(
        "/talks/schedule/import",
        json={
            "title": "Backwards Talk",
            "start": "2026-09-09T17:15:00Z",
            "end": "2026-09-09T16:15:00Z",
        },
        headers={"X-API-Key": api_key},
    )
    assert res_bad_range.status_code == 400
    assert "after start time" in res_bad_range.json()["detail"]

    # Atomicity: client event_ids should not have been updated when validation failed
    db_session.refresh(client_model)
    assert client_model.event_ids == []


def test_import_schedule_preserves_event_name_and_atomic(
    client: TestClient, db_session
):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    # Successful import with event_name and talks list preserves event_name
    res = client.post(
        "/talks/schedule/import",
        json={
            "event_name": "Unique Atomic Event",
            "talks": [
                {"title": "Valid Talk 1", "room": "Room A", "duration": "30m"},
                {"title": "Valid Talk 2", "room": "Room B", "duration": "45m"},
            ],
        },
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["event_name"] == "Unique Atomic Event"
    assert data["imported_count"] == 2

    # Partial failure in batch doesn't import any talks or modify client event_ids for new event
    res_fail = client.post(
        "/talks/schedule/import",
        json={
            "event_name": "Failed Batch Event",
            "talks": [
                {"title": "Good Talk", "room": "Room A", "duration": "30m"},
                {"title": "Bad Talk", "room": "Room B", "duration": "invalid_duration"},
            ],
        },
        headers={"X-API-Key": api_key},
    )
    assert res_fail.status_code == 400
    failed_ev = (
        db_session.query(models.Event).filter_by(name="Failed Batch Event").first()
    )
    assert failed_ev is None


def test_import_schedule_end_time_only(client: TestClient, db_session):
    api_key = f"import_test_key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[])
    db_session.add(client_model)
    db_session.commit()

    target_end = "2026-09-12T18:00:00Z"
    res = client.post(
        "/talks/schedule/import",
        json={
            "title": "End Only Talk",
            "room": "Room A",
            "end": target_end,
            "duration": "30m",
        },
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200
    talk = db_session.query(models.Talk).filter_by(title="End Only Talk").first()
    assert talk is not None
    assert talk.end == datetime.fromisoformat(target_end)
    assert talk.start == talk.end - timedelta(minutes=30)


def test_delete_talk_propagates_storage_error(client: TestClient, db_session):
    """When storage fails during talk deletion, an error is raised and the record is not deleted."""
    from unittest.mock import MagicMock

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
        title="Protected Talk",
        room="Hall 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = RuntimeError("Disk failure")

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    try:
        import pytest

        with pytest.raises(RuntimeError, match="Cleanup failed"):
            client.delete(f"/talks/{talk.id}", headers={"X-API-Key": api_key})

        # Ensure talk still exists in DB
        db_session.refresh(talk)
        assert talk is not None
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)
