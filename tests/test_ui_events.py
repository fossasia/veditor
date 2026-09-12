import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, engine, get_db
from app.main import app
from app.security import create_session_token, hash_password

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
    assert response.headers["location"] == "/login"


def test_get_events_forbidden_for_user_role(client: TestClient, db_session):
    user = create_user(db_session, "viewer@test.com", "user")
    authenticate_client(client, user)

    response = client.get("/studio/events")
    assert response.status_code == 403
    assert response.json()["detail"] == "Operation requires minimum role 'organizer'"


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


def test_get_events_admin_sees_all(client: TestClient, db_session):
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
    assert "Org Event Beta" in response.text
    assert "admin@test.com" in response.text
    assert "org_events@test.com" in response.text


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
        data={"name": "FOSSASIA Summit 2026"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    event = (
        db_session.query(models.Event)
        .filter(models.Event.name == "FOSSASIA Summit 2026")
        .first()
    )
    assert event is not None
    assert event.created_by_user_id == organizer.id
    assert response.headers["location"] == f"/studio?event_id={event.id}"


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

    # 4. Admin should see both events in <select id="quick-event-name">
    admin = create_user(db_session, "qt_admin@test.com", "admin")
    authenticate_client(client, admin)
    resp_admin = client.get("/studio")
    assert '<select id="quick-event-name"' in resp_admin.text
    assert "Org1 Selectable Summit" in resp_admin.text
    assert "Org2 Private Conference" in resp_admin.text


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
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from app.storage import get_storage_backend

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
    from datetime import UTC, datetime, timedelta
    from unittest.mock import MagicMock

    from app.storage import get_storage_backend

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
    from datetime import UTC, datetime, timedelta

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

    # 3. Admin visits /studio: sees BOTH talks
    authenticate_client(client, admin)
    resp_admin = client.get("/studio")
    assert "Org1 Exclusive Talk Alpha" in resp_admin.text
    assert "Org2 Exclusive Talk Beta" in resp_admin.text

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
