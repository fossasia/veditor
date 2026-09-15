import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import hash_api_key
from app.db import SessionLocal, get_db
from app.main import app
from app.storage import INTERMEDIATE_STAGES, LocalDiskBackend, get_storage_backend


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def temp_storage(tmp_path):
    storage = LocalDiskBackend(tmp_path)
    app.dependency_overrides[get_storage_backend] = lambda: storage
    try:
        yield storage
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


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
            errors = []

            for obj in reversed(created):
                try:
                    insp = inspect(obj)
                    if insp and insp.has_identity and insp.identity:
                        obj_id = insp.identity[0]
                        if isinstance(obj, models.Job):
                            db.query(models.Job).filter(models.Job.id == obj_id).delete(
                                synchronize_session=False
                            )
                        elif isinstance(obj, models.Review):
                            db.query(models.Review).filter(
                                models.Review.id == obj_id
                            ).delete(synchronize_session=False)
                        elif isinstance(obj, models.Talk):
                            db.query(models.Job).filter(
                                models.Job.talk_id == obj_id
                            ).delete(synchronize_session=False)
                            db.query(models.Review).filter(
                                models.Review.talk_id == obj_id
                            ).delete(synchronize_session=False)
                            db.query(models.Talk).filter(
                                models.Talk.id == obj_id
                            ).delete(synchronize_session=False)
                        elif isinstance(obj, models.Client):
                            db.query(models.Client).filter(
                                models.Client.id == obj_id
                            ).delete(synchronize_session=False)
                        elif isinstance(obj, models.Event):
                            talk_ids = [
                                t[0]
                                for t in db.query(models.Talk.id)
                                .filter(models.Talk.event_id == obj_id)
                                .all()
                            ]
                            if talk_ids:
                                db.query(models.Job).filter(
                                    models.Job.talk_id.in_(talk_ids)
                                ).delete(synchronize_session=False)
                                db.query(models.Review).filter(
                                    models.Review.talk_id.in_(talk_ids)
                                ).delete(synchronize_session=False)
                                db.query(models.Talk).filter(
                                    models.Talk.id.in_(talk_ids)
                                ).delete(synchronize_session=False)
                            db.query(models.Event).filter(
                                models.Event.id == obj_id
                            ).delete(synchronize_session=False)
                        elif isinstance(obj, models.User):
                            db.query(models.Review).filter(
                                models.Review.user_id == obj_id
                            ).delete(synchronize_session=False)
                            db.query(models.Event).filter(
                                models.Event.created_by_user_id == obj_id
                            ).update(
                                {"created_by_user_id": None},
                                synchronize_session=False,
                            )
                            db.query(models.User).filter(
                                models.User.id == obj_id
                            ).delete(synchronize_session=False)
                        db.commit()
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    errors.append(exc)

            if errors:
                raise errors[0]
        except Exception:
            db.rollback()
            raise
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
    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    response = client.get("/studio", headers={"X-API-Key": api_key})
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

    # Unauthenticated browser caller redirects to login with next param
    unauth = client.get(f"/studio/talks/{talk.id}", follow_redirects=False)
    assert unauth.status_code == 302
    assert unauth.headers["location"] == f"/login?next=/studio/talks/{talk.id}"

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

    # Unauthenticated browser caller redirects to login
    assert client.get("/studio/talks/999999", follow_redirects=False).status_code == 302

    response = client.get("/studio/talks/999999", headers={"X-API-Key": api_key})
    assert response.status_code == 404


def test_media_serving(client: TestClient, db_session, temp_storage, tmp_path):
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
    try:
        temp_storage.put(f"{talk.id}/preview/preview.mp4", clip)

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
            f"/studio/media/{talk.id}/preview/preview.mp4",
            headers={"X-API-Key": api_key},
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
    finally:
        clip.unlink(missing_ok=True)


def test_talk_waveform_endpoint(client: TestClient, db_session, temp_storage, tmp_path):
    from tests.conftest import generate_clip

    event = models.Event(name=f"Waveform Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    talk = models.Talk(
        title="Waveform Talk",
        room="Auditorium",
        start=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 3, 1, 11, 0, tzinfo=UTC),
        status="preview",
        event_id=event.id,
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # 1. Unauthenticated request -> 401
    assert client.get(f"/studio/talks/{talk.id}/waveform").status_code == 401

    # 2. Authenticated but media not created yet -> returns empty peaks
    res_empty = client.get(
        f"/studio/talks/{talk.id}/waveform", headers={"X-API-Key": api_key}
    )
    assert res_empty.status_code == 200
    assert res_empty.json() == {"peaks": []}

    # 3. Create clip with audio and store as preview.mp4
    clip = generate_clip(
        1.0, has_audio=True, audio_waveform="tone", output_dir=tmp_path
    )
    try:
        temp_storage.put(f"{talk.id}/preview/preview.mp4", clip)

        # A cache miss must not decode media on the request thread.
        res = client.get(
            f"/studio/talks/{talk.id}/waveform", headers={"X-API-Key": api_key}
        )
        assert res.status_code == 200
        assert res.json() == {"peaks": []}

        # Background preview generation stores the waveform cache for later reads.
        data = {"peaks": [0.25, 1.0, 0.5]}
        temp_storage.put(
            f"{talk.id}/preview/preview.mp4.waveform.json",
            json.dumps(data).encode("utf-8"),
        )
        res_cached = client.get(
            f"/studio/talks/{talk.id}/waveform", headers={"X-API-Key": api_key}
        )
        assert res_cached.status_code == 200
        assert res_cached.json() == data
        assert "peaks" in data
        assert len(data["peaks"]) > 0
        assert all(0.0 <= p <= 1.0 for p in data["peaks"])

        # Subsequent fetches are served from cached JSON.
        res_cached_again = client.get(
            f"/studio/talks/{talk.id}/waveform", headers={"X-API-Key": api_key}
        )
        assert res_cached_again.status_code == 200
        assert res_cached_again.json() == data

        # Query with explicit category and filename
        res_explicit = client.get(
            f"/studio/talks/{talk.id}/waveform?category=preview&filename=preview.mp4",
            headers={"X-API-Key": api_key},
        )
        assert res_explicit.status_code == 200
        assert res_explicit.json() == data

        # Invalid category -> 404
        assert (
            client.get(
                f"/studio/talks/{talk.id}/waveform?category=invalid&filename=preview.mp4",
                headers={"X-API-Key": api_key},
            ).status_code
            == 404
        )
    finally:
        clip.unlink(missing_ok=True)


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


def test_talk_upload_recording(client: TestClient, db_session, temp_storage, tmp_path):
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

    clip = generate_clip(0.5, output_dir=tmp_path)
    mock_queue = None
    try:
        with (
            patch("app.routes.talks.light_queue") as mq,
            open(clip, "rb") as f_vid,
        ):
            mock_queue = mq
            res = client.post(
                f"/talks/{talk.id}/upload",
                files={"file": ("recording.mp4", f_vid, "video/mp4")},
                headers={"X-API-Key": api_key},
            )
            assert res.status_code == 202
            assert res.json()["status"] == "waiting_for_files"
            mock_queue.enqueue.assert_called_once()
            call_args = mock_queue.enqueue.call_args
            enqueued_func = (
                call_args.args[0]
                if call_args and call_args.args
                else (call_args.kwargs.get("func") if call_args else None)
            )
            assert enqueued_func is not None
            assert enqueued_func.__name__ == "job_ingest"
    finally:
        clip.unlink(missing_ok=True)
        if mock_queue and mock_queue.enqueue.called:
            call = mock_queue.enqueue.call_args
            staged_path = None
            if call and call.args and len(call.args) > 2:
                staged_path = call.args[2]
            elif call and call.kwargs:
                staged_path = (
                    call.kwargs.get("file_path")
                    or call.kwargs.get("staging_path")
                    or call.kwargs.get("staged_path")
                )
            if staged_path:
                Path(staged_path).unlink(missing_ok=True)


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
    dash_res = client.get("/studio", headers={"X-API-Key": api_key})
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


def test_sidebar_declutter_and_buttons(client: TestClient):
    """Test sidebar declutter: only Events and Talks, no status filters, toggle button present."""
    response = client.get("/studio")
    assert response.status_code == 200

    # Fixed topbar and toggle button are present
    assert 'id="app-topbar"' in response.text
    assert 'id="sidebar-toggle-btn"' in response.text
    assert 'id="sidebar-collapse-btn"' in response.text
    assert 'id="topbar-brand-link"' in response.text

    # Talks link is present
    assert 'id="nav-talks-link"' in response.text
    assert "Talks" in response.text

    # Redundant status filter links are absent from sidebar
    assert 'href="/studio?status_filter=pending_approval"' not in response.text
    assert 'href="/studio?status_filter=preview"' not in response.text
    assert 'href="/studio?status_filter=done"' not in response.text
    assert 'href="/studio?status_filter=broken"' not in response.text
    assert "REST API Docs" not in response.text


def test_studio_mode_body_class(client: TestClient, db_session):
    """Test that Studio routes (/studio, /studio/events, /studio/talks/{id}) include is-studio-mode body class."""
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
        title="Studio Mode Talk",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    # Talk studio view
    res = client.get(f"/studio/talks/{talk.id}", headers={"X-API-Key": api_key})
    assert res.status_code == 200
    assert "is-studio-mode" in res.text
    assert 'id="sidebar-toggle-btn"' in res.text

    # Talks dashboard view
    res_dash = client.get("/studio", headers={"X-API-Key": api_key})
    assert res_dash.status_code == 200
    assert "is-studio-mode" in res_dash.text

    # Non-studio view should not have is-studio-mode
    res_login = client.get("/login")
    assert res_login.status_code == 200
    assert "is-studio-mode" not in res_login.text


def test_studio_speaker_timeline_omits_bumpers(client: TestClient, db_session):
    """When viewed with a speaker token, studio scrubber omits INTRO/OUTRO and enables speaker mode."""
    from app.security import create_sso_token

    event = models.Event(name=f"Event {uuid.uuid4().hex}")
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Speaker Talk",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=45),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    speaker_token = create_sso_token(
        scope_type="talk", scope_id=talk.id, role="speaker"
    )
    client.cookies.set("veditor_session", speaker_token)

    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200

    # Speaker must NOT see INTRO or OUTRO bumper blocks
    assert 'id="tl-intro"' not in res.text
    assert 'id="tl-outro"' not in res.text

    # Timeline scrubber is present
    assert 'id="timeline-track"' in res.text


def test_studio_organizer_timeline_omits_bumpers(client: TestClient, db_session):
    """When viewed by an organizer, studio scrubber also omits INTRO and OUTRO bumper blocks from timeline."""
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
        title="Organizer Talk",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=45),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    token = create_session_token(org.id, org.role)
    client.cookies.set("veditor_session", token)

    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200

    # INTRO and OUTRO bumper blocks are NOT on the timeline
    assert 'id="tl-intro"' not in res.text
    assert 'id="tl-outro"' not in res.text

    # Timeline scrubber is present
    assert 'id="timeline-track"' in res.text


def test_studio_upload_pending_state_hides_timeline(client: TestClient, db_session):
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
    # 1. Talk in waiting_for_files state (upload pending)
    pending_talk = models.Talk(
        event_id=event.id,
        title="Pending Upload Talk",
        room="Hall 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    # 2. Talk in preview state (video available)
    ready_talk = models.Talk(
        event_id=event.id,
        title="Ready Preview Talk",
        room="Hall 2",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(pending_talk)
    db_session.add(ready_talk)
    db_session.commit()

    token = create_session_token(org.id, org.role)
    client.cookies.set("veditor_session", token)

    # When upload is pending:
    res_pending = client.get(f"/studio/talks/{pending_talk.id}")
    assert res_pending.status_code == 200
    # Shows the upload dropzone box and browse button
    assert "upload-dropzone-box" in res_pending.text
    assert "Attach Recording Video" in res_pending.text
    assert 'id="btn-browse-file"' in res_pending.text
    assert 'id="video-file-input"' in res_pending.text
    # Hides timeline track, controls, and timecode bar
    assert 'id="timeline-track"' not in res_pending.text
    assert "timeline-section" not in res_pending.text
    assert "player-controls" not in res_pending.text
    assert "timecode-bar" not in res_pending.text
    assert 'id="main-video"' not in res_pending.text

    # When upload is complete / preview ready:
    res_ready = client.get(f"/studio/talks/{ready_talk.id}")
    assert res_ready.status_code == 200
    # Shows video player, timecode bar, player controls, and timeline track
    assert 'id="main-video"' in res_ready.text
    assert "timecode-bar" in res_ready.text
    assert "player-controls" in res_ready.text
    assert "timeline-section" in res_ready.text
    assert 'id="timeline-track"' in res_ready.text
    assert 'id="timeline-ticks"' in res_ready.text
    # Does NOT show upload-pending-container
    assert "upload-pending-container" not in res_ready.text
    # Does NOT show cut bounds controls (only timeline scrub track for video)
    assert 'id="tl-start-marker"' not in res_ready.text
    assert 'id="tl-end-marker"' not in res_ready.text
    assert 'id="tl-content"' not in res_ready.text
    assert 'id="btn-set-in"' not in res_ready.text
    assert "timeline-inputs-bar" not in res_ready.text

    # When bounds cutting is needed (pending_bounds or needs_work):
    bounds_talk = models.Talk(
        event_id=event.id,
        title="Bounds Cut Talk",
        room="Hall 3",
        start=now,
        end=now + timedelta(minutes=30),
        status="pending_bounds",
    )
    db_session.add(bounds_talk)
    db_session.commit()

    res_bounds = client.get(f"/studio/talks/{bounds_talk.id}")
    assert res_bounds.status_code == 200
    # Shows timeline AND cut bounds markers/inputs
    assert 'id="timeline-track"' in res_bounds.text
    assert 'id="tl-start-marker"' in res_bounds.text
    assert 'id="tl-end-marker"' in res_bounds.text
    assert 'id="tl-content"' in res_bounds.text
    assert 'id="btn-set-in"' in res_bounds.text
    assert 'id="btn-set-out"' in res_bounds.text
    assert 'id="cut-duration-badge"' in res_bounds.text
    assert 'id="btn-play-cut"' in res_bounds.text
    assert "timeline-inputs-bar" in res_bounds.text
