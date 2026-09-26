from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, engine, get_db
from app.main import app
from app.security import create_session_token, create_sso_token, hash_password
from app.storage import get_storage_backend

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False)


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(
        bind=connection, join_transaction_mode="create_savepoint"
    )
    app.dependency_overrides[get_db] = lambda: session

    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_db, None)
        try:
            session.rollback()
            session.query(models.Talk).filter(
                models.Talk.event.has(models.Event.name.like("%Test Event%"))
            ).delete(synchronize_session=False)
            session.query(models.Event).filter(
                models.Event.name.like("%Test Event%")
            ).delete(synchronize_session=False)
            session.query(models.User).filter(
                models.User.email.like("%@example.com")
            ).delete(synchronize_session=False)
            session.commit()
        except Exception:  # noqa: BLE001
            session.rollback()
        session.close()
        transaction.rollback()
        connection.close()


def create_user(db_session, email: str, role: str) -> models.User:
    user = models.User(
        email=email,
        hashed_password=hash_password("testpass123"),
        role=role,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def authenticate_client(client: TestClient, user: models.User):
    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)


def test_get_events_unauthenticated(client: TestClient):
    response = client.get("/studio/events", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/studio/events"


def test_get_events_forbidden_for_user_role(client: TestClient, db_session):
    user = create_user(db_session, "viewer@test.com", "user")
    authenticate_client(client, user)

    response = client.get("/studio/events", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/studio"

    followed = client.get("/studio/events", follow_redirects=True)
    assert followed.status_code == 200
    assert "You do not have the permission to access that page" in followed.text
    assert "alert alert-danger" in followed.text


def test_get_events_organizer_empty(client: TestClient, db_session):
    organizer = create_user(db_session, "organizer1@test.com", "organizer")
    authenticate_client(client, organizer)

    response = client.get("/studio/events")
    assert response.status_code == 200
    assert "Events" in response.text
    assert "Events — VEditor" in response.text
    assert "Create Event" in response.text
    assert "No events found. Create your first event above." in response.text
    assert 'action="/studio/events"' in response.text
    assert 'name="name"' in response.text


def test_get_events_organizer_isolation(client: TestClient, db_session):
    org1 = create_user(db_session, "org1@test.com", "organizer")
    org2 = create_user(db_session, "org2@test.com", "organizer")

    event1 = models.Event(name="Org 1 Summit", created_by_user_id=org1.id)
    event2 = models.Event(name="Org 2 Conference", created_by_user_id=org2.id)
    db_session.add_all([event1, event2])
    db_session.commit()

    # Logged in as org1: should see event1 but NOT event2
    authenticate_client(client, org1)
    response = client.get("/studio/events")
    assert response.status_code == 200
    assert "Org 1 Summit" in response.text
    assert "Org 2 Conference" not in response.text
    assert f"/studio?event_id={event1.id}" in response.text
    assert "View Talks" in response.text


def test_get_events_admin_sees_only_own(client: TestClient, db_session):
    admin = create_user(db_session, "admin@test.com", "admin")
    org = create_user(db_session, "org_events@test.com", "organizer")

    event1 = models.Event(name="Admin Event Alpha", created_by_user_id=admin.id)
    event2 = models.Event(name="Org Event Beta", created_by_user_id=org.id)
    db_session.add_all([event1, event2])
    db_session.commit()

    authenticate_client(client, admin)
    response = client.get("/studio/events")
    assert response.status_code == 200
    assert "Admin Event Alpha" in response.text
    assert "Org Event Beta" not in response.text
    assert "admin@test.com" in response.text
    assert "org_events@test.com" not in response.text


def test_post_events_unauthenticated(client: TestClient):
    response = client.post(
        "/studio/events",
        data={"name": "Unauthorized Conf"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == "/login"


def test_post_events_forbidden_for_user_role(client: TestClient, db_session):
    user = create_user(db_session, "user_post@test.com", "user")
    authenticate_client(client, user)

    response = client.post("/studio/events", data={"name": "Forbidden Conf"})
    assert response.status_code == 403
    assert response.json()["detail"] == "Operation requires minimum role 'organizer'"


def test_post_events_empty_name(client: TestClient, db_session):
    organizer = create_user(db_session, "org_empty@test.com", "organizer")
    authenticate_client(client, organizer)

    response = client.post("/studio/events", data={"name": "   "})
    assert response.status_code == 400
    assert "Event name is required." in response.text


def test_post_events_success_organizer(client: TestClient, db_session):
    organizer = create_user(db_session, "org_creator@test.com", "organizer")
    authenticate_client(client, organizer)

    response = client.post(
        "/studio/events",
        data={"name": "FOSSASIA Summit 2026 Organizer Post"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    event = (
        db_session.query(models.Event)
        .filter(models.Event.name == "FOSSASIA Summit 2026 Organizer Post")
        .order_by(models.Event.id.desc())
        .first()
    )
    assert event is not None
    assert event.created_by_user_id == organizer.id
    assert response.headers["location"] == "/studio/events"


def test_sidebar_nav_events_link_visibility(client: TestClient, db_session):
    # 1. Unauthenticated: nav-events-link should not exist
    resp_anon = client.get("/studio")
    assert 'id="nav-events-link"' not in resp_anon.text

    # 2. User role: nav-events-link should not exist
    user = create_user(db_session, "regular_user@test.com", "user")
    authenticate_client(client, user)
    resp_user = client.get("/studio")
    assert 'id="nav-events-link"' not in resp_user.text
    assert 'id="btn-events-link"' not in resp_user.text

    # 3. Organizer role: nav-events-link should exist
    organizer = create_user(db_session, "nav_org@test.com", "organizer")
    authenticate_client(client, organizer)
    resp_org = client.get("/studio")
    assert 'id="nav-events-link"' in resp_org.text
    assert 'id="btn-events-link"' in resp_org.text

    # When on /studio/events, nav link has active class
    resp_events = client.get("/studio/events")
    assert 'id="nav-events-link"' in resp_events.text
    assert "nav-item active" in resp_events.text

    # 4. Admin role: nav-events-link and btn-events-link exist
    admin = create_user(db_session, "nav_admin@test.com", "admin")
    authenticate_client(client, admin)
    resp_admin = client.get("/studio")
    assert 'id="nav-events-link"' in resp_admin.text
    assert 'id="btn-events-link"' in resp_admin.text


def test_dashboard_quick_talk_event_selection(client: TestClient, db_session):
    org1 = create_user(db_session, "qt_org1@test.com", "organizer")
    org2 = create_user(db_session, "qt_org2@test.com", "organizer")

    # 1. Organizer with no events sees text input fallback
    authenticate_client(client, org1)
    resp_empty = client.get("/studio")
    assert '<input type="text" id="quick-event-name"' in resp_empty.text

    # 2. Add events for org1 and org2
    event1 = models.Event(name="Org1 Selectable Summit", created_by_user_id=org1.id)
    event2 = models.Event(name="Org2 Private Conference", created_by_user_id=org2.id)
    db_session.add_all([event1, event2])
    db_session.commit()

    # 3. Org1 should see <select id="quick-event-name"> with event1, but NOT event2
    resp_org1 = client.get("/studio")
    assert '<select id="quick-event-name"' in resp_org1.text
    assert "Org1 Selectable Summit" in resp_org1.text
    assert "Org2 Private Conference" not in resp_org1.text

    # 4. Admin should see no events (text input fallback) if they didn't create any
    admin = create_user(db_session, "qt_admin@test.com", "admin")
    authenticate_client(client, admin)
    resp_admin = client.get("/studio")
    assert '<input type="text" id="quick-event-name"' in resp_admin.text
    assert "Org1 Selectable Summit" not in resp_admin.text
    assert "Org2 Private Conference" not in resp_admin.text


def test_events_page_renders_clickable_name_and_action_buttons(
    client: TestClient, db_session
):
    org = create_user(db_session, "ui_actions_org@test.com", "organizer")
    event = models.Event(name="Clickable Summit", created_by_user_id=org.id)
    db_session.add(event)
    db_session.commit()

    authenticate_client(client, org)
    response = client.get("/studio/events")
    assert response.status_code == 200
    # Clickable name linking to /studio?event_id={id}
    assert f'href="/studio?event_id={event.id}"' in response.text
    assert "event-title-link" in response.text
    assert "Clickable Summit" in response.text
    # Edit & Delete buttons
    assert 'class="btn btn-ghost btn-sm btn-edit-event"' in response.text
    assert (
        'class="btn btn-ghost btn-sm btn-delete-event btn-danger-ghost"'
        in response.text
    )
    assert f'data-event-id="{event.id}"' in response.text
    # Edit Modal
    assert 'id="modal-edit-event"' in response.text
    assert 'id="edit-event-form"' in response.text
    # Delete Modal
    assert 'id="modal-delete-event"' in response.text
    assert 'id="delete-event-form"' in response.text


def test_post_events_edit_success_and_permissions(client: TestClient, db_session):
    org1 = create_user(db_session, "edit_org1@test.com", "organizer")
    org2 = create_user(db_session, "edit_org2@test.com", "organizer")
    admin = create_user(db_session, "edit_admin@test.com", "admin")

    event = models.Event(name="Original Name", created_by_user_id=org1.id)
    db_session.add(event)
    db_session.commit()

    # 1. Unauthenticated
    resp = client.post(
        f"/studio/events/{event.id}/edit",
        data={"name": "Hacked"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/login"

    # 2. Non-owning organizer gets 403
    authenticate_client(client, org2)
    resp_org2 = client.post(
        f"/studio/events/{event.id}/edit",
        data={"name": "Org2 Rename"},
    )
    assert resp_org2.status_code == 403

    # 3. Empty name gets 400
    authenticate_client(client, org1)
    resp_empty = client.post(
        f"/studio/events/{event.id}/edit",
        data={"name": "   "},
    )
    assert resp_empty.status_code == 400

    # 4. Owning organizer succeeds
    resp_owner = client.post(
        f"/studio/events/{event.id}/edit",
        data={"name": "Renamed By Owner"},
        follow_redirects=False,
    )
    assert resp_owner.status_code == 303
    assert resp_owner.headers["location"] == "/studio/events"
    db_session.refresh(event)
    assert event.name == "Renamed By Owner"

    # 5. Admin can rename any event
    authenticate_client(client, admin)
    resp_admin = client.post(
        f"/studio/events/{event.id}/edit",
        data={"name": "Renamed By Admin"},
        follow_redirects=False,
    )
    assert resp_admin.status_code == 303
    db_session.refresh(event)
    assert event.name == "Renamed By Admin"


def test_post_events_delete_success_and_permissions(client: TestClient, db_session):
    org1 = create_user(db_session, "del_org1@test.com", "organizer")
    org2 = create_user(db_session, "del_org2@test.com", "organizer")
    admin = create_user(db_session, "del_admin@test.com", "admin")

    event1 = models.Event(name="Event To Delete 1", created_by_user_id=org1.id)
    event2 = models.Event(name="Event To Delete 2", created_by_user_id=org1.id)
    db_session.add_all([event1, event2])
    db_session.commit()

    # 1. Non-owning organizer gets 403
    authenticate_client(client, org2)
    resp_org2 = client.post(f"/studio/events/{event1.id}/delete")
    assert resp_org2.status_code == 403

    # 2. Owning organizer deletes event1
    authenticate_client(client, org1)
    resp_owner = client.post(
        f"/studio/events/{event1.id}/delete",
        follow_redirects=False,
    )
    assert resp_owner.status_code == 303
    assert resp_owner.headers["location"] == "/studio/events"
    assert (
        db_session.query(models.Event).filter(models.Event.id == event1.id).first()
        is None
    )

    # 3. Admin deletes event2
    authenticate_client(client, admin)
    resp_admin = client.post(
        f"/studio/events/{event2.id}/delete",
        follow_redirects=False,
    )
    assert resp_admin.status_code == 303
    assert (
        db_session.query(models.Event).filter(models.Event.id == event2.id).first()
        is None
    )


def test_delete_studio_event_teardown_and_failure_resilience(
    client: TestClient, db_session
):
    org = create_user(db_session, "teardown_org@test.com", "organizer")
    event = models.Event(name="Teardown Event", created_by_user_id=org.id)
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Teardown Talk",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    job = models.Job(talk_id=talk.id, kind="transcode", status="queued")
    review = models.Review(talk_id=talk.id, user_id=org.id, decision="approve")
    db_session.add_all([job, review])
    db_session.commit()

    # 1. When storage fails, RuntimeError is propagated and event is NOT deleted
    mock_storage = MagicMock()
    mock_storage.delete.side_effect = RuntimeError("Storage disk unreachable")
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    authenticate_client(client, org)
    try:
        with pytest.raises(RuntimeError, match="Cleanup failed"):
            client.post(f"/studio/events/{event.id}/delete")

        # Verify event, talk, job, and review still exist
        assert (
            db_session.query(models.Event).filter(models.Event.id == event.id).first()
            is not None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk.id).first()
            is not None
        )
        assert (
            db_session.query(models.Job).filter(models.Job.id == job.id).first()
            is not None
        )
        assert (
            db_session.query(models.Review)
            .filter(models.Review.id == review.id)
            .first()
            is not None
        )
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)

    # 2. When storage succeeds, jobs/reviews/talk/event are deleted
    mock_storage_ok = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage_ok
    try:
        resp = client.post(f"/studio/events/{event.id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        mock_storage_ok.delete.assert_called_once_with(str(talk.id))

        assert (
            db_session.query(models.Event).filter(models.Event.id == event.id).first()
            is None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk.id).first()
            is None
        )
        assert (
            db_session.query(models.Job).filter(models.Job.id == job.id).first() is None
        )
        assert (
            db_session.query(models.Review)
            .filter(models.Review.id == review.id)
            .first()
            is None
        )
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_delete_studio_event_multi_talk_failure_resilience_and_retry(
    client: TestClient, db_session
):
    org = create_user(db_session, "multi_teardown_org@test.com", "organizer")
    event = models.Event(name="Multi Teardown Event", created_by_user_id=org.id)
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(tz=UTC)
    talk1 = models.Talk(
        event_id=event.id,
        title="Teardown Talk 1",
        room="Room 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    talk2 = models.Talk(
        event_id=event.id,
        title="Teardown Talk 2",
        room="Room 2",
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=1, minutes=30),
        status="preview",
    )
    db_session.add_all([talk1, talk2])
    db_session.commit()
    db_session.refresh(talk1)
    db_session.refresh(talk2)

    job1 = models.Job(talk_id=talk1.id, kind="transcode", status="queued")
    review1 = models.Review(talk_id=talk1.id, user_id=org.id, decision="approve")
    job2 = models.Job(talk_id=talk2.id, kind="transcode", status="queued")
    review2 = models.Review(talk_id=talk2.id, user_id=org.id, decision="approve")
    db_session.add_all([job1, review1, job2, review2])
    db_session.commit()

    def mock_delete(key: str):
        if key == str(talk1.id):
            return
        raise RuntimeError("Storage failure on talk 2")

    mock_storage = MagicMock()
    mock_storage.delete.side_effect = mock_delete
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    authenticate_client(client, org)
    try:
        with pytest.raises(RuntimeError, match="Cleanup failed for talk"):
            client.post(f"/studio/events/{event.id}/delete")

        # Verify event, both talks, and their jobs/reviews remain untouched in DB
        assert (
            db_session.query(models.Event).filter(models.Event.id == event.id).first()
            is not None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk1.id).first()
            is not None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk2.id).first()
            is not None
        )
        assert (
            db_session.query(models.Job).filter(models.Job.id == job1.id).first()
            is not None
        )
        assert (
            db_session.query(models.Job).filter(models.Job.id == job2.id).first()
            is not None
        )
        assert (
            db_session.query(models.Review)
            .filter(models.Review.id == review1.id)
            .first()
            is not None
        )
        assert (
            db_session.query(models.Review)
            .filter(models.Review.id == review2.id)
            .first()
            is not None
        )
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)

    # When storage succeeds on retry, all talks, jobs, reviews, and event are deleted
    mock_storage_ok = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage_ok
    try:
        resp = client.post(f"/studio/events/{event.id}/delete", follow_redirects=False)
        assert resp.status_code == 303

        assert (
            db_session.query(models.Event).filter(models.Event.id == event.id).first()
            is None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk1.id).first()
            is None
        )
        assert (
            db_session.query(models.Talk).filter(models.Talk.id == talk2.id).first()
            is None
        )
        assert (
            db_session.query(models.Job)
            .filter(models.Job.id.in_([job1.id, job2.id]))
            .count()
            == 0
        )
        assert (
            db_session.query(models.Review)
            .filter(models.Review.id.in_([review1.id, review2.id]))
            .count()
            == 0
        )
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)


def test_dashboard_talks_scoped_to_organizers_events(client: TestClient, db_session):
    org1 = create_user(db_session, "scope_org1@test.com", "organizer")
    org2 = create_user(db_session, "scope_org2@test.com", "organizer")
    admin = create_user(db_session, "scope_admin@test.com", "admin")

    event1 = models.Event(name="Summit Alpha", created_by_user_id=org1.id)
    event2 = models.Event(name="Conf Beta", created_by_user_id=org2.id)
    db_session.add_all([event1, event2])
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk1 = models.Talk(
        event_id=event1.id,
        title="Org1 Exclusive Talk Alpha",
        room="Hall 1",
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    talk2 = models.Talk(
        event_id=event2.id,
        title="Org2 Exclusive Talk Beta",
        room="Hall 2",
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    db_session.add_all([talk1, talk2])
    db_session.commit()

    # 1. Organizer 1 visits /studio: only sees Talk 1, NOT Talk 2
    authenticate_client(client, org1)
    resp_org1 = client.get("/studio")
    assert "Org1 Exclusive Talk Alpha" in resp_org1.text
    assert "Org2 Exclusive Talk Beta" not in resp_org1.text
    # Check stats count is 1
    assert "1 result" in resp_org1.text

    # 2. Organizer 2 visits /studio: only sees Talk 2, NOT Talk 1
    authenticate_client(client, org2)
    resp_org2 = client.get("/studio")
    assert "Org2 Exclusive Talk Beta" in resp_org2.text
    assert "Org1 Exclusive Talk Alpha" not in resp_org2.text
    assert "1 result" in resp_org2.text

    # 3. Admin visits /studio: sees NO talks (as they didn't create these events)
    authenticate_client(client, admin)
    resp_admin = client.get("/studio")
    assert "Org1 Exclusive Talk Alpha" not in resp_admin.text
    assert "Org2 Exclusive Talk Beta" not in resp_admin.text

    # 4. Organizer 1 filters by event2 (not owned): sees 0 talks
    authenticate_client(client, org1)
    resp_cross_filter = client.get(f"/studio?event_id={event2.id}")
    assert "Org1 Exclusive Talk Alpha" not in resp_cross_filter.text
    assert "Org2 Exclusive Talk Beta" not in resp_cross_filter.text
    assert "0 results" in resp_cross_filter.text

    # 5. Organizer 1 filters by event1 (owned): sees Talk 1
    resp_own_filter = client.get(f"/studio?event_id={event1.id}")
    assert "Org1 Exclusive Talk Alpha" in resp_own_filter.text
    assert "1 result" in resp_own_filter.text


def test_create_quick_talk_with_session_cookie(client: TestClient, db_session):
    org = create_user(db_session, "quicktalk_org@test.com", "organizer")
    event = models.Event(name="Quick Talk Conference", created_by_user_id=org.id)
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    authenticate_client(client, org)

    # Submit quick talk under this event
    res = client.post(
        "/talks/schedule/import",
        json={
            "event_id": event.id,
            "event_name": event.name,
            "title": "Modern Pipelines with PyAV",
            "room": "Room 101",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["event_id"] == event.id
    assert data["imported_count"] == 1

    # Verify talk exists in DB under event
    talk = (
        db_session.query(models.Talk)
        .filter(
            models.Talk.event_id == event.id,
            models.Talk.title == "Modern Pipelines with PyAV",
        )
        .first()
    )
    assert talk is not None
    assert talk.room == "Room 101"
    assert talk.status == "waiting_for_files"


def test_create_quick_talk_unauthorized_event(client: TestClient, db_session):
    org1 = create_user(db_session, "org1_quicktalk@test.com", "organizer")
    org2 = create_user(db_session, "org2_quicktalk@test.com", "organizer")
    event2 = models.Event(name="Org2 Conf", created_by_user_id=org2.id)
    db_session.add(event2)
    db_session.commit()
    db_session.refresh(event2)

    # Org1 tries to create talk in Org2's event
    authenticate_client(client, org1)
    res = client.post(
        "/talks/schedule/import",
        json={
            "event_id": event2.id,
            "title": "Malicious Talk",
            "room": "Room X",
        },
    )
    assert res.status_code == 403


def test_dashboard_edit_button_visible_for_organizer(client: TestClient, db_session):
    org = create_user(db_session, "org_edit_btn@test.com", "organizer")
    ev = models.Event(name="Edit Btn Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title="Edit Me",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    authenticate_client(client, org)
    res = client.get("/studio")
    assert res.status_code == 200
    assert "btn-edit-talk" in res.text


def test_dashboard_edit_button_hidden_for_sso(client: TestClient, db_session):
    org = create_user(db_session, "org_sso_btn@test.com", "organizer")
    ev = models.Event(name="SSO Btn Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title="SSO Me",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    sso_token = create_sso_token(scope_type="talk", scope_id=talk.id, role="speaker")
    client.cookies.set("veditor_session", sso_token)

    res = client.get("/studio")
    assert res.status_code == 200
    assert "btn-edit-talk" not in res.text


def test_studio_edit_button_visible_for_organizer(client: TestClient, db_session):
    org = create_user(db_session, "org_edit_studio_btn@test.com", "organizer")
    ev = models.Event(name="Edit Studio Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title="Edit Me Studio",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    authenticate_client(client, org)
    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200
    assert "btn-edit-talk-studio" in res.text


def test_studio_edit_button_hidden_for_sso(client: TestClient, db_session):
    org = create_user(db_session, "org_sso_studio_btn@test.com", "organizer")
    ev = models.Event(name="SSO Studio Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title="SSO Me Studio",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    sso_token = create_sso_token(scope_type="talk", scope_id=talk.id, role="speaker")
    client.cookies.set("veditor_session", sso_token)

    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200
    assert "btn-edit-talk-studio" not in res.text


def test_dashboard_room_attribute_escaped(client: TestClient, db_session):
    org = create_user(db_session, "org_xss@test.com", "organizer")
    ev = models.Event(name="XSS Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title='Title "With Quotes"',
        room='Room "Breakout" <script>alert(1)</script>',
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    authenticate_client(client, org)
    res = client.get("/studio")
    assert res.status_code == 200
    assert (
        'data-room="Room &#34;Breakout&#34; &lt;script&gt;alert(1)&lt;/script&gt;"'
        in res.text
    )


def test_studio_room_attribute_escaped(client: TestClient, db_session):
    org = create_user(db_session, "org_studio_xss@test.com", "organizer")
    ev = models.Event(name="Studio XSS Conf", created_by_user_id=org.id)
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    talk = models.Talk(
        event_id=ev.id,
        title='Title "With Quotes"',
        room='Room "Breakout" <script>alert(1)</script>',
        start=datetime.now(UTC),
        end=datetime.now(UTC),
    )
    db_session.add(talk)
    db_session.commit()

    authenticate_client(client, org)
    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200
    assert (
        'data-talk-room="Room &#34;Breakout&#34; &lt;script&gt;alert(1)&lt;/script&gt;"'
        in res.text
    )


def _seed_room_talks(db_session):
    from datetime import UTC, datetime, timedelta

    org = create_user(db_session, "room_org@example.com", "organizer")
    other_org = create_user(db_session, "room_other_org@example.com", "organizer")
    event = models.Event(name="Room Test Event", created_by_user_id=org.id)
    other_event = models.Event(
        name="Other Room Test Event", created_by_user_id=other_org.id
    )
    db_session.add_all([event, other_event])
    db_session.commit()

    now = datetime.now(tz=UTC)

    def make_talk(event_id: int, title: str, room: str | None, offset: int):
        return models.Talk(
            event_id=event_id,
            title=title,
            room=room,
            start=now + timedelta(hours=offset),
            end=now + timedelta(hours=offset, minutes=30),
            status="waiting_for_files",
        )

    talks = {
        "main_1": make_talk(event.id, "Main Stage Opening", "Main Stage", 0),
        "main_2": make_talk(event.id, "Main Stage Closing", "Main Stage", 1),
        "workshop": make_talk(event.id, "Workshop Hands-On", "Room B/2", 2),
        "no_room": make_talk(event.id, "Roomless Lightning Talk", None, 3),
        "other_event_main": make_talk(
            other_event.id, "Other Event Main Stage Talk", "Main Stage", 0
        ),
    }
    db_session.add_all(talks.values())
    db_session.commit()
    return org, other_org, event, other_event, talks


def _extract_href(page: str, css_class: str) -> str:
    import html
    import re

    match = re.search(
        rf'<a href="([^"]+)" class="[^"]*\b{re.escape(css_class)}\b[^"]*"', page
    )
    assert match, f"No link with class {css_class!r} found"
    return html.unescape(match.group(1))


def test_room_page_lists_only_talks_in_that_room(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)
    authenticate_client(client, org)

    resp = client.get(f"/studio/rooms/Main Stage?event_id={event.id}")
    assert resp.status_code == 200
    assert 'id="talks-scope-title">Main Stage</h1>' in resp.text
    assert "Main Stage Opening" in resp.text
    assert "Main Stage Closing" in resp.text
    assert "Workshop Hands-On" not in resp.text
    assert "Roomless Lightning Talk" not in resp.text
    assert "Other Event Main Stage Talk" not in resp.text
    assert "2 results" in resp.text
    # Stat cards count only the room's talks, not the whole workspace.
    assert '<span class="stat-value">2</span>' in resp.text
    # Filters submit back to the room page and keep the event scope.
    assert 'action="/studio/rooms/Main%20Stage"' in resp.text
    assert f'name="event_id" value="{event.id}"' in resp.text


def test_room_page_is_scoped_to_callers_events(client: TestClient, db_session):
    org, other_org, _, other_event, _ = _seed_room_talks(db_session)

    # Without an event filter, a same-named room in another organizer's event
    # must not leak into the listing.
    authenticate_client(client, org)
    resp = client.get("/studio/rooms/Main Stage")
    assert resp.status_code == 200
    assert "Main Stage Opening" in resp.text
    assert "Other Event Main Stage Talk" not in resp.text

    # Asking for an event the caller does not own yields nothing.
    resp = client.get(f"/studio/rooms/Main Stage?event_id={other_event.id}")
    assert resp.status_code == 200
    assert "Other Event Main Stage Talk" not in resp.text
    assert "0 results" in resp.text

    authenticate_client(client, other_org)
    resp = client.get(f"/studio/rooms/Main Stage?event_id={other_event.id}")
    assert "Other Event Main Stage Talk" in resp.text
    assert "Main Stage Opening" not in resp.text
    assert "1 result" in resp.text


def test_room_page_supports_status_and_search_filters(client: TestClient, db_session):
    org, _, event, _, talks = _seed_room_talks(db_session)
    talks["main_2"].status = "done"
    db_session.commit()
    authenticate_client(client, org)

    resp = client.get(
        f"/studio/rooms/Main Stage?event_id={event.id}&status_filter=done"
    )
    assert "Main Stage Closing" in resp.text
    assert "Main Stage Opening" not in resp.text

    resp = client.get(f"/studio/rooms/Main Stage?event_id={event.id}&q=opening")
    assert "Main Stage Opening" in resp.text
    assert "Main Stage Closing" not in resp.text
    clear_href = f'href="/studio/rooms/Main%20Stage?event_id={event.id}"'
    assert f'{clear_href} class="btn btn-ghost" id="clear-btn"' in resp.text


def test_event_page_shows_event_title_header(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)
    authenticate_client(client, org)

    resp = client.get(f"/studio?event_id={event.id}")
    assert resp.status_code == 200
    assert 'id="talks-scope-title">Room Test Event</h1>' in resp.text
    assert "Main Stage Opening" in resp.text
    assert "Other Event Main Stage Talk" not in resp.text

    # The unfiltered dashboard keeps the generic heading.
    resp = client.get("/studio")
    assert "talks-scope-title" not in resp.text
    assert "Conference Talks" in resp.text

    # With a second event owned by the same organizer, the event page stats
    # count only the selected event's talks.
    second = models.Event(name="Second Room Test Event", created_by_user_id=org.id)
    db_session.add(second)
    db_session.commit()
    db_session.add(
        models.Talk(
            event_id=second.id,
            title="Second Event Talk",
            room="Main Stage",
            start=datetime.now(tz=UTC),
            end=datetime.now(tz=UTC) + timedelta(minutes=30),
            status="waiting_for_files",
        )
    )
    db_session.commit()
    resp = client.get(f"/studio?event_id={event.id}")
    assert "Second Event Talk" not in resp.text
    assert '<span class="stat-value">4</span>' in resp.text
    resp = client.get("/studio")
    assert '<span class="stat-value">5</span>' in resp.text


def test_room_page_requires_authentication(client: TestClient, db_session):
    _, _, event, _, _ = _seed_room_talks(db_session)

    resp = client.get(
        f"/studio/rooms/Main Stage?event_id={event.id}", follow_redirects=False
    )
    assert resp.status_code == 302
    # The target is percent-encoded exactly once (space -> %20, not %2520).
    assert resp.headers["location"] == (
        f"/login?next=/studio/rooms/Main%20Stage%3Fevent_id%3D{event.id}"
    )

    # Without an event filter it still redirects rather than listing talks
    # from every event that has a room with this name.
    resp = client.get("/studio/rooms/Main Stage", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login?next=/studio/rooms/Main%20Stage"
    followed = client.get("/studio/rooms/Main Stage")
    assert "Main Stage Opening" not in followed.text
    assert "Other Event Main Stage Talk" not in followed.text


def test_room_page_accepts_event_slug(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)
    event.source = "eventyay"
    event.external_id = "room-test-slug-262"
    db_session.commit()
    authenticate_client(client, org)

    resp = client.get("/studio/rooms/Main Stage?event_id=room-test-slug-262")
    assert resp.status_code == 200
    assert 'id="talks-scope-title">Main Stage</h1>' in resp.text
    assert "Main Stage Opening" in resp.text
    assert "Workshop Hands-On" not in resp.text

    # An unknown slug resolves to no event rather than a 422.
    resp = client.get("/studio/rooms/Main Stage?event_id=no-such-slug")
    assert resp.status_code == 200
    assert "Main Stage Opening" not in resp.text
    assert "0 results" in resp.text


def test_event_slug_resolves_within_callers_own_events(client: TestClient, db_session):
    """external_id is unique per source, so the caller's own event wins."""
    org, other_org, event, _, _ = _seed_room_talks(db_session)
    shared_slug = "shared-slug-262"

    # Another organizer's event, imported from a different source, reuses the
    # slug and was created first, so a global lookup would find it first.
    foreign = models.Event(
        name="Foreign Slug Test Event",
        source="other-source",
        external_id=shared_slug,
        created_by_user_id=other_org.id,
    )
    db_session.add(foreign)
    db_session.commit()
    db_session.add(
        models.Talk(
            event_id=foreign.id,
            title="Foreign Slug Talk",
            room="Main Stage",
            start=datetime.now(tz=UTC),
            end=datetime.now(tz=UTC) + timedelta(minutes=30),
            status="waiting_for_files",
        )
    )
    event.source = "eventyay"
    event.external_id = shared_slug
    db_session.commit()

    authenticate_client(client, org)
    for url in (
        f"/studio?event_id={shared_slug}",
        f"/studio/rooms/Main Stage?event_id={shared_slug}",
    ):
        resp = client.get(url)
        assert resp.status_code == 200
        assert "Main Stage Opening" in resp.text
        assert "Foreign Slug Talk" not in resp.text

    # The other organizer resolves the same slug to their own event.
    authenticate_client(client, other_org)
    resp = client.get(f"/studio?event_id={shared_slug}")
    assert resp.status_code == 200
    assert "Foreign Slug Talk" in resp.text
    assert "Main Stage Opening" not in resp.text


def test_event_slug_owned_by_another_organizer_resolves_to_nothing(
    client: TestClient, db_session
):
    org, _, _, other_event, _ = _seed_room_talks(db_session)
    other_event.source = "eventyay"
    other_event.external_id = "foreign-only-slug-262"
    db_session.commit()

    authenticate_client(client, org)
    resp = client.get("/studio?event_id=foreign-only-slug-262")
    assert resp.status_code == 200
    assert "Other Event Main Stage Talk" not in resp.text
    assert "Other Room Test Event" not in resp.text
    assert "0 results" in resp.text
    assert "talks-scope-title" not in resp.text


def test_api_client_resolves_slugs_for_its_events(client: TestClient, db_session):
    from app.auth import hash_api_key

    _, _, event, other_event, _ = _seed_room_talks(db_session)
    event.source = "eventyay"
    event.external_id = "client-slug-262"
    other_event.source = "eventyay"
    other_event.external_id = "client-foreign-slug-262"
    db_session.add(
        models.Client(
            name="Room Test Client",
            hashed_key=hash_api_key("room-test-client-key-262"),
            event_ids=[event.id],
        )
    )
    db_session.commit()
    headers = {"X-API-Key": "room-test-client-key-262"}

    for url in (
        "/studio?event_id=client-slug-262",
        "/studio/rooms/Main Stage?event_id=client-slug-262",
    ):
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200
        assert 'id="talks-scope-title">' in resp.text
        assert "Main Stage Opening" in resp.text
        assert "Other Event Main Stage Talk" not in resp.text

    # A slug for an event outside the key's event_ids resolves to nothing.
    resp = client.get("/studio?event_id=client-foreign-slug-262", headers=headers)
    assert resp.status_code == 200
    assert "Other Event Main Stage Talk" not in resp.text
    assert "0 results" in resp.text


def test_talk_list_is_scoped_for_every_caller_type(client: TestClient, db_session):
    """The talk list, not just the stats, only ever contains visible events."""
    from app.auth import hash_api_key

    org, other_org, event, other_event, _ = _seed_room_talks(db_session)
    admin = create_user(db_session, "room_scope_admin@example.com", "admin")
    plain = create_user(db_session, "room_scope_user@example.com", "user")
    db_session.add(
        models.Client(
            name="Scope Test Client",
            hashed_key=hash_api_key("scope-test-client-key-262"),
            event_ids=[other_event.id],
        )
    )
    db_session.commit()
    own, foreign = "Main Stage Opening", "Other Event Main Stage Talk"

    def listing(url: str, **kwargs) -> str:
        resp = client.get(url, **kwargs)
        assert resp.status_code == 200
        return resp.text

    authenticate_client(client, org)
    for url in ("/studio", f"/studio?event_id={event.id}", "/studio/rooms/Main Stage"):
        page = listing(url)
        assert own in page and foreign not in page
    # Filtering on someone else's event lists nothing rather than their talks.
    page = listing(f"/studio?event_id={other_event.id}")
    assert own not in page and foreign not in page

    authenticate_client(client, other_org)
    page = listing("/studio")
    assert foreign in page and own not in page

    # Admins are scoped to their own events on the studio dashboard.
    authenticate_client(client, admin)
    page = listing("/studio")
    assert own not in page and foreign not in page

    authenticate_client(client, plain)
    page = listing("/studio/rooms/Main Stage")
    assert own not in page and foreign not in page

    client.cookies.clear()
    headers = {"X-API-Key": "scope-test-client-key-262"}
    page = listing("/studio", headers=headers)
    assert foreign in page and own not in page
    page = listing(f"/studio?event_id={event.id}", headers=headers)
    assert own not in page and foreign not in page


def test_room_page_rejects_invalid_api_key(client: TestClient, db_session):
    _seed_room_talks(db_session)
    resp = client.get(
        "/studio/rooms/Main Stage", headers={"X-API-Key": "not-a-real-key"}
    )
    assert resp.status_code == 401


def _login_via_redirect(client: TestClient, email: str, start_url: str):
    """Hit start_url logged out, then log in with the `next` it hands back."""
    import urllib.parse

    login_redirect = client.get(start_url, follow_redirects=False)
    assert login_redirect.status_code == 302
    location = urllib.parse.urlsplit(login_redirect.headers["location"])
    assert location.path == "/login"
    next_target = urllib.parse.parse_qs(location.query)["next"][0]

    login_page = client.get(login_redirect.headers["location"])
    assert login_page.status_code == 200

    return client.post(
        "/login",
        data={"email": email, "password": "testpass123", "next": next_target},
        follow_redirects=True,
    )


def test_login_next_returns_to_room_page(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)

    resp = _login_via_redirect(
        client, org.email, f"/studio/rooms/Main Stage?event_id={event.id}"
    )
    assert resp.status_code == 200
    assert resp.url.path == "/studio/rooms/Main Stage"
    assert resp.url.params["event_id"] == str(event.id)
    assert 'id="talks-scope-title">Main Stage</h1>' in resp.text
    assert "Main Stage Opening" in resp.text
    assert "Workshop Hands-On" not in resp.text


def test_login_next_keeps_dashboard_filters(client: TestClient, db_session):
    org, _, event, _, talks = _seed_room_talks(db_session)
    talks["main_2"].status = "done"
    db_session.commit()

    resp = _login_via_redirect(
        client, org.email, f"/studio?event_id={event.id}&status_filter=done"
    )
    assert resp.status_code == 200
    assert resp.url.path == "/studio"
    assert resp.url.params["event_id"] == str(event.id)
    assert resp.url.params["status_filter"] == "done"
    assert "Main Stage Closing" in resp.text
    assert "Main Stage Opening" not in resp.text


def _seed_delimiter_rooms(db_session, event):
    for title, room in (
        ("Ask Anything Session", "Ask Me? Anything"),
        ("Hash Room Session", "Room #5"),
    ):
        db_session.add(
            models.Talk(
                event_id=event.id,
                title=title,
                room=room,
                start=datetime.now(tz=UTC),
                end=datetime.now(tz=UTC) + timedelta(minutes=30),
                status="waiting_for_files",
            )
        )
    db_session.commit()


@pytest.mark.parametrize(
    ("room", "encoded", "title"),
    [
        ("Ask Me? Anything", "Ask%20Me%3F%20Anything", "Ask Anything Session"),
        ("Room #5", "Room%20%235", "Hash Room Session"),
    ],
)
def test_room_page_keeps_question_mark_and_hash_in_room_names(
    client: TestClient, db_session, room: str, encoded: str, title: str
):
    org, _, event, _, _ = _seed_room_talks(db_session)
    _seed_delimiter_rooms(db_session, event)
    authenticate_client(client, org)

    # The dashboard link escapes the delimiter and lands on the right room.
    dashboard = client.get(f"/studio?event_id={event.id}&q={title.split()[0]}")
    room_href = _extract_href(dashboard.text, "talk-room-link")
    assert room_href == f"/studio/rooms/{encoded}?event_id={event.id}"

    page = client.get(room_href)
    assert page.status_code == 200
    assert f'id="talks-scope-title">{room}</h1>' in page.text
    assert title in page.text
    assert "Main Stage Opening" not in page.text

    # Filtering submits back to the same room, not to a truncated path.
    assert f'action="/studio/rooms/{encoded}"' in page.text
    filtered = client.get(f"/studio/rooms/{encoded}?event_id={event.id}&q=session")
    assert title in filtered.text


@pytest.mark.parametrize(
    ("encoded", "title"),
    [
        ("Ask%20Me%3F%20Anything", "Ask Anything Session"),
        ("Room%20%235", "Hash Room Session"),
    ],
)
def test_login_next_keeps_question_mark_and_hash_in_room_names(
    client: TestClient, db_session, encoded: str, title: str
):
    org, _, event, _, _ = _seed_room_talks(db_session)
    _seed_delimiter_rooms(db_session, event)

    start_url = f"/studio/rooms/{encoded}?event_id={event.id}"
    redirect = client.get(start_url, follow_redirects=False)
    # The delimiter is escaped once inside the path and once more as part of
    # the `next` value; the space is only encoded once.
    assert redirect.headers["location"] == (
        "/login?next=/studio/rooms/"
        + encoded.replace("%20", " ").replace("%", "%25").replace(" ", "%20")
        + f"%3Fevent_id%3D{event.id}"
    )

    resp = _login_via_redirect(client, org.email, start_url)
    assert resp.status_code == 200
    assert resp.url.params["event_id"] == str(event.id)
    assert title in resp.text
    assert "Main Stage Opening" not in resp.text


def test_room_page_redirects_talk_scoped_sso_to_its_talk(
    client: TestClient, db_session
):
    from app.security import create_sso_token

    _, _, event, _, talks = _seed_room_talks(db_session)
    token = create_sso_token(
        scope_type="talk", scope_id=talks["main_1"].id, role="speaker"
    )
    client.cookies.set("veditor_session", token)

    for url in (
        "/studio/rooms/Main Stage",
        f"/studio/rooms/Main Stage?event_id={event.id}",
    ):
        resp = client.get(url, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/studio/talks/{talks['main_1'].id}"


def test_event_page_room_list_is_scoped_to_event(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)
    second = models.Event(name="Rooms Second Test Event", created_by_user_id=org.id)
    db_session.add(second)
    db_session.commit()
    db_session.add(
        models.Talk(
            event_id=second.id,
            title="Second Event Annex Talk",
            room="Annex Hall",
            start=datetime.now(tz=UTC),
            end=datetime.now(tz=UTC) + timedelta(minutes=30),
            status="waiting_for_files",
        )
    )
    db_session.commit()
    authenticate_client(client, org)

    # The Attach Room Video list only offers rooms from the selected event.
    resp = client.get(f"/studio?event_id={event.id}")
    assert '<option value="Main Stage">' in resp.text
    assert '<option value="Room B/2">' in resp.text
    assert '<option value="Annex Hall">' not in resp.text

    resp = client.get("/studio")
    assert '<option value="Annex Hall">' in resp.text


def test_dashboard_room_and_event_links_navigate(client: TestClient, db_session):
    org, _, event, _, _ = _seed_room_talks(db_session)
    authenticate_client(client, org)

    dashboard = client.get("/studio?q=Workshop")
    assert dashboard.status_code == 200

    room_href = _extract_href(dashboard.text, "talk-room-link")
    assert room_href == f"/studio/rooms/Room%20B/2?event_id={event.id}"
    room_page = client.get(room_href)
    assert room_page.status_code == 200
    assert 'id="talks-scope-title">Room B/2</h1>' in room_page.text
    assert "Workshop Hands-On" in room_page.text
    assert "Main Stage Opening" not in room_page.text
    # The room page breadcrumb links back to its event.
    assert _extract_href(room_page.text, "breadcrumb-link") == (
        f"/studio?event_id={event.id}"
    )

    event_href = _extract_href(dashboard.text, "talk-event-link")
    assert event_href == f"/studio?event_id={event.id}"
    event_page = client.get(event_href)
    assert 'id="talks-scope-title">Room Test Event</h1>' in event_page.text
    assert "Main Stage Opening" in event_page.text
    assert "Other Event Main Stage Talk" not in event_page.text


def test_studio_breadcrumb_links_to_event_and_room(client: TestClient, db_session):
    org, _, event, _, talks = _seed_room_talks(db_session)
    authenticate_client(client, org)

    studio = client.get(f"/studio/talks/{talks['main_1'].id}")
    assert studio.status_code == 200

    event_href = _extract_href(studio.text, "breadcrumb-event")
    assert event_href == f"/studio?event_id={event.id}"
    event_page = client.get(event_href)
    assert 'id="talks-scope-title">Room Test Event</h1>' in event_page.text

    room_href = _extract_href(studio.text, "breadcrumb-room")
    room_page = client.get(room_href)
    assert room_page.status_code == 200
    assert 'id="talks-scope-title">Main Stage</h1>' in room_page.text
    assert "Main Stage Closing" in room_page.text
    assert "Workshop Hands-On" not in room_page.text

    # A talk without a room renders a placeholder instead of a link.
    roomless = client.get(f"/studio/talks/{talks['no_room'].id}")
    assert 'id="breadcrumb-room-link"' not in roomless.text
    assert 'id="breadcrumb-event-link"' in roomless.text


def test_studio_breadcrumb_is_plain_text_for_talk_scoped_sso(
    client: TestClient, db_session
):
    from app.security import create_sso_token

    _, _, _, _, talks = _seed_room_talks(db_session)
    token = create_sso_token(
        scope_type="talk", scope_id=talks["main_1"].id, role="speaker"
    )
    client.cookies.set("veditor_session", token)

    studio = client.get(f"/studio/talks/{talks['main_1'].id}")
    assert studio.status_code == 200
    assert "Room Test Event" in studio.text
    assert 'id="breadcrumb-event-link"' not in studio.text
    assert 'id="breadcrumb-room-link"' not in studio.text
