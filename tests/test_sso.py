"""Comprehensive test suite for scoped SSO tokens and external client handoff."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

from app import models
from app.auth import (
    CurrentUser,
    check_event_access,
    check_talk_access,
    get_client,
    get_current_user,
)
from app.config import settings
from app.db import get_db
from app.main import app
from app.security import (
    create_access_token,
    create_session_token,
    create_sso_token,
    decode_sso_token,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    """Ensure dependency overrides are cleaned up after each test."""
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_db():
    db = MagicMock()

    def fake_flush():
        for call in db.add.call_args_list:
            obj = call[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

    db.flush.side_effect = fake_flush

    def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)

    db.refresh.side_effect = fake_refresh

    mock_filter = db.query.return_value.filter.return_value
    mock_filter.with_for_update.return_value = mock_filter

    return db


# ── 1. POST /events/{event_id}/sso-token ───────────────────────────────────────


def test_event_sso_token_unauthenticated():
    """Unauthenticated caller without API key is rejected with 401."""
    response = client.post("/events/1/sso-token")
    assert response.status_code == 401


def test_event_sso_token_rejects_human_session_auth():
    """Human caller with session cookie or Bearer token is rejected with 401."""
    session_cookie = create_session_token(user_id=1, role="admin")
    bearer_token = create_access_token(user_id=1, email="admin@test.com", role="admin")

    # With session cookie
    resp_cookie = client.post(
        "/events/1/sso-token", cookies={"veditor_session": session_cookie}
    )
    assert resp_cookie.status_code == 401

    # With Bearer header
    resp_bearer = client.post(
        "/events/1/sso-token", headers={"Authorization": f"Bearer {bearer_token}"}
    )
    assert resp_bearer.status_code == 401


def test_event_sso_token_event_not_found(mock_db):
    """If the requested event does not exist, return 404."""
    mock_client = models.Client(id=1, event_ids=[1, 2])
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = None

    response = client.post("/events/999/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 404
    assert "Event not found" in response.json()["detail"]


def test_event_sso_token_client_forbidden(mock_db):
    """If the calling API client is not scoped to this event, return 403."""
    mock_client = models.Client(id=1, event_ids=[2])
    mock_event = models.Event(id=1, name="Conf 2026")

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = mock_event

    response = client.post("/events/1/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 403
    assert (
        "Client is not authorized to mint an SSO token for this event"
        in response.json()["detail"]
    )


def test_event_sso_token_success(mock_db):
    """Valid API client receives an event-scoped organizer SSO token."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_event = models.Event(id=1, name="Conf 2026")

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = mock_event

    response = client.post("/events/1/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 200
    data = response.json()

    assert "token" in data
    assert data["token_type"] == "bearer"
    assert data["scope_type"] == "event"
    assert data["scope_id"] == 1
    assert data["role"] == "organizer"
    assert data["expires_in_seconds"] == settings.sso_token_expire_seconds
    assert data["url"] == f"/studio?event_id=1&sso_token={data['token']}"

    payload = decode_sso_token(data["token"])
    assert payload is not None
    assert payload["scope_type"] == "event"
    assert payload["scope_id"] == 1
    assert payload["role"] == "organizer"
    assert "user_id" not in payload
    assert "sub" not in payload
    assert "email" not in payload


# ── 2. POST /talks/{talk_id}/sso-token ─────────────────────────────────────────


def test_talk_sso_token_unauthenticated():
    """Unauthenticated caller without API key is rejected with 401."""
    response = client.post("/talks/10/sso-token")
    assert response.status_code == 401


def test_talk_sso_token_rejects_human_auth():
    """Human caller with bearer token is rejected with 401."""
    bearer_token = create_access_token(user_id=1, email="admin@test.com", role="admin")
    resp = client.post(
        "/talks/10/sso-token", headers={"Authorization": f"Bearer {bearer_token}"}
    )
    assert resp.status_code == 401


def test_talk_sso_token_talk_not_found(mock_db):
    """If the requested talk does not exist, return 404."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = None

    response = client.post("/talks/999/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 404
    assert "Talk not found" in response.json()["detail"]


def test_talk_sso_token_client_forbidden(mock_db):
    """If talk belongs to an event the client is not scoped for, return 403."""
    mock_client = models.Client(id=1, event_ids=[2])
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        title="Opening Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post("/talks/10/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 403
    assert (
        "Client is not authorized to mint an SSO token for this talk"
        in response.json()["detail"]
    )


def test_talk_sso_token_success(mock_db):
    """Valid API client receives a talk-scoped speaker SSO token."""
    mock_client = models.Client(id=1, event_ids=[1])
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        title="Opening Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )

    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post("/talks/10/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 200
    data = response.json()

    assert "token" in data
    assert data["token_type"] == "bearer"
    assert data["scope_type"] == "talk"
    assert data["scope_id"] == 10
    assert data["role"] == "speaker"
    assert data["expires_in_seconds"] == settings.sso_token_expire_seconds
    assert data["url"] == f"/studio/talks/10?sso_token={data['token']}"

    payload = decode_sso_token(data["token"])
    assert payload is not None
    assert payload["scope_type"] == "talk"
    assert payload["scope_id"] == 10
    assert payload["role"] == "speaker"
    assert "user_id" not in payload
    assert "sub" not in payload


# ── 3. Identity Resolution (get_current_user) ─────────────────────────────────


def test_get_current_user_via_sso_token_query_param(mock_db):
    """get_current_user resolves SSO token from query parameter."""
    token = create_sso_token(scope_type="event", scope_id=5, role="organizer")

    request = MagicMock()
    request.query_params.get.side_effect = lambda k: token if k == "sso_token" else None
    request.headers = {}
    request.cookies = {}

    user = get_current_user(request=request, db=mock_db)
    assert user.is_sso is True
    assert user.source == "sso"
    assert user.user_id is None
    assert user.email is None
    assert user.role == "organizer"
    assert user.scope_type == "event"
    assert user.scope_id == 5
    assert user.is_machine is False
    assert user.is_human is False


def test_get_current_user_via_x_sso_token_header(mock_db):
    """get_current_user resolves SSO token from X-SSO-Token header."""
    token = create_sso_token(scope_type="talk", scope_id=12, role="speaker")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {"X-SSO-Token": token}
    request.cookies = {}

    user = get_current_user(request=request, db=mock_db)
    assert user.is_sso is True
    assert user.source == "sso"
    assert user.role == "speaker"
    assert user.scope_type == "talk"
    assert user.scope_id == 12


def test_get_current_user_via_veditor_session_cookie(mock_db):
    """get_current_user resolves SSO token stored in veditor_session cookie."""
    token = create_sso_token(scope_type="talk", scope_id=12, role="speaker")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {}
    request.cookies = {"veditor_session": token}

    user = get_current_user(request=request, db=mock_db, cookie_token=token)
    assert user.is_sso is True
    assert user.scope_id == 12


def test_get_current_user_via_bearer_sso_token(mock_db):
    """get_current_user resolves SSO token passed in Authorization: Bearer header."""
    token = create_sso_token(scope_type="event", scope_id=7, role="organizer")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {"Authorization": f"Bearer {token}"}
    request.cookies = {}

    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = get_current_user(
        request=request,
        db=mock_db,
        bearer_creds=bearer,
    )
    assert user.is_sso is True
    assert user.scope_type == "event"
    assert user.scope_id == 7


def test_get_current_user_tampered_sso_token(mock_db):
    """Tampered or invalid SSO token immediately raises 401 Unauthorized."""
    token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    tampered = token[:-5] + "wrong"

    request = MagicMock()
    request.query_params.get.side_effect = lambda k: (
        tampered if k == "sso_token" else None
    )
    request.headers = {}
    request.cookies = {}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(request=request, db=mock_db)
    assert exc_info.value.status_code == 401
    assert "Invalid or expired SSO token" in exc_info.value.detail


# ── 4. Access Scope Checks ────────────────────────────────────────────────────


def test_check_event_access_sso(mock_db):
    mock_event = models.Event(id=1, name="Conf 1", created_by_user_id=100)
    mock_db.query.return_value.filter.return_value.first.return_value = mock_event

    # Event-scoped user
    event_user = CurrentUser(
        source="sso", role="organizer", scope_type="event", scope_id=1
    )
    assert check_event_access(1, event_user, mock_db) == mock_event

    with pytest.raises(HTTPException) as exc:
        check_event_access(2, event_user, mock_db)
    assert exc.value.status_code == 403

    # Talk-scoped user cannot access event-level endpoints
    talk_user = CurrentUser(
        source="sso", role="speaker", scope_type="talk", scope_id=10
    )
    with pytest.raises(HTTPException) as exc:
        check_event_access(1, talk_user, mock_db)
    assert exc.value.status_code == 403


def test_check_talk_access_sso(mock_db):
    talk_in_event1 = models.Talk(id=10, event_id=1)
    talk2_in_event1 = models.Talk(id=11, event_id=1)
    talk_in_event2 = models.Talk(id=20, event_id=2)

    # Event-scoped organizer can access any talk in its event
    event_user = CurrentUser(
        source="sso", role="organizer", scope_type="event", scope_id=1
    )
    assert check_talk_access(talk_in_event1, event_user, mock_db) == talk_in_event1
    assert check_talk_access(talk2_in_event1, event_user, mock_db) == talk2_in_event1

    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk_in_event2, event_user, mock_db)
    assert exc.value.status_code == 403

    # Talk-scoped speaker can ONLY access its specific talk
    talk_user = CurrentUser(
        source="sso", role="speaker", scope_type="talk", scope_id=10
    )
    assert check_talk_access(talk_in_event1, talk_user, mock_db) == talk_in_event1

    # Same event, different talk -> 403 Forbidden!
    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk2_in_event1, talk_user, mock_db)
    assert exc.value.status_code == 403

    # Different event -> 403 Forbidden
    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk_in_event2, talk_user, mock_db)
    assert exc.value.status_code == 403


# ── 5. Review Submission with SSO ─────────────────────────────────────────────


def test_sso_speaker_submits_review_success(mock_db):
    """Talk-scoped speaker submits review on target talk; user_id recorded as None."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    mock_talk = models.Talk(
        id=10,
        event_id=1,
        title="Opening Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )

    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    response = client.post(
        "/talks/10/review",
        json={"decision": "approve", "note": "Looks fantastic!"},
        cookies={"veditor_session": talk_token},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["talk"]["id"] == 10
    assert data["review"]["decision"] == "approve"
    assert data["review"]["user_id"] is None

    # Verify Review model added to DB had user_id = None
    review_added = [
        call[0][0]
        for call in mock_db.add.call_args_list
        if isinstance(call[0][0], models.Review)
    ]
    assert len(review_added) == 1
    assert review_added[0].user_id is None


def test_sso_speaker_cannot_review_other_talk(mock_db):
    """Talk-scoped speaker attempting to review another talk receives 403."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    mock_talk11 = models.Talk(
        id=11,
        event_id=1,
        title="Second Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )

    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk11

    response = client.post(
        "/talks/11/review",
        json={"decision": "approve"},
        cookies={"veditor_session": talk_token},
    )
    assert response.status_code == 403


# ── 6. Studio Landing Flow and Template Gating ────────────────────────────────


def test_studio_dashboard_landing_with_event_sso_token(mock_db):
    """Landing on /studio?event_id=1&sso_token=... sets cookie and 303 redirects."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    response = client.get(
        f"/studio?event_id=1&sso_token={event_token}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio?event_id=1"
    assert "veditor_session" in response.cookies
    assert response.cookies["veditor_session"] == event_token


def test_studio_talk_landing_with_talk_sso_token(mock_db):
    """Landing on /studio/talks/10?sso_token=... sets cookie and 303 redirects."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    response = client.get(
        f"/studio/talks/10?sso_token={talk_token}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio/talks/10"
    assert "veditor_session" in response.cookies
    assert response.cookies["veditor_session"] == talk_token


def test_studio_landing_with_invalid_sso_token():
    """Landing with an invalid or expired sso_token returns 401 Unauthorized."""
    response = client.get("/studio?sso_token=corrupt-token")
    assert response.status_code == 401
    assert "Invalid or expired SSO token" in response.json()["detail"]


def test_studio_events_endpoint_forbidden_for_sso():
    """SSO sessions attempting to access /studio/events receive 403 Forbidden."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    response = client.get("/studio/events", cookies={"veditor_session": event_token})
    assert response.status_code == 403


def test_studio_dashboard_redirects_talk_scoped_sso():
    """Talk-scoped SSO session accessing /studio is redirected to /studio/talks/{id}."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    response = client.get(
        "/studio", cookies={"veditor_session": talk_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio/talks/10"


def test_studio_dashboard_hides_action_buttons_for_sso(mock_db):
    """In rendered HTML, dashboard suppresses New Talk, Import, Attach, and Events buttons."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.query.return_value.filter.return_value.first.return_value = models.Event(
        id=1, name="Test Conf"
    )

    response = client.get("/studio", cookies={"veditor_session": event_token})
    assert response.status_code == 200
    html = response.text

    # Topbar should display SSO role
    assert "SSO (Organizer)" in html

    # Restricted action buttons should NOT be present
    assert 'id="btn-open-quick-talk"' not in html
    assert "+ New Talk" not in html
    assert 'id="btn-open-import"' not in html
    assert 'id="btn-open-room-attach"' not in html
    assert 'id="btn-events-link"' not in html
    assert 'id="nav-events-link"' not in html
    assert 'id="modal-import"' not in html
    assert 'id="modal-attach-room"' not in html
    assert 'id="modal-quick-talk"' not in html
