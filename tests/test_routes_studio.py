import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.auth import hash_api_key
from app.db import Base, engine, get_db
from app.main import app
from app.security import create_session_token, create_sso_token

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


def test_unauthenticated_studio_dashboard_redirects(client: TestClient, db_session):
    """GET /studio without active session redirects to /login?next=/studio (HTTP 302)."""
    response = client.get("/studio", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/studio"


def test_unauthenticated_studio_events_redirects(client: TestClient):
    """GET /studio/events without active session redirects to /login?next=/studio/events (HTTP 302)."""
    response = client.get("/studio/events", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/studio/events"


def test_unauthenticated_studio_talk_detail_redirects(client: TestClient, db_session):
    """GET /studio/talks/{id} without active session redirects to /login?next=/studio/talks/{id} (HTTP 302)."""
    event = models.Event(name="Redirect Event")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Redirect Talk",
        room="Main",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    response = client.get(f"/studio/talks/{talk.id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/login?next=/studio/talks/{talk.id}"


def test_unauthenticated_dashboard_no_talk_data_leakage(client: TestClient, db_session):
    """Verify no talk data, event statistics, or room information is returned to unauthenticated callers."""
    event = models.Event(name="Confidential Event Secret-12345")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Super Secret Internal Keynote XYZ-999",
        room="Classified Vault Room 404",
        start=now,
        end=now + timedelta(minutes=45),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()

    # Direct 302 response has no leak
    resp_redirect = client.get("/studio", follow_redirects=False)
    assert resp_redirect.status_code == 302
    assert "Super Secret Internal Keynote" not in resp_redirect.text
    assert "Confidential Event Secret-12345" not in resp_redirect.text
    assert "Classified Vault Room 404" not in resp_redirect.text

    # Following the redirect lands on login page, which also has no talk data
    resp_followed = client.get("/studio", follow_redirects=True)
    assert resp_followed.status_code == 200
    assert "Sign In" in resp_followed.text
    assert "Super Secret Internal Keynote" not in resp_followed.text
    assert "Confidential Event Secret-12345" not in resp_followed.text
    assert "Classified Vault Room 404" not in resp_followed.text


def test_authenticated_user_studio_dashboard_returns_200(
    client: TestClient, db_session
):
    """Authenticated organizer/admin session accessing /studio returns 200."""
    admin = models.User(
        email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        role="admin",
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", token)

    response = client.get("/studio")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_api_key_studio_dashboard_returns_200(client: TestClient, db_session):
    """Machine clients sending X-API-Key continue to return HTTP 200 on /studio."""
    event = models.Event(name="API Key Event")
    db_session.add(event)
    db_session.commit()

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    response = client.get("/studio", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_api_key_cookie_studio_dashboard_returns_200(client: TestClient, db_session):
    """Machine clients with veditor_api_key cookie continue to return HTTP 200 on /studio."""
    event = models.Event(name="Cookie API Key Event")
    db_session.add(event)
    db_session.commit()

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(hashed_key=hash_api_key(api_key), event_ids=[event.id])
    db_session.add(client_model)
    db_session.commit()

    client.cookies.set("veditor_api_key", api_key)
    response = client.get("/studio")
    assert response.status_code == 200
    client.cookies.clear()


def test_sso_token_studio_dashboard_returns_200(client: TestClient, db_session):
    """Valid SSO token parameter ?sso_token=... operates and lands on studio dashboard."""
    event = models.Event(name="SSO Event")
    db_session.add(event)
    db_session.commit()

    sso_token = create_sso_token("event", event.id, "organizer")
    response = client.get(f"/studio?sso_token={sso_token}", follow_redirects=True)
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_sso_cookie_session_studio_dashboard_returns_200(
    client: TestClient, db_session
):
    """User with valid event-scoped SSO token in veditor_session cookie accesses /studio directly without query string."""
    event = models.Event(name="SSO Direct Cookie Event")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Scoped SSO Talk",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    sso_token = create_sso_token("event", event.id, "organizer")
    client.cookies.set("veditor_session", sso_token)

    response = client.get("/studio", follow_redirects=False)
    assert response.status_code == 200
    assert "Scoped SSO Talk" in response.text
    client.cookies.clear()


def test_sso_token_studio_talk_detail_returns_200(client: TestClient, db_session):
    """Valid SSO token allows viewing talk detail without redirection."""
    event = models.Event(name="SSO Talk Event")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="SSO Talk",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="preview",
    )
    db_session.add(talk)
    db_session.commit()

    sso_token = create_sso_token("talk", talk.id, "speaker")
    response = client.get(
        f"/studio/talks/{talk.id}?sso_token={sso_token}", follow_redirects=True
    )
    assert response.status_code == 200
    assert "SSO Talk" in response.text


def test_invalid_api_key_studio_dashboard_returns_401(client: TestClient):
    """Invalid API key header on /studio returns 401 and does not redirect to /login."""
    response = client.get(
        "/studio",
        headers={"X-API-Key": "invalid-secret-key"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Invalid API Key" in response.json()["detail"]


def test_invalid_api_key_cookie_studio_dashboard_redirects_to_login(client: TestClient):
    """Invalid or stale veditor_api_key cookie on /studio redirects browser to /login?next=/studio."""
    client.cookies.set("veditor_api_key", "stale_or_invalid_cookie_key")
    response = client.get("/studio", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/studio"
    client.cookies.clear()


def test_invalid_api_key_studio_talk_detail_returns_401(client: TestClient, db_session):
    """Invalid API key on /studio/talks/{id} returns 401 and does not redirect to /login."""
    event = models.Event(name="Talk Detail API Key Event")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Talk Detail Key Test",
        room="Room B",
        start=now,
        end=now + timedelta(minutes=20),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    response = client.get(
        f"/studio/talks/{talk.id}",
        headers={"X-API-Key": "invalid-key"},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "Invalid API Key" in response.json()["detail"]


def test_invalid_session_cookie_studio_talk_detail_redirects_to_login(
    client: TestClient, db_session
):
    """Caller with an invalid/expired session cookie accessing /studio/talks/{id} redirects to login."""
    event = models.Event(name="Cookie Redirect Event")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Cookie Redirect Talk",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    client.cookies.set("veditor_session", "invalid_or_expired_cookie_token")
    response = client.get(f"/studio/talks/{talk.id}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/login?next=/studio/talks/{talk.id}"
    client.cookies.clear()


def test_api_key_studio_dashboard_renders_scoped_talks(client: TestClient, db_session):
    """API key machine client on /studio sees talks for authorized events and not other events."""
    event1 = models.Event(name="Authorized Event")
    event2 = models.Event(name="Unauthorized Event")
    db_session.add_all([event1, event2])
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk1 = models.Talk(
        event_id=event1.id,
        title="Authorized Talk Title",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    talk2 = models.Talk(
        event_id=event2.id,
        title="Unauthorized Talk Title",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add_all([talk1, talk2])

    api_key = f"key_{uuid.uuid4().hex}"
    client_model = models.Client(
        hashed_key=hash_api_key(api_key),
        event_ids=[event1.id],
    )
    db_session.add(client_model)
    db_session.commit()

    response = client.get("/studio", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    assert "Authorized Talk Title" in response.text
    assert "Unauthorized Talk Title" not in response.text


def test_normal_user_accessing_studio_events_redirects_to_studio_with_error(
    client: TestClient, db_session
):
    """Normal user accessing /studio/events is redirected back to /studio with error notification."""
    user = models.User(
        email="normal_user@test.com",
        role="user",
        hashed_password="hash",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    response = client.get("/studio/events", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/studio"
    assert "httponly" in response.headers.get("set-cookie", "").lower()

    # HTTPS requests carry secure flag
    resp_https = client.get("https://testserver/studio/events", follow_redirects=False)
    assert resp_https.status_code == 302
    cookie_header = resp_https.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie_header
    assert "secure" in cookie_header

    followed = client.get("/studio/events", follow_redirects=True)
    assert followed.status_code == 200
    assert "You do not have the permission to access that page" in followed.text
    assert "alert alert-danger" in followed.text
    client.cookies.clear()


def test_admin_viewing_other_organizer_talk_renders_view_only_mode(
    client: TestClient, db_session
):
    """When an admin views a talk from another organizer's event, it renders in View-Only Mode."""
    organizer = models.User(
        email=f"org_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        role="organizer",
        is_active=True,
    )
    admin = models.User(
        email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        role="admin",
        is_active=True,
    )
    db_session.add_all([organizer, admin])
    db_session.commit()

    event = models.Event(
        name="Other Organizer Event",
        created_by_user_id=organizer.id,
    )
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Other Organizer Talk",
        room="Room A",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", token)

    response = client.get(f"/studio/talks/{talk.id}")
    assert response.status_code == 200
    html = response.text
    assert 'data-is-other-organizer="true"' in html
    assert "studio-shell is-view-only" in html
    assert "admin-view-only-banner" in html
    assert 'id="banner-mode-text"' in html
    assert (
        f"Viewing another organizer's talk ({event.name}). Editing and pipeline actions are locked."
        in html
    )
    assert "Enable Edit Mode" in html
    assert "admin-confirm-edit-modal" in html
    assert "btn-modal-confirm" in html
    # Navigation back to admin's talk page
    assert f'href="/admin/events/{event.id}"' in html
    assert '<a href="/admin">Admin Console</a>' in html
    assert '<a href="/admin/events">Events</a>' in html
    assert (
        f'<a href="/admin/events/{event.id}" class="breadcrumb-event">{event.name}</a>'
        in html
    )
    assert (
        f'<a href="/admin/events/{event.id}" class="btn btn-ghost btn-sm" id="btn-banner-back">'
        in html
    )
    client.cookies.clear()


def test_admin_viewing_own_talk_renders_editable_mode(client: TestClient, db_session):
    """When an admin views a talk from their own event, it does not enable View-Only Mode."""
    admin = models.User(
        email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        role="admin",
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    event = models.Event(
        name="Admin's Own Event",
        created_by_user_id=admin.id,
    )
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Admin's Own Talk",
        room="Room B",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", token)

    # When accessed directly from studio, back link points to /studio
    response = client.get(f"/studio/talks/{talk.id}")
    assert response.status_code == 200
    html = response.text
    assert 'data-is-other-organizer="false"' in html
    assert "is-view-only" not in html
    assert "admin-view-only-banner" not in html
    assert "admin-confirm-edit-modal" not in html
    assert '<a href="/studio" class="breadcrumb-back">&larr; Talks</a>' in html

    # When accessed with an /admin referer header, back link still points to /studio without ?from=admin
    res_referer = client.get(
        f"/studio/talks/{talk.id}", headers={"referer": f"/admin/events/{event.id}"}
    )
    assert res_referer.status_code == 200
    assert (
        '<a href="/studio" class="breadcrumb-back">&larr; Talks</a>' in res_referer.text
    )

    # When accessed from admin area (?from=admin), back link points to admin talk page
    res_admin = client.get(f"/studio/talks/{talk.id}?from=admin")
    assert res_admin.status_code == 200
    html_admin = res_admin.text
    assert '<a href="/admin">Admin Console</a>' in html_admin
    assert '<a href="/admin/events">Events</a>' in html_admin
    assert f'<a href="/admin/events/{event.id}" class="breadcrumb-event">' in html_admin

    client.cookies.clear()


def test_organizer_viewing_own_talk_renders_editable_mode(
    client: TestClient, db_session
):
    """When an organizer views their own talk, it does not enable View-Only Mode."""
    organizer = models.User(
        email=f"org_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password="hash",
        role="organizer",
        is_active=True,
    )
    db_session.add(organizer)
    db_session.commit()

    event = models.Event(
        name="Organizer Event",
        created_by_user_id=organizer.id,
    )
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Organizer Talk",
        room="Room C",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    token = create_session_token(organizer.id, organizer.role)
    client.cookies.set("veditor_session", token)

    response = client.get(f"/studio/talks/{talk.id}")
    assert response.status_code == 200
    html = response.text
    assert 'data-is-other-organizer="false"' in html
    assert "is-view-only" not in html
    assert "admin-view-only-banner" not in html
    assert "admin-confirm-edit-modal" not in html
    assert '<a href="/studio" class="breadcrumb-back">&larr; Talks</a>' in html
    client.cookies.clear()
