from typing import Annotated
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, status

from app.auth import get_client, hash_api_key, lock_active_admins, verify_event_access
from app.models import Client, User


def test_hash_api_key():
    key1 = "some-random-key"
    key2 = "some-random-key"
    key3 = "another-key"
    assert hash_api_key(key1) == hash_api_key(key2)
    assert hash_api_key(key1) != hash_api_key(key3)
    # Ensure it's 64 chars for SHA-256
    assert len(hash_api_key(key1)) == 64


def test_get_client_missing_key():
    with pytest.raises(HTTPException) as excinfo:
        get_client(None, MagicMock())
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Missing API Key"


def test_get_client_invalid_key():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        get_client("invalid-key", mock_db)

    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"


def test_get_client_valid_key():
    mock_client = Client(id=1, hashed_key="hashed", event_ids=[1, 2])
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_client

    client = get_client("valid-key", mock_db)
    assert client == mock_client


def test_verify_event_access_in_scope():
    mock_client = Client(id=1, event_ids=[1, 2, 3])
    # Should not raise any exception
    verify_event_access(2, mock_client)


def test_verify_event_access_out_of_scope():
    mock_client = Client(id=1, event_ids=[1, 2, 3])
    with pytest.raises(HTTPException) as excinfo:
        verify_event_access(4, mock_client)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Client is not authorized to access this event"


# ---------------------------------------------------------------------------
# CurrentUser Model Tests
# ---------------------------------------------------------------------------


from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import (
    CurrentUser,
    check_event_access,
    get_current_user,
    require_admin,
    require_event_access,
    require_role,
)
from app.models import Event
from app.security import create_access_token, create_session_token


def test_current_user_model_attributes():
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


def test_get_current_user_api_key_valid():
    mock_client = Client(id=1, hashed_key=hash_api_key("secret-key"), event_ids=[42])
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_client

    user = get_current_user(api_key="secret-key", db=mock_db)
    assert user.source == "api_key"
    assert user.role == "admin"
    assert user.user_id is None
    assert user.email is None
    assert user.event_ids == [42]
    assert user.is_machine


def test_get_current_user_api_key_invalid():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(api_key="bad-key", db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_valid():
    token = create_session_token(user_id=7, role="organizer")
    mock_user = User(id=7, email="org@example.com", role="organizer", is_active=True)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_user

    user = get_current_user(cookie_token=token, db=mock_db)
    assert user.source == "cookie"
    assert user.user_id == 7
    assert user.email == "org@example.com"
    assert user.role == "organizer"
    assert user.event_ids == []
    assert not user.is_machine


def test_get_current_user_session_cookie_invalid():
    mock_db = MagicMock()
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(cookie_token="not.a.valid.jwt", db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired session token" in excinfo.value.detail
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_user_inactive():
    token = create_session_token(user_id=7, role="user")
    mock_user = User(id=7, email="inactive@example.com", role="user", is_active=False)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_user

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(cookie_token=token, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "inactive" in excinfo.value.detail.lower()
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"


def test_get_current_user_session_cookie_user_not_found():
    token = create_session_token(user_id=99, role="user")
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(cookie_token=token, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "not found" in excinfo.value.detail.lower()
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"
    assert "not found" in excinfo.value.detail.lower()


def test_get_current_user_bearer_token_valid():
    token = create_access_token(user_id=15, email="dev@example.com", role="admin")
    mock_creds = MagicMock()
    mock_creds.credentials = token
    mock_user = User(id=15, email="dev@example.com", role="admin", is_active=True)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_user

    user = get_current_user(bearer_creds=mock_creds, db=mock_db)
    assert user.source == "jwt"
    assert user.user_id == 15
    assert user.email == "dev@example.com"
    assert user.role == "admin"
    assert user.is_admin


def test_get_current_user_bearer_token_invalid():
    mock_creds = MagicMock()
    mock_creds.credentials = "invalid.bearer.token"
    mock_db = MagicMock()

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(bearer_creds=mock_creds, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Invalid or expired access token" in excinfo.value.detail


def test_get_current_user_bearer_token_user_inactive():
    token = create_access_token(user_id=15, email="dev@example.com", role="admin")
    mock_creds = MagicMock()
    mock_creds.credentials = token
    mock_user = User(id=15, email="dev@example.com", role="admin", is_active=False)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_user

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(bearer_creds=mock_creds, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert "inactive" in excinfo.value.detail.lower()


def test_get_current_user_no_credentials():
    mock_db = MagicMock()
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Not authenticated"
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"


def test_get_current_user_precedence_api_key_over_cookie():
    # When both API key and cookie are provided, API key is evaluated first.
    mock_client = Client(id=1, hashed_key=hash_api_key("api-key"), event_ids=[1])
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_client

    token = create_session_token(user_id=2, role="user")
    user = get_current_user(api_key="api-key", cookie_token=token, db=mock_db)
    assert user.source == "api_key"


def test_get_current_user_fail_fast_on_invalid_api_key():
    # If API key is provided but invalid, fails immediately with 401 instead of falling back.
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None

    token = create_session_token(user_id=2, role="user")
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(api_key="bad-key", cookie_token=token, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == "Invalid API Key"


# ---------------------------------------------------------------------------
# Role Hierarchy Tests
# ---------------------------------------------------------------------------


def test_require_role_invalid_role():
    with pytest.raises(ValueError) as excinfo:
        require_role("superadmin")
    assert "Invalid min_role" in str(excinfo.value)


def test_require_role_hierarchy():
    check_user = require_role("user")
    check_org = require_role("organizer")
    check_admin = require_role("admin")

    regular_user = CurrentUser(role="user", source="jwt")
    organizer_user = CurrentUser(role="organizer", source="jwt")
    admin_user = CurrentUser(role="admin", source="jwt")

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


def test_require_admin():
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


def test_check_event_access_not_found():
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = None
    user = CurrentUser(role="admin", source="jwt")

    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=99, user=user, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
    assert excinfo.value.detail == "Event not found"


def test_check_event_access_machine_client_in_scope():
    event = Event(id=5, name="PyCon", created_by_user_id=1)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    client_user = CurrentUser(role="admin", source="api_key", event_ids=[4, 5, 6])
    assert check_event_access(event_id=5, user=client_user, db=mock_db) == event


def test_check_event_access_machine_client_out_of_scope():
    event = Event(id=10, name="PyCon", created_by_user_id=1)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    client_user = CurrentUser(role="admin", source="api_key", event_ids=[1, 2, 3])
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=10, user=client_user, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "Client is not authorized to access this event"


def test_check_event_access_human_admin_unconditional():
    event = Event(id=20, name="FOSSASIA", created_by_user_id=99)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    admin_user = CurrentUser(user_id=5, role="admin", source="jwt")
    assert check_event_access(event_id=20, user=admin_user, db=mock_db) == event


def test_check_event_access_event_creator():
    event = Event(id=30, name="OpenTech", created_by_user_id=12)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    creator_user = CurrentUser(user_id=12, role="organizer", source="cookie")
    assert check_event_access(event_id=30, user=creator_user, db=mock_db) == event


def test_check_event_access_non_creator_forbidden():
    event = Event(id=30, name="OpenTech", created_by_user_id=12)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    other_user = CurrentUser(user_id=13, role="organizer", source="cookie")
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=30, user=other_user, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN
    assert excinfo.value.detail == "User is not authorized to access this event"


def test_check_event_access_no_creator_regular_user_forbidden():
    event = Event(id=40, name="LegacyEvent", created_by_user_id=None)
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = event

    reg_user = CurrentUser(user_id=1, role="user", source="jwt")
    with pytest.raises(HTTPException) as excinfo:
        check_event_access(event_id=40, user=reg_user, db=mock_db)
    assert excinfo.value.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# FastAPI TestClient Integration Tests
# ---------------------------------------------------------------------------


def test_fastapi_route_auth_integration():
    test_app = FastAPI()

    mock_event = Event(id=1, name="Test Conf", created_by_user_id=10)

    @test_app.get("/events/{event_id}")
    def get_event_endpoint(event: Annotated[Event, Depends(require_event_access())]):
        return {"id": event.id, "name": event.name}

    @test_app.get("/organizer-only")
    def org_endpoint(user: Annotated[CurrentUser, Depends(require_role("organizer"))]):
        return {"user_id": user.user_id, "role": user.role}

    # Override get_db to return a mock DB session
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.return_value = mock_event

    from app.db import get_db

    test_app.dependency_overrides[get_db] = lambda: mock_db

    client = TestClient(test_app)

    # 1. Unauthenticated request to /events/1 -> 401
    resp = client.get("/events/1")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"

    # 2. Authenticated via API key (in scope) -> 200
    mock_client = Client(id=1, hashed_key=hash_api_key("good-key"), event_ids=[1])
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_client,  # get_current_user client lookup
        mock_event,  # check_event_access event lookup
    ]
    resp = client.get("/events/1", headers={"X-API-Key": "good-key"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 1, "name": "Test Conf"}

    # 3. Authenticated via cookie for event creator -> 200
    session_token = create_session_token(user_id=10, role="organizer")
    creator_user = User(id=10, email="owner@test.com", role="organizer", is_active=True)
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        creator_user,  # user lookup
        mock_event,  # event lookup
    ]
    resp = client.get("/events/1", cookies={"veditor_session": session_token})
    assert resp.status_code == 200
    assert resp.json()["id"] == 1

    # 4. Role check passes for organizer
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        creator_user,
    ]
    resp = client.get("/organizer-only", cookies={"veditor_session": session_token})
    assert resp.status_code == 200
    assert resp.json() == {"user_id": 10, "role": "organizer"}

    # 5. Role check fails for regular user
    user_token = create_session_token(user_id=20, role="user")
    regular_user = User(id=20, email="user@test.com", role="user", is_active=True)
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        regular_user,
    ]
    resp = client.get("/organizer-only", cookies={"veditor_session": user_token})
    assert resp.status_code == 403


def test_current_user_is_human_admin():
    machine = CurrentUser(role="admin", source="api_key")
    assert machine.is_admin
    assert not machine.is_human_admin

    human_admin = CurrentUser(role="admin", source="jwt", user_id=1)
    assert human_admin.is_admin
    assert human_admin.is_human_admin


def test_require_event_access_invalid_path_param_format():
    test_app = FastAPI()

    @test_app.get("/events/{event_id}")
    def endpoint(event: Annotated[Event, Depends(require_event_access())]):
        return {"id": event.id}

    test_app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="admin", source="jwt"
    )

    client = TestClient(test_app)
    resp = client.get("/events/not-an-int")
    assert resp.status_code == 400
    assert "Invalid event ID" in resp.json()["detail"]


def test_require_event_access_query_param_bypass_prevented():
    test_app = FastAPI()

    # Route defines {id} as the event path parameter
    @test_app.get("/events/{id}")
    def endpoint(event: Annotated[Event, Depends(require_event_access("id"))]):
        return {"id": event.id}

    mock_event_2 = Event(id=2, name="Target Event", created_by_user_id=99)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.first.side_effect = [
        mock_event_2,
    ]

    from app.db import get_db

    test_app.dependency_overrides[get_db] = lambda: mock_db
    # Caller is authorized for event 1, but requesting /events/2?event_id=1
    client_user = CurrentUser(role="admin", source="api_key", event_ids=[1])
    test_app.dependency_overrides[get_current_user] = lambda: client_user

    client = TestClient(test_app)
    # Query parameter event_id=1 must NOT bypass path parameter {id}=2
    resp = client.get("/events/2?event_id=1")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Client is not authorized to access this event"


def test_get_current_user_bearer_invalid_header_format():
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Basic dXNlcjpwYXNz"}
    mock_request.cookies = {}

    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=mock_request)
    assert excinfo.value.status_code == 401
    assert "Invalid authorization header format" in excinfo.value.detail
    assert excinfo.value.headers.get("WWW-Authenticate") == "Bearer"


def test_get_current_user_cookie_fail_fast_over_bearer():
    mock_request = MagicMock()
    mock_request.headers = {"Authorization": "Bearer valid-looking-token"}
    mock_request.cookies = {"veditor_session": "malformed.session.token"}

    mock_db = MagicMock()
    with pytest.raises(HTTPException) as excinfo:
        get_current_user(request=mock_request, db=mock_db)
    assert excinfo.value.status_code == 401
    assert "session token" in excinfo.value.detail.lower()


def test_lock_active_admins_mock():
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.order_by.return_value.with_for_update.return_value.all.return_value = [
        (1,),
        (2,),
    ]
    admin_ids = lock_active_admins(mock_session)
    assert admin_ids == [1, 2]
