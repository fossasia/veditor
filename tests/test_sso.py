"""Comprehensive test suite for scoped SSO tokens and external client handoff."""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

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
from app.storage import get_storage_backend

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


def test_get_current_user_rejects_sso_token_query_param(mock_db):
    """get_current_user does not accept SSO tokens from query parameters (restricted to handoff handler)."""
    token = create_sso_token(scope_type="event", scope_id=5, role="organizer")

    request = MagicMock()
    request.query_params.get.side_effect = lambda k: token if k == "sso_token" else None
    request.headers = {}
    request.cookies = {}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(request=request, db=mock_db)
    assert exc_info.value.status_code == 401
    assert "Not authenticated" in exc_info.value.detail


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
    request.query_params.get.return_value = None
    request.headers = {"X-SSO-Token": tampered}
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
        cut_start=10.0,
        cut_end=60.0,
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


def test_sso_speaker_submits_cut_bounds_success(mock_db):
    """Talk-scoped speaker can submit cut bounds for their scoped talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        title="Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = ["10/raw/recording.mp4"]
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    with patch("app.routes.talks.light_queue.enqueue") as mock_enqueue:
        response = client.post(
            "/talks/10/cut",
            json={"cut_start": "00:01:00", "cut_end": "00:30:00"},
            headers={"X-SSO-Token": talk_token},
        )
        assert response.status_code == 202
        assert mock_talk.status == "cutting"
        assert mock_talk.cut_start == 60.0
        assert mock_talk.cut_end == 1800.0
        mock_enqueue.assert_called_once()


def test_sso_speaker_cannot_submit_cut_bounds_other_talk(mock_db):
    """Talk-scoped speaker cannot submit cut bounds for another talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    mock_talk11 = models.Talk(
        id=11,
        event_id=1,
        title="Other Talk",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk11

    response = client.post(
        "/talks/11/cut",
        json={"cut_start": "00:01:00", "cut_end": "00:30:00"},
        headers={"X-SSO-Token": talk_token},
    )
    assert response.status_code == 403


def test_sso_speaker_raw_preview_success(mock_db):
    """Talk-scoped speaker can fetch raw preview URL for their scoped talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    mock_talk = models.Talk(
        id=10,
        event_id=1,
        title="Keynote",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = ["10/raw/recording.mp4"]
    mock_storage.url.return_value = "http://storage/10/raw/recording.mp4"
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage

    response = client.get(
        "/talks/10/raw-preview",
        headers={"X-SSO-Token": talk_token},
    )
    assert response.status_code == 200
    assert response.json() == {"url": "http://storage/10/raw/recording.mp4"}


def test_sso_speaker_raw_preview_other_talk_forbidden(mock_db):
    """Talk-scoped speaker cannot fetch raw preview URL for another talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    mock_talk11 = models.Talk(
        id=11,
        event_id=1,
        title="Other Talk",
        room="Main",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="pending_bounds",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.first.return_value = mock_talk11

    response = client.get(
        "/talks/11/raw-preview",
        headers={"X-SSO-Token": talk_token},
    )
    assert response.status_code == 403


def test_studio_dashboard_sso_session_binds_event_id_to_template_context(mock_db):
    """When event SSO accesses /studio without ?event_id, template context receives scoped event_id."""
    event_token = create_sso_token(scope_type="event", scope_id=42, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    t = models.Talk(
        id=1,
        event_id=42,
        title="Keynote",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="done",
    )
    mock_db.query.return_value.filter.return_value.all.return_value = [t]
    mock_db.query.return_value.filter.return_value.first.return_value = models.Event(
        id=42, name="Scoped Event"
    )

    response = client.get("/studio", cookies={"veditor_session": event_token})
    assert response.status_code == 200
    assert '<input type="hidden" name="event_id" value="42">' in response.text


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
    """In rendered HTML, dashboard suppresses New Talk, Import, Attach, Events, and Delete actions for SSO."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    app.dependency_overrides[get_db] = lambda: mock_db
    t = models.Talk(
        id=1,
        event_id=1,
        title="Keynote",
        room="Main Stage",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="done",
    )
    mock_db.query.return_value.filter.return_value.all.return_value = [t]
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

    # Bulk actions and talk delete controls should NOT be present
    assert 'id="bulk-actions-bar"' not in html
    assert 'id="select-all-talks"' not in html
    assert 'class="talk-checkbox"' not in html
    assert "btn-delete-talk" not in html


# ── 6. SSO Endpoint Restrictions (Events, Bulk Delete, Import) ─────────────────


def test_create_event_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from creating new events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post(
        "/events",
        json={"name": "Disallowed Event"},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to create events" in resp.json()["detail"]


def test_update_event_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from modifying events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.patch(
        "/events/1",
        json={"name": "Updated Event"},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to modify events" in resp.json()["detail"]


def test_delete_event_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from deleting events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.delete(
        "/events/1",
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to delete events" in resp.json()["detail"]


def test_create_talk_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from creating talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "New Talk",
            "room": "Room A",
            "start": "2026-09-20T10:00:00Z",
            "end": "2026-09-20T11:00:00Z",
        },
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to create talks" in resp.json()["detail"]


def test_update_talk_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from modifying talk metadata."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.patch(
        "/talks/1",
        json={"title": "Updated Title"},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert (
        "SSO sessions are not permitted to modify talk metadata"
        in resp.json()["detail"]
    )


def test_delete_talk_rejected_for_sso(mock_db):
    """SSO sessions are forbidden from deleting talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    app.dependency_overrides[get_db] = lambda: mock_db

    resp = client.delete(
        "/talks/1",
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to delete talks" in resp.json()["detail"]


def test_list_events_sso_scoping(mock_db):
    """Event SSO only sees its scoped event; talk SSO is forbidden."""
    event_token = create_sso_token(scope_type="event", scope_id=42, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    app.dependency_overrides[get_db] = lambda: mock_db
    scoped_event = models.Event(id=42, name="Scoped Conf")
    mock_db.query.return_value.filter.return_value.all.return_value = [scoped_event]

    # Event SSO succeeds and gets only scoped event
    resp = client.get("/events", headers={"X-SSO-Token": event_token})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == 42

    # Verify query filter expression enforces Event scoping predicate
    event_filter_args = mock_db.query.return_value.filter.call_args.args
    assert any(
        getattr(e, "left", None) is not None
        and e.left.name == "id"
        and e.left.table.name == "events"
        and getattr(getattr(e, "right", None), "value", None) == 42
        for e in event_filter_args
    ), "Expected Event query to filter by models.Event.id == user.scope_id"

    # Talk SSO is forbidden
    resp = client.get("/events", headers={"X-SSO-Token": talk_token})
    assert resp.status_code == 403


def test_bulk_delete_talks_rejected_for_sso(mock_db):
    """SSO sessions (both event-scoped and talk-scoped) are forbidden from bulk deleting talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    app.dependency_overrides[get_db] = lambda: mock_db

    # Talk SSO is forbidden (role requirement)
    resp = client.post(
        "/talks/bulk-delete",
        json={"talk_ids": [10]},
        headers={"X-SSO-Token": talk_token},
    )
    assert resp.status_code == 403

    # Event SSO is also forbidden (SSO mutation restriction)
    resp = client.post(
        "/talks/bulk-delete",
        json={"talk_ids": [1, 2]},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert (
        "SSO sessions are not permitted to bulk delete talks" in resp.json()["detail"]
    )


def test_import_schedule_rejected_for_sso(mock_db):
    """SSO sessions (both event-scoped and talk-scoped) are forbidden from importing schedules."""
    event_token = create_sso_token(scope_type="event", scope_id=5, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    app.dependency_overrides[get_db] = lambda: mock_db

    payload = {
        "event_name": "Test Event",
        "talks": [
            {
                "title": "Opening Talk",
                "room": "Room A",
                "start": "2026-09-20T10:00:00Z",
                "end": "2026-09-20T11:00:00Z",
            }
        ],
    }

    # Talk SSO is forbidden (role requirement)
    resp = client.post(
        "/talks/schedule/import",
        json=payload,
        headers={"X-SSO-Token": talk_token},
    )
    assert resp.status_code == 403

    # Event SSO is also forbidden (SSO mutation restriction)
    resp = client.post(
        "/talks/schedule/import",
        json=payload,
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to import schedules" in resp.json()["detail"]


def test_sso_token_organizer_role_and_identity():
    """Verify reviewer role and identity claims (email, display_name) in SSO token."""
    token = create_sso_token(
        scope_type="event",
        scope_id=42,
        role="organizer",
        email="organizer@example.org",
        display_name="Lead Organizer",
    )
    payload = decode_sso_token(token)
    assert payload is not None
    assert payload["role"] == "organizer"
    assert payload["email"] == "organizer@example.org"
    assert payload["display_name"] == "Lead Organizer"
    assert payload["scope_type"] == "event"
    assert payload["scope_id"] == 42


def test_get_current_user_populates_identity_from_sso(mock_db):
    """Verify CurrentUser retains email and display_name from SSO JWT."""
    token = create_sso_token(
        scope_type="event",
        scope_id=1,
        role="organizer",
        email="organizer@example.org",
        display_name="Lead Organizer",
    )
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
    assert user.role == "organizer"
    assert user.email == "organizer@example.org"
    assert user.display_name == "Lead Organizer"
    assert user.is_sso is True
    assert user.event_ids == [1]


def test_studio_dashboard_displays_sso_email_identity(mock_db):
    """When an SSO token contains an email, the topbar displays the user's email address."""
    event_token = create_sso_token(
        scope_type="event",
        scope_id=1,
        role="organizer",
        email="organizer@eventyay.com",
        display_name="Organizer User",
    )
    app.dependency_overrides[get_db] = lambda: mock_db
    mock_db.query.return_value.filter.return_value.all.return_value = []
    mock_db.query.return_value.filter.return_value.first.return_value = models.Event(
        id=1, name="Test Conf"
    )

    response = client.get("/studio", cookies={"veditor_session": event_token})
    assert response.status_code == 200
    assert "organizer@eventyay.com" in response.text
