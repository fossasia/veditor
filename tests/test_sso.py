"""Comprehensive test suite for scoped SSO tokens and external client handoff."""

from collections.abc import Generator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.auth import (
    CurrentUser,
    check_event_access,
    check_talk_access,
    get_client,
    get_current_user,
)
from app.config import settings
from app.consts import AuthRole, SSOScopeType
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
def clean_dependency_overrides() -> Generator:
    """Ensure dependency overrides are cleaned up after each test."""
    yield
    app.dependency_overrides.clear()


def add_event(
    db_session: Session,
    *,
    id: int,
    name: str,
    speaker_magic_link_expire_hours: int | None = None,
) -> models.Event:
    event = models.Event(
        id=id,
        name=name,
        speaker_magic_link_expire_hours=speaker_magic_link_expire_hours,
    )
    db_session.add(event)
    db_session.flush()
    return event


def add_talk(
    db_session: Session,
    *,
    id: int,
    event_id: int,
    title: str,
    room: str,
    status: str = "waiting_for_files",
    cut_start: float | None = None,
    cut_end: float | None = None,
    raw_duration_seconds: float | None = None,
) -> models.Talk:
    talk = models.Talk(
        id=id,
        event_id=event_id,
        title=title,
        room=room,
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status=status,
        cut_start=cut_start,
        cut_end=cut_end,
        raw_duration_seconds=raw_duration_seconds,
    )
    db_session.add(talk)
    db_session.flush()
    return talk


# ── 1. POST /events/{event_id}/sso-token ───────────────────────────────────────


def test_event_sso_token_unauthenticated() -> None:
    """Unauthenticated caller without API key is rejected with 401."""
    response = client.post("/events/1/sso-token")
    assert response.status_code == 401


def test_event_sso_token_rejects_human_session_auth() -> None:
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


def test_event_sso_token_event_not_found(db_session: Session) -> None:
    """If the requested event does not exist, return 404."""
    mock_client = models.Client(id=1, event_ids=[1, 2])
    app.dependency_overrides[get_client] = lambda: mock_client

    response = client.post("/events/999/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 404
    assert "Event not found" in response.json()["detail"]


def test_event_sso_token_client_forbidden(db_session: Session) -> None:
    """If the calling API client is not scoped to this event, return 403."""
    mock_client = models.Client(id=1, event_ids=[2])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Conf 2026")

    response = client.post("/events/1/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 403
    assert (
        "Client is not authorized to mint an SSO token for this event"
        in response.json()["detail"]
    )


def test_event_sso_token_success(db_session: Session) -> None:
    """Valid API client receives an event-scoped organizer SSO token."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Conf 2026")

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
    assert payload.scope_type == "event"
    assert payload.scope_id == 1
    assert payload.role == "organizer"
    # Identity claims are explicitly excluded from the typed SSO payload.
    assert not hasattr(payload, "user_id")
    assert not hasattr(payload, "sub")


# ── 2. POST /talks/{talk_id}/sso-token ─────────────────────────────────────────


def test_talk_sso_token_unauthenticated() -> None:
    """Unauthenticated caller without API key is rejected with 401."""
    response = client.post("/talks/10/sso-token")
    assert response.status_code == 401


def test_talk_sso_token_rejects_human_auth() -> None:
    """Human caller with bearer token is rejected with 401."""
    bearer_token = create_access_token(user_id=1, email="admin@test.com", role="admin")
    resp = client.post(
        "/talks/10/sso-token", headers={"Authorization": f"Bearer {bearer_token}"}
    )
    assert resp.status_code == 401


def test_talk_sso_token_talk_not_found(db_session: Session) -> None:
    """If the requested talk does not exist, return 404."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client

    response = client.post("/talks/999/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 404
    assert "Talk not found" in response.json()["detail"]


def test_talk_sso_token_client_forbidden(db_session: Session) -> None:
    """If talk belongs to an event the client is not scoped for, return 403."""
    mock_client = models.Client(id=1, event_ids=[2])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(db_session, id=10, event_id=1, title="Opening Keynote", room="Main")

    response = client.post("/talks/10/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 403
    assert (
        "Client is not authorized to mint an SSO token for this talk"
        in response.json()["detail"]
    )


def test_talk_sso_token_success(db_session: Session) -> None:
    """Valid API client receives a talk-scoped speaker SSO token."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(db_session, id=10, event_id=1, title="Opening Keynote", room="Main")

    response = client.post("/talks/10/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 200
    data = response.json()

    assert "token" in data
    assert data["token_type"] == "bearer"
    assert data["scope_type"] == "talk"
    assert data["scope_id"] == 10
    assert data["role"] == "speaker"
    assert data["expires_in_seconds"] == settings.speaker_magic_link_expire_hours * 3600
    assert data["url"] == f"/studio/talks/10?sso_token={data['token']}"

    payload = decode_sso_token(data["token"])
    assert payload is not None
    assert payload.scope_type == "talk"
    assert payload.scope_id == 10
    assert payload.role == "speaker"
    assert not hasattr(payload, "user_id")
    assert not hasattr(payload, "sub")


def test_talk_sso_token_custom_expires_in_seconds(db_session: Session) -> None:
    """Valid API client can supply a custom expires_in_seconds for talk SSO token."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(db_session, id=10, event_id=1, title="Opening Keynote", room="Main")

    response = client.post(
        "/talks/10/sso-token",
        headers={"X-API-Key": "test"},
        json={"expires_in_seconds": 7200},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["expires_in_seconds"] == 7200

    payload = decode_sso_token(data["token"])
    assert payload is not None
    assert payload.scope_id == 10


def test_talk_sso_token_invalid_expires_in_seconds(db_session: Session) -> None:
    """API client cannot supply a non-positive expires_in_seconds."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client

    response = client.post(
        "/talks/10/sso-token",
        headers={"X-API-Key": "test"},
        json={"expires_in_seconds": 0},
    )
    assert response.status_code == 422

    response_neg = client.post(
        "/talks/10/sso-token",
        headers={"X-API-Key": "test"},
        json={"expires_in_seconds": -10},
    )
    assert response_neg.status_code == 422

    response_bool = client.post(
        "/talks/10/sso-token",
        headers={"X-API-Key": "test"},
        json={"expires_in_seconds": True},
    )
    assert response_bool.status_code == 422


# ── 3. Identity Resolution (get_current_user) ─────────────────────────────────


def test_get_current_user_rejects_sso_token_query_param(db_session: Session) -> None:
    """get_current_user does not accept SSO tokens from query parameters (restricted to handoff handler)."""
    token = create_sso_token(scope_type="event", scope_id=5, role="organizer")

    request = MagicMock()
    request.query_params.get.side_effect = lambda k: token if k == "sso_token" else None
    request.headers = {}
    request.cookies = {}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(request=request, db=db_session)
    assert exc_info.value.status_code == 401
    assert "Not authenticated" in exc_info.value.detail


def test_get_current_user_via_x_sso_token_header(db_session: Session) -> None:
    """get_current_user resolves SSO token from X-SSO-Token header."""
    token = create_sso_token(scope_type="talk", scope_id=12, role="speaker")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {"X-SSO-Token": token}
    request.cookies = {}

    user = get_current_user(request=request, db=db_session)
    assert user.is_sso is True
    assert user.source == "sso"
    assert user.role == "speaker"
    assert user.scope_type == "talk"
    assert user.scope_id == 12


def test_get_current_user_via_veditor_session_cookie(db_session: Session) -> None:
    """get_current_user resolves SSO token stored in veditor_session cookie."""
    token = create_sso_token(scope_type="talk", scope_id=12, role="speaker")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {}
    request.cookies = {"veditor_session": token}

    user = get_current_user(request=request, db=db_session, cookie_token=token)
    assert user.is_sso is True
    assert user.scope_id == 12


def test_get_current_user_via_bearer_sso_token(db_session: Session) -> None:
    """get_current_user resolves SSO token passed in Authorization: Bearer header."""
    token = create_sso_token(scope_type="event", scope_id=7, role="organizer")

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {"Authorization": f"Bearer {token}"}
    request.cookies = {}

    bearer = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = get_current_user(
        request=request,
        db=db_session,
        bearer_creds=bearer,
    )
    assert user.is_sso is True
    assert user.scope_type == "event"
    assert user.scope_id == 7


def test_get_current_user_tampered_sso_token(db_session: Session) -> None:
    """Tampered or invalid SSO token immediately raises 401 Unauthorized."""
    token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    tampered = token[:-5] + "wrong"

    request = MagicMock()
    request.query_params.get.return_value = None
    request.headers = {"X-SSO-Token": tampered}
    request.cookies = {}

    with pytest.raises(HTTPException) as exc_info:
        get_current_user(request=request, db=db_session)
    assert exc_info.value.status_code == 401
    assert "Invalid or expired SSO token" in exc_info.value.detail


# ── 4. Access Scope Checks ────────────────────────────────────────────────────


def test_check_event_access_sso(db_session: Session) -> None:
    add_event(db_session, id=1, name="Conf 1")
    add_event(db_session, id=2, name="Conf 2")

    # Event-scoped user
    event_user = CurrentUser(
        source="sso", role=AuthRole.ORGANIZER, scope_type=SSOScopeType.EVENT, scope_id=1
    )
    result = check_event_access(1, event_user, db_session)
    assert result is not None
    assert result.id == 1

    with pytest.raises(HTTPException) as exc:
        check_event_access(2, event_user, db_session)
    assert exc.value.status_code == 403

    # Talk-scoped user cannot access event-level endpoints
    talk_user = CurrentUser(
        source="sso", role=AuthRole.SPEAKER, scope_type=SSOScopeType.TALK, scope_id=10
    )
    with pytest.raises(HTTPException) as exc:
        check_event_access(1, talk_user, db_session)
    assert exc.value.status_code == 403


def test_check_talk_access_sso(db_session: Session) -> None:
    add_event(db_session, id=1, name="Event 1")
    add_event(db_session, id=2, name="Event 2")
    add_talk(db_session, id=10, event_id=1, title="Talk A", room="Room 1")
    add_talk(db_session, id=11, event_id=1, title="Talk B", room="Room 2")
    add_talk(db_session, id=20, event_id=2, title="Talk C", room="Room 3")

    talk_in_event1 = db_session.query(models.Talk).filter(models.Talk.id == 10).first()
    talk2_in_event1 = db_session.query(models.Talk).filter(models.Talk.id == 11).first()
    talk_in_event2 = db_session.query(models.Talk).filter(models.Talk.id == 20).first()

    # Event-scoped organizer can access any talk in its event
    event_user = CurrentUser(
        source="sso", role=AuthRole.ORGANIZER, scope_type=SSOScopeType.EVENT, scope_id=1
    )
    assert talk_in_event1 is not None
    assert check_talk_access(talk_in_event1, event_user, db_session) == talk_in_event1
    assert talk2_in_event1 is not None
    assert check_talk_access(talk2_in_event1, event_user, db_session) == talk2_in_event1

    assert talk_in_event2 is not None
    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk_in_event2, event_user, db_session)
    assert exc.value.status_code == 403

    # Talk-scoped speaker can ONLY access its specific talk
    talk_user = CurrentUser(
        source="sso", role=AuthRole.SPEAKER, scope_type=SSOScopeType.TALK, scope_id=10
    )
    assert talk_in_event1 is not None
    assert check_talk_access(talk_in_event1, talk_user, db_session) == talk_in_event1

    # Same event, different talk -> 403 Forbidden!
    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk2_in_event1, talk_user, db_session)
    assert exc.value.status_code == 403

    # Different event -> 403 Forbidden
    assert talk_in_event2 is not None
    with pytest.raises(HTTPException) as exc:
        check_talk_access(talk_in_event2, talk_user, db_session)
    assert exc.value.status_code == 403


# ── 5. Review Submission with SSO ─────────────────────────────────────────────


def test_sso_speaker_submits_review_success(db_session: Session) -> None:
    """Talk-scoped speaker submits review on target talk; user_id recorded as None."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=10,
        event_id=1,
        title="Opening Keynote",
        room="Main",
        status="preview",
        cut_start=10.0,
        cut_end=60.0,
    )

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

    # Verify Review model persisted to DB had user_id = None
    review = db_session.query(models.Review).filter(models.Review.talk_id == 10).first()
    assert review is not None
    assert review.user_id is None


def test_sso_speaker_cannot_review_other_talk(db_session: Session) -> None:
    """Talk-scoped speaker attempting to review another talk receives 403."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=11,
        event_id=1,
        title="Second Keynote",
        room="Main",
        status="preview",
    )

    response = client.post(
        "/talks/11/review",
        json={"decision": "approve"},
        cookies={"veditor_session": talk_token},
    )
    assert response.status_code == 403


def test_sso_speaker_submits_cut_bounds_success(db_session: Session) -> None:
    """Talk-scoped speaker can submit cut bounds for their scoped talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=10,
        event_id=1,
        title="Keynote",
        room="Main",
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )

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
        mock_enqueue.assert_called_once()

    # Verify the talk was updated in the DB
    db_session.expire_all()
    talk = db_session.query(models.Talk).filter(models.Talk.id == 10).first()
    assert talk is not None
    assert talk.status == "cutting"
    assert talk.cut_start == 60.0
    assert talk.cut_end == 1800.0


def test_sso_speaker_cannot_submit_cut_bounds_other_talk(db_session: Session) -> None:
    """Talk-scoped speaker cannot submit cut bounds for another talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=11,
        event_id=1,
        title="Other Talk",
        room="Main",
        status="pending_bounds",
        raw_duration_seconds=3600.0,
    )

    response = client.post(
        "/talks/11/cut",
        json={"cut_start": "00:01:00", "cut_end": "00:30:00"},
        headers={"X-SSO-Token": talk_token},
    )
    assert response.status_code == 403


def test_sso_speaker_raw_preview_success(db_session: Session) -> None:
    """Talk-scoped speaker can fetch raw preview URL for their scoped talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=10,
        event_id=1,
        title="Keynote",
        room="Main",
        status="pending_bounds",
    )

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


def test_sso_speaker_raw_preview_other_talk_forbidden(db_session: Session) -> None:
    """Talk-scoped speaker cannot fetch raw preview URL for another talk."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    add_event(db_session, id=1, name="Conf 2026")
    add_talk(
        db_session,
        id=11,
        event_id=1,
        title="Other Talk",
        room="Main",
        status="pending_bounds",
    )

    response = client.get(
        "/talks/11/raw-preview",
        headers={"X-SSO-Token": talk_token},
    )
    assert response.status_code == 403


def test_studio_dashboard_sso_session_binds_event_id_to_template_context(
    db_session: Session,
) -> None:
    """When event SSO accesses /studio without ?event_id, template context receives scoped event_id."""
    event_token = create_sso_token(scope_type="event", scope_id=42, role="organizer")
    add_event(db_session, id=42, name="Scoped Event")
    add_talk(
        db_session,
        id=1,
        event_id=42,
        title="Keynote",
        room="Room 1",
        status="done",
    )

    response = client.get("/studio", cookies={"veditor_session": event_token})
    assert response.status_code == 200
    assert '<input type="hidden" name="event_id" value="42">' in response.text


# ── 6. Studio Landing Flow and Template Gating ────────────────────────────────


def test_studio_dashboard_landing_with_event_sso_token() -> None:
    """Landing on /studio?event_id=1&sso_token=... sets cookie and 303 redirects."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    response = client.get(
        f"/studio?event_id=1&sso_token={event_token}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio?event_id=1"
    assert "veditor_session" in response.cookies
    assert response.cookies["veditor_session"] == event_token


def test_studio_talk_landing_with_talk_sso_token() -> None:
    """Landing on /studio/talks/10?sso_token=... sets cookie and 303 redirects with matched max_age."""
    talk_token = create_sso_token(
        scope_type="talk",
        scope_id=10,
        role="speaker",
        expires_in_seconds=settings.speaker_magic_link_expire_hours * 3600,
    )

    response = client.get(
        f"/studio/talks/10?sso_token={talk_token}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio/talks/10"
    assert "veditor_session" in response.cookies
    assert response.cookies["veditor_session"] == talk_token
    # Verify cookie Max-Age reflects token's validity window
    set_cookie = response.headers.get("set-cookie", "")
    assert "Max-Age=" in set_cookie
    # Should be well above default 300s
    import re

    m = re.search(r"Max-Age=(\d+)", set_cookie)
    assert m is not None
    assert int(m.group(1)) > 300


def test_studio_landing_with_invalid_sso_token() -> None:
    """Landing with an invalid or expired sso_token returns 401 Unauthorized."""
    response = client.get("/studio?sso_token=corrupt-token")
    assert response.status_code == 401
    assert "Invalid or expired SSO token" in response.json()["detail"]


def test_studio_events_endpoint_forbidden_for_sso() -> None:
    """SSO sessions attempting to access /studio/events receive 403 Forbidden."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    response = client.get("/studio/events", cookies={"veditor_session": event_token})
    assert response.status_code == 403


def test_studio_dashboard_redirects_talk_scoped_sso() -> None:
    """Talk-scoped SSO session accessing /studio is redirected to /studio/talks/{id}."""
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")
    response = client.get(
        "/studio", cookies={"veditor_session": talk_token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio/talks/10"


def test_studio_dashboard_hides_action_buttons_for_sso(db_session: Session) -> None:
    """In rendered HTML, dashboard permits management controls for SSO organizers while hiding global events links, and suppresses management controls for speakers."""
    org_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    add_event(db_session, id=1, name="Test Conf")
    add_talk(
        db_session,
        id=1,
        event_id=1,
        title="Keynote",
        room="Main Stage",
        status="done",
    )

    # 1. Organizer arriving via SSO
    response = client.get("/studio", cookies={"veditor_session": org_token})
    assert response.status_code == 200
    html = response.text

    # Topbar should display SSO role
    assert "SSO (Organizer)" in html

    # Room recording controls SHOULD be present for SSO organizer
    assert 'id="btn-open-room-attach"' in html
    assert 'id="modal-attach-room"' in html

    # Mutation controls unsupported for SSO should NOT be present
    assert 'id="btn-open-quick-talk"' not in html
    assert "+ New Talk" not in html
    assert 'id="btn-open-import"' not in html
    assert 'id="modal-import"' not in html
    assert 'id="modal-quick-talk"' not in html
    assert 'id="bulk-actions-bar"' not in html
    assert 'id="select-all-talks"' not in html
    assert 'class="talk-checkbox"' not in html
    assert "btn-delete-talk" not in html

    # Global cross-event links should NOT be present for scoped SSO session
    assert 'id="btn-events-link"' not in html
    assert 'id="nav-events-link"' not in html

    # 2. Speaker arriving via SSO
    spk_token = create_sso_token(
        scope_type="event", scope_id=1, role="speaker", email="speaker@example.com"
    )
    response_spk = client.get("/studio", cookies={"veditor_session": spk_token})
    assert response_spk.status_code == 200
    html_spk = response_spk.text

    assert 'id="btn-open-quick-talk"' not in html_spk
    assert 'id="btn-open-import"' not in html_spk
    assert 'id="btn-open-room-attach"' not in html_spk
    assert 'id="modal-import"' not in html_spk
    assert 'id="modal-attach-room"' not in html_spk
    assert 'id="modal-quick-talk"' not in html_spk
    assert 'id="bulk-actions-bar"' not in html_spk


# ── 6. SSO Endpoint Restrictions (Events, Bulk Delete, Import) ─────────────────


def test_create_event_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from creating new events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    resp = client.post(
        "/events",
        json={"name": "Disallowed Event"},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to create events" in resp.json()["detail"]


def test_update_event_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from modifying events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    resp = client.patch(
        "/events/1",
        json={"name": "Updated Event"},
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to modify events" in resp.json()["detail"]


def test_delete_event_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from deleting events."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    resp = client.delete(
        "/events/1",
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to delete events" in resp.json()["detail"]


def test_create_talk_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from creating talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

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


def test_update_talk_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from modifying talk metadata."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

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


def test_delete_talk_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions are forbidden from deleting talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")

    resp = client.delete(
        "/talks/1",
        headers={"X-SSO-Token": event_token},
    )
    assert resp.status_code == 403
    assert "SSO sessions are not permitted to delete talks" in resp.json()["detail"]


def test_list_events_sso_scoping(db_session: Session) -> None:
    """Event SSO only sees its scoped event; talk SSO is forbidden."""
    event_token = create_sso_token(scope_type="event", scope_id=42, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

    add_event(db_session, id=42, name="Scoped Conf")

    # Event SSO succeeds and gets only scoped event
    resp = client.get("/events", headers={"X-SSO-Token": event_token})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == 42

    # Talk SSO is forbidden
    resp = client.get("/events", headers={"X-SSO-Token": talk_token})
    assert resp.status_code == 403


def test_bulk_delete_talks_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions (both event-scoped and talk-scoped) are forbidden from bulk deleting talks."""
    event_token = create_sso_token(scope_type="event", scope_id=1, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

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


def test_import_schedule_rejected_for_sso(db_session: Session) -> None:
    """SSO sessions (both event-scoped and talk-scoped) are forbidden from importing schedules."""
    event_token = create_sso_token(scope_type="event", scope_id=5, role="organizer")
    talk_token = create_sso_token(scope_type="talk", scope_id=10, role="speaker")

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


def test_sso_token_organizer_role_and_identity() -> None:
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
    assert payload.role == "organizer"
    assert payload.email == "organizer@example.org"
    assert payload.display_name == "Lead Organizer"
    assert payload.scope_type == "event"
    assert payload.scope_id == 42


def test_get_current_user_populates_identity_from_sso(db_session: Session) -> None:
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
        db=db_session,
        bearer_creds=bearer,
    )
    assert user.role == "organizer"
    assert user.email == "organizer@example.org"
    assert user.display_name == "Lead Organizer"
    assert user.is_sso is True
    assert user.event_ids == [1]

    # Wiring: decode_sso_token emits AuthRole, so user.role is the enum itself
    # without any per-call-site conversion.
    assert user.role is AuthRole.ORGANIZER
    assert isinstance(user.role, AuthRole)


def test_studio_dashboard_displays_sso_email_identity(db_session: Session) -> None:
    """When an SSO token contains an email, the topbar displays the user's email address."""
    event_token = create_sso_token(
        scope_type="event",
        scope_id=1,
        role="organizer",
        email="organizer@eventyay.com",
        display_name="Organizer User",
    )
    add_event(db_session, id=1, name="Test Conf")

    response = client.get("/studio", cookies={"veditor_session": event_token})
    assert response.status_code == 200
    assert "organizer@eventyay.com" in response.text


def test_talk_sso_token_event_override(db_session: Session) -> None:
    """Talk SSO token honors per-event speaker_magic_link_expire_hours override."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Custom Event", speaker_magic_link_expire_hours=48)
    add_talk(db_session, id=15, event_id=1, title="Event Override Talk", room="Room 1")

    response = client.post("/talks/15/sso-token", headers={"X-API-Key": "test"})
    assert response.status_code == 200
    data = response.json()
    assert data["expires_in_seconds"] == 48 * 3600


def test_talk_sso_token_request_override_beats_event_override(
    db_session: Session,
) -> None:
    """Explicit expires_in_seconds in payload beats per-event override."""
    mock_client = models.Client(id=1, event_ids=[1])
    app.dependency_overrides[get_client] = lambda: mock_client
    add_event(db_session, id=1, name="Custom Event", speaker_magic_link_expire_hours=48)
    add_talk(
        db_session, id=16, event_id=1, title="Explicit Override Talk", room="Room 1"
    )

    response = client.post(
        "/talks/16/sso-token",
        headers={"X-API-Key": "test"},
        json={"expires_in_seconds": 1800},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["expires_in_seconds"] == 1800
