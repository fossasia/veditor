import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import (
    CurrentUser,
    check_event_access,
    get_client,
    get_current_user,
    hash_api_key,
    lock_active_admins,
    require_admin,
    require_event_access,
    require_role,
    require_talk_access,
    verify_event_access,
)
from app.consts import AuthRole
from app.db import get_db
from app.main import app
from app.models import Client, Event, Talk, User
from app.security import (
    create_access_token,
    create_session_token,
    hash_password,
)
from tests.conftest import TestSessionLocal


def _create_user(db: Session, **overrides: Any) -> User:
    data = {
        "email": f"user-{uuid.uuid4().hex[:8]}@example.com",
        "hashed_password": "x",
        "role": "organizer",
        "is_active": True,
    }
    data.update(overrides)
    user = User(**data)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_client(
    db: Session,
    *,
    api_key: str | None = None,
    event_ids: list[int] | None = None,
    **overrides: Any,
) -> Client:
    data = {
        "name": "Test Client",
        "hashed_key": hash_api_key(api_key) if api_key else uuid.uuid4().hex,
        "event_ids": list(event_ids or []),
    }
    data.update(overrides)
    client_row = Client(**data)
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


def _create_event(
    db: Session, *, name: str = "Test Event", created_by_user_id: int | None = None
) -> Event:
    event = Event(name=name, created_by_user_id=created_by_user_id)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(db: Session, event_id: int, *, status: str = "done") -> Talk:
    talk = Talk(
        event_id=event_id,
        title=f"Talk {uuid.uuid4().hex[:8]}",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status=status,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def test_hash_api_key() -> None:
    key1 = "some-random-key"
    key2 = "some-random-key"
    key3 = "another-key"
    assert hash_api_key(key1) == hash_api_key(key2)
    assert hash_api_key(key1) != hash_api_key(key3)
    # Ensure it's 64 chars for SHA-256
    assert len(hash_api_key(key1)) == 64


def test_get_client_missing_key() -> None:
    with pytest.raises(HTTPException) as excinfo:
        get_client(None, MagicMock())
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Missing API Key"


def test_get_client_invalid_key(db_session: Session) -> None:
    with pytest.raises(HTTPException) as excinfo:
        get_client("invalid-key", db_session)

    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"


def test_get_client_valid_key(db_session: Session) -> None:
    client_row = _create_client(
        db_session,
        api_key="valid-key",
        event_ids=[1, 2],
        last_used_at=datetime.now(UTC),
    )

    resolved = get_client("valid-key", db_session)
    assert resolved.id == client_row.id


def test_verify_event_access_in_scope() -> None:
    mock_client = Client(id=1, event_ids=[1, 2, 3])
    # Should not raise any exception
    verify_event_access(2, mock_client)


def test_verify_event_access_out_of_scope() -> None:
    mock_client = Client(id=1, event_ids=[1, 2, 3])
    with pytest.raises(HTTPException) as excinfo:
        verify_event_access(4, mock_client)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Client is not authorized to access this event"


# ---------------------------------------------------------------------------
# CurrentUser Model Tests
# ---------------------------------------------------------------------------


def test_current_user_model_attributes() -> None:
    user = CurrentUser(
        user_id=1,
        email="user@example.com",
        role="organizer",
        source="jwt",
        event_ids=[],
    )
    assert user.user_id == 1
    assert user.email == "user@example.com"
    assert user.role == "organizer"
    assert user.source == "jwt"
    assert user.event_ids == []
    assert not user.is_machine
    assert not user.is_admin

    machine = CurrentUser(
        user_id=None,
        email=None,
        role="admin",
        source="api_key",
        event_ids=[10, 20],
    )
    assert machine.is_machine
    assert machine.is_admin
    assert machine.event_ids == [10, 20]


# ---------------------------------------------------------------------------
# get_current_user Resolution Tests
# ---------------------------------------------------------------------------


def test_get_current_user_api_key_valid(db_session: Session) -> None:
    _create_client(db_session, api_key="secret-key", event_ids=[42])

    user = get_current_user(request=MagicMock(), api_key="secret-key", db=db_session)
    assert user.source == "api_key"
    assert user.role == "admin"
    assert user.user_id is None
    assert user.email is None
    assert user.event_ids == [42]
    assert user.is_machine


def test_get_current_user_cookie_api_key_valid(db_session: Session) -> None:
    _create_client(db_session, api_key="secret-cookie-key", event_ids=[42])

    user = get_current_user(
        request=MagicMock(), cookie_api_key="secret-cookie-key", db=db_session
    )
    assert user.source == "api_key"
    assert user.role == "admin"
    assert user.user_id is None
    assert user.email is None
    assert user.event_ids == [42]
    assert user.is_machine


def test_get_current_user_api_key_invalid(db_session: Session) -> None:
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=MagicMock(), api_key="bad-key", db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_valid(db_session: Session) -> None:
    user_row = _create_user(db_session, email="org@example.com", role="organizer")
    token = create_session_token(user_id=user_row.id, role="organizer")

    user = get_current_user(request=MagicMock(), cookie_token=token, db=db_session)
    assert user.source == "cookie"
    assert user.user_id == user_row.id
    assert user.email == "org@example.com"
    assert user.role == "organizer"
    assert user.event_ids == []
    assert not user.is_machine


def test_get_current_user_session_cookie_invalid(db_session: Session) -> None:
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            request=MagicMock(), cookie_token="not.a.valid.jwt", db=db_session
        )
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired session token" in excinfo.value.detail
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_user_inactive(db_session: Session) -> None:
    user_row = _create_user(
        db_session, email="inactive@example.com", role="user", is_active=False
    )
    token = create_session_token(user_id=user_row.id, role="user")

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=MagicMock(), cookie_token=token, db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "inactive" in excinfo.value.detail.lower()
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_user_not_found(db_session: Session) -> None:
    missing_id = 10_000_000
    token = create_session_token(user_id=missing_id, role="user")

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=MagicMock(), cookie_token=token, db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "not found" in excinfo.value.detail.lower()
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_bearer_token_valid(db_session: Session) -> None:
    user_row = _create_user(db_session, email="dev@example.com", role="admin")
    token = create_access_token(
        user_id=user_row.id, email="dev@example.com", role="admin"
    )
    mock_creds = MagicMock()
    mock_creds.credentials = token

    user = get_current_user(request=MagicMock(), bearer_creds=mock_creds, db=db_session)
    assert user.source == "jwt"
    assert user.user_id == user_row.id
    assert user.email == "dev@example.com"
    assert user.role == "admin"
    assert user.is_admin


def test_get_current_user_bearer_token_invalid(db_session: Session) -> None:
    mock_creds = MagicMock()
    mock_creds.credentials = "invalid.bearer.token"

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=MagicMock(), bearer_creds=mock_creds, db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired access token" in excinfo.value.detail


def test_get_current_user_bearer_token_user_inactive(db_session: Session) -> None:
    user_row = _create_user(
        db_session, email="dev@example.com", role="admin", is_active=False
    )
    token = create_access_token(
        user_id=user_row.id, email="dev@example.com", role="admin"
    )
    mock_creds = MagicMock()
    mock_creds.credentials = token

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=MagicMock(), bearer_creds=mock_creds, db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "inactive" in excinfo.value.detail.lower()


def test_get_current_user_no_credentials(db_session: Session) -> None:
    request = MagicMock()
    request.headers = {}
    request.cookies = {}
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=request, db=db_session)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Not authenticated"
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_precedence_api_key_over_cookie(db_session: Session) -> None:
    # When both API key and cookie are provided, API key is evaluated first.
    _create_client(db_session, api_key="api-key", event_ids=[1])

    token = create_session_token(user_id=2, role="user")
    user = get_current_user(
        request=MagicMock(), api_key="api-key", cookie_token=token, db=db_session
    )
    assert user.source == "api_key"


def test_get_current_user_fail_fast_on_invalid_api_key(db_session: Session) -> None:
    # If explicit API key is provided but invalid, fails immediately with 401 instead of falling back.
    token = create_session_token(user_id=2, role="user")
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(
            request=MagicMock(), api_key="bad-key", cookie_token=token, db=db_session
        )
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"


def test_get_current_user_precedence_session_over_cookie_api_key(
    db_session: Session,
) -> None:
    # Valid session cookie takes precedence over stale or invalid cookie_api_key.
    user_row = _create_user(db_session, email="u@example.com", role="user")

    token = create_session_token(user_id=user_row.id, role="user")
    user = get_current_user(
        request=MagicMock(),
        cookie_token=token,
        cookie_api_key="stale-or-invalid-key",
        db=db_session,
    )
    assert user.source == "cookie"
    assert user.user_id == user_row.id


def test_get_current_user_ignores_stale_cookie_api_key_for_bearer_token(
    db_session: Session,
) -> None:
    user_row = _create_user(db_session, email="u@example.com", role="user")
    token = create_access_token(user_id=user_row.id, email="u@example.com", role="user")
    mock_creds = MagicMock()
    mock_creds.credentials = token

    user = get_current_user(
        request=MagicMock(),
        cookie_api_key="stale-or-invalid-key",
        bearer_creds=mock_creds,
        db=db_session,
    )

    assert user.source == "jwt"
    assert user.user_id == user_row.id


# ---------------------------------------------------------------------------
# Role Hierarchy Tests
# ---------------------------------------------------------------------------


def test_require_role_hierarchy() -> None:
    check_user = require_role(AuthRole.USER)
    check_org = require_role(AuthRole.ORGANIZER)
    check_admin = require_role(AuthRole.ADMIN)

    regular_user = CurrentUser(role=AuthRole.USER, source="jwt")
    organizer_user = CurrentUser(role=AuthRole.ORGANIZER, source="jwt")
    admin_user = CurrentUser(role=AuthRole.ADMIN, source="jwt")

    # 'user' requirement allows all
    assert check_user(regular_user) == regular_user
    assert check_user(organizer_user) == organizer_user
    assert check_user(admin_user) == admin_user

    # 'organizer' requirement blocks 'user', allows 'organizer' and 'admin'
    with pytest.raises(HTTPException) as excinfo:
        check_org(regular_user)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert check_org(organizer_user) == organizer_user
    assert check_org(admin_user) == admin_user

    # 'admin' requirement blocks 'user' and 'organizer', allows 'admin'
    with pytest.raises(HTTPException) as excinfo:
        check_admin(regular_user)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN

    with pytest.raises(HTTPException) as excinfo:
        check_admin(organizer_user)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN

    assert check_admin(admin_user) == admin_user


def test_require_admin() -> None:
    regular_user = CurrentUser(user_id=1, role="user", source="jwt")
    machine_admin = CurrentUser(role="admin", source="api_key")
    human_admin = CurrentUser(user_id=2, role="admin", source="cookie")

    with pytest.raises(HTTPException) as excinfo:
        require_admin(regular_user)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Operation requires a human administrator"

    with pytest.raises(HTTPException) as excinfo:
        require_admin(machine_admin)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Operation requires a human administrator"

    assert require_admin(human_admin) == human_admin


# ---------------------------------------------------------------------------
# Event Access Control Tests
# ---------------------------------------------------------------------------


def test_check_event_access_not_found(db_session: Session) -> None:
    user = CurrentUser(role="admin", source="jwt")
    missing_id = 10_000_000

    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=missing_id, user=user, db=db_session)
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert excinfo.value.detail == "Event not found"


def test_check_event_access_machine_client_in_scope(db_session: Session) -> None:
    owner = _create_user(db_session)
    event = _create_event(db_session, name="PyCon", created_by_user_id=owner.id)

    client_user = CurrentUser(role="admin", source="api_key", event_ids=[event.id])
    assert (
        check_event_access(event_id=event.id, user=client_user, db=db_session) is event
    )


def test_check_event_access_machine_client_not_found(db_session: Session) -> None:
    missing_id = 10_000_000
    client_user = CurrentUser(role="admin", source="api_key", event_ids=[missing_id])
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=missing_id, user=client_user, db=db_session)
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert excinfo.value.detail == "Event not found"


def test_check_event_access_machine_client_out_of_scope(db_session: Session) -> None:
    owner = _create_user(db_session)
    event = _create_event(db_session, name="PyCon", created_by_user_id=owner.id)

    client_user = CurrentUser(
        role="admin", source="api_key", event_ids=[event.id + 1000]
    )
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=event.id, user=client_user, db=db_session)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Client is not authorized to access this event"


def test_check_event_access_human_admin_unconditional(db_session: Session) -> None:
    owner = _create_user(db_session)
    admin_user_row = _create_user(db_session, email="admin@example.com", role="admin")
    event = _create_event(db_session, name="FOSSASIA", created_by_user_id=owner.id)

    admin_user = CurrentUser(user_id=admin_user_row.id, role="admin", source="jwt")
    assert (
        check_event_access(event_id=event.id, user=admin_user, db=db_session) is event
    )


def test_check_event_access_event_creator(db_session: Session) -> None:
    owner = _create_user(db_session)
    event = _create_event(db_session, name="OpenTech", created_by_user_id=owner.id)

    creator_user = CurrentUser(user_id=owner.id, role="organizer", source="cookie")
    assert (
        check_event_access(event_id=event.id, user=creator_user, db=db_session) is event
    )


def test_check_event_access_non_creator_forbidden(db_session: Session) -> None:
    owner = _create_user(db_session)
    actor = _create_user(db_session, email="actor@example.com")
    event = _create_event(db_session, name="OpenTech", created_by_user_id=owner.id)

    other_user = CurrentUser(user_id=actor.id, role="organizer", source="cookie")
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=event.id, user=other_user, db=db_session)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "User is not authorized to access this event"


def test_check_event_access_no_creator_regular_user_forbidden(
    db_session: Session,
) -> None:
    event = _create_event(db_session, name="LegacyEvent", created_by_user_id=None)

    reg_user = CurrentUser(user_id=1, role="user", source="jwt")
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=event.id, user=reg_user, db=db_session)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# FastAPI TestClient Integration Tests
# ---------------------------------------------------------------------------


def test_fastapi_route_auth_integration(db_session: Session) -> None:
    test_app = FastAPI()

    owner = _create_user(db_session, email="owner@test.com", role="organizer")
    event = _create_event(db_session, name="Test Conf", created_by_user_id=owner.id)
    _create_client(db_session, api_key="good-key", event_ids=[event.id])
    regular_user = _create_user(db_session, email="user@test.com", role="user")

    @test_app.get("/events/{event_id}")
    def get_event_endpoint(
        event: Annotated[Event, Depends(require_event_access())],
    ) -> dict[str, int | str]:
        return {"id": event.id, "name": event.name}

    @test_app.get("/organizer-only")
    def org_endpoint(
        user: Annotated[CurrentUser, Depends(require_role(AuthRole.ORGANIZER))],
    ) -> dict[str, int | str | None]:
        return {"user_id": user.user_id, "role": user.role}

    test_app.dependency_overrides[get_db] = lambda: db_session

    client = TestClient(test_app)

    # 1. Unauthenticated request to /events/{id} -> 401
    resp = client.get(f"/events/{event.id}")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"

    # 2. Authenticated via API key (in scope) -> 200
    resp = client.get(f"/events/{event.id}", headers={"X-API-Key": "good-key"})
    assert resp.status_code == 200
    assert resp.json() == {"id": event.id, "name": "Test Conf"}

    # 3. Authenticated via cookie for event creator -> 200
    session_token = create_session_token(user_id=owner.id, role="organizer")
    resp = client.get(f"/events/{event.id}", cookies={"veditor_session": session_token})
    assert resp.status_code == 200
    assert resp.json()["id"] == event.id

    # 4. Role check passes for organizer
    resp = client.get("/organizer-only", cookies={"veditor_session": session_token})
    assert resp.status_code == 200
    assert resp.json() == {"user_id": owner.id, "role": "organizer"}

    # 5. Role check fails for regular user
    user_token = create_session_token(user_id=regular_user.id, role="user")
    resp = client.get("/organizer-only", cookies={"veditor_session": user_token})
    assert resp.status_code == 403


def test_current_user_is_human_admin() -> None:
    machine = CurrentUser(role="admin", source="api_key")
    assert machine.is_admin
    assert not machine.is_human_admin

    human_admin = CurrentUser(role="admin", source="jwt", user_id=1)
    assert human_admin.is_admin
    assert human_admin.is_human_admin


def test_require_event_access_invalid_path_param_format() -> None:
    test_app = FastAPI()

    @test_app.get("/events/{event_id}")
    def endpoint(
        event: Annotated[Event, Depends(require_event_access())],
    ) -> dict[str, int]:
        return {"id": event.id}

    test_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="admin", source="jwt"
    )

    client = TestClient(test_app)
    resp = client.get("/events/not-an-int")
    assert resp.status_code == 400
    assert "Invalid event ID" in resp.json()["detail"]


def test_require_event_access_query_param_bypass_prevented(db_session: Session) -> None:
    test_app = FastAPI()

    # Route defines {id} as the event path parameter
    @test_app.get("/events/{id}")
    def endpoint(
        event: Annotated[Event, Depends(require_event_access("id"))],
    ) -> dict[str, int]:
        return {"id": event.id}

    event = _create_event(db_session, name="Target Event")

    test_app.dependency_overrides[get_db] = lambda: db_session
    # Caller is authorized for a different event, but requesting the path event
    client_user = CurrentUser(
        role="admin", source="api_key", event_ids=[event.id + 1000]
    )
    test_app.dependency_overrides[get_current_user] = lambda: client_user

    client = TestClient(test_app)
    # Query parameter event_id must NOT bypass path parameter
    resp = client.get(f"/events/{event.id}?event_id={event.id + 1000}")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Client is not authorized to access this event"


def test_get_current_user_bearer_invalid_header_format(db_session: Session) -> None:
    request = MagicMock()
    request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
    request.cookies = {}

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=request, db=db_session)
    assert excinfo.value.status_code == 401
    assert "Invalid authorization header format" in excinfo.value.detail
    assert (excinfo.value.headers or {}).get("WWW-Authenticate") == "Bearer"


def test_get_current_user_cookie_fail_fast_over_bearer(db_session: Session) -> None:
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer valid-looking-token"}
    mock_request.cookies = {"veditor_session": "malformed.session.token"}

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=mock_request, db=db_session)
    assert excinfo.value.status_code == 401
    assert "session token" in excinfo.value.detail.lower()


def test_lock_active_admins(db_session: Session) -> None:
    admin_one = _create_user(db_session, email="admin1@example.com", role="admin")
    admin_two = _create_user(db_session, email="admin2@example.com", role="admin")
    _create_user(
        db_session,
        email="inactive-admin@example.com",
        role="admin",
        is_active=False,
    )

    admin_ids = lock_active_admins(db_session)
    assert admin_ids == sorted([admin_one.id, admin_two.id])


def test_require_talk_access_invalid_path_param_format() -> None:
    test_app = FastAPI()

    @test_app.get("/talks/{talk_id}")
    def endpoint(
        talk: Annotated[Talk, Depends(require_talk_access())],
    ) -> dict[str, int]:
        return {"id": talk.id}

    test_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="admin", source="jwt"
    )

    client = TestClient(test_app)
    resp = client.get("/talks/not-an-int")
    assert resp.status_code == 400
    assert "Invalid talk ID" in resp.json()["detail"]


def test_require_talk_access_success_and_unauthorized(db_session: Session) -> None:
    test_app = FastAPI()

    @test_app.get("/talks/{id}")
    def endpoint(
        talk: Annotated[Talk, Depends(require_talk_access("id"))],
    ) -> dict[str, int]:
        return {"id": talk.id}

    owner = _create_user(db_session)
    event = _create_event(db_session, created_by_user_id=owner.id)
    talk = _create_talk(db_session, event.id)
    test_app.dependency_overrides[get_db] = lambda: db_session

    # Authorized user (admin)
    test_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="admin", source="jwt"
    )
    client = TestClient(test_app)
    resp = client.get(f"/talks/{talk.id}")
    assert resp.status_code == 200
    assert resp.json() == {"id": talk.id}

    # Unauthorized machine client (talk.event_id not in client event_ids)
    test_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="organizer", source="api_key", event_ids=[talk.event_id + 1000]
    )
    resp = client.get(f"/talks/{talk.id}")
    assert resp.status_code == 403


def test_login_routes_next_redirect_and_open_redirect_protection(
    db_session: Session,
) -> None:
    app.dependency_overrides[get_db] = lambda: db_session

    user = User(
        email=f"next_test_{uuid.uuid4().hex[:6]}@example.com",
        hashed_password=hash_password("Pass1234!"),
        role="organizer",
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    client = TestClient(app)

    # 1. Login page passes next parameter to template context
    resp_page = client.get("/login?next=/studio/events")
    assert resp_page.status_code == 200
    assert '<input type="hidden" name="next" value="/studio/events">' in resp_page.text

    # 2. Login submit with next redirects to the specified route (HTTP 303)
    resp_next = client.post(
        "/login",
        data={
            "email": user.email,
            "password": "Pass1234!",
            "next": "/studio/events",
        },
        follow_redirects=False,
    )
    assert resp_next.status_code == 303
    assert resp_next.headers["location"] == "/studio/events"

    # 3. Missing next parameter defaults to /studio
    resp_default = client.post(
        "/login",
        data={"email": user.email, "password": "Pass1234!"},
        follow_redirects=False,
    )
    assert resp_default.status_code == 303
    assert resp_default.headers["location"] == "/studio"

    # 4. Open-redirect prevention: external URL is rejected and safely defaults to /studio
    resp_malicious_ext = client.post(
        "/login",
        data={
            "email": user.email,
            "password": "Pass1234!",
            "next": "https://attacker.com",
        },
        follow_redirects=False,
    )
    assert resp_malicious_ext.status_code == 303
    assert resp_malicious_ext.headers["location"] == "/studio"

    # 5. Open-redirect prevention: protocol-relative URL is rejected and safely defaults to /studio
    resp_malicious_proto = client.post(
        "/login",
        data={
            "email": user.email,
            "password": "Pass1234!",
            "next": "//attacker.com",
        },
        follow_redirects=False,
    )
    assert resp_malicious_proto.status_code == 303
    assert resp_malicious_proto.headers["location"] == "/studio"

    # 6. Auth loop prevention: case-insensitive auth paths (/Login, /LOGOUT, /Signup) safely default to /studio
    for auth_target in ("/Login", "/LOGOUT", "/Signup", "/login/", "/Logout/"):
        resp_loop = client.post(
            "/login",
            data={
                "email": user.email,
                "password": "Pass1234!",
                "next": auth_target,
            },
            follow_redirects=False,
        )
        assert resp_loop.status_code == 303
        assert resp_loop.headers["location"] == "/studio"

    # 7. Failed login re-renders page preserving next hidden input
    resp_fail = client.post(
        "/login",
        data={
            "email": user.email,
            "password": "WrongPassword!",
            "next": "/studio/talks/10",
        },
        follow_redirects=False,
    )
    assert resp_fail.status_code == 400
    assert (
        '<input type="hidden" name="next" value="/studio/talks/10">' in resp_fail.text
    )


def test_get_client_throttles_last_used_at_updates(db_session: Session) -> None:
    now = datetime.now(UTC)
    recent = _create_client(
        db_session,
        api_key="recent-key",
        last_used_at=now - timedelta(seconds=60),
    )
    recent_last_used = recent.last_used_at
    assert recent_last_used is not None

    resolved = get_client(api_key="recent-key", db=db_session)
    assert resolved.id == recent.id
    db_session.expire_all()
    reloaded_recent = db_session.get(Client, recent.id)
    assert reloaded_recent is not None
    assert reloaded_recent.last_used_at == recent_last_used

    stale = _create_client(
        db_session,
        api_key="stale-key",
        last_used_at=now - timedelta(seconds=350),
    )
    stale_last_used = stale.last_used_at
    assert stale_last_used is not None

    with patch(
        "app.auth.SessionLocal",
        side_effect=lambda: TestSessionLocal(bind=db_session.get_bind()),
    ):
        resolved_stale = get_client(api_key="stale-key", db=db_session)
    assert resolved_stale.id == stale.id

    db_session.expire_all()
    reloaded_stale = db_session.get(Client, stale.id)
    assert reloaded_stale is not None
    assert reloaded_stale.last_used_at is not None
    assert reloaded_stale.last_used_at > stale_last_used
