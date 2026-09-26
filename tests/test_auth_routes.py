import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, engine, get_db
from app.main import app
from app.security import (
    create_session_token,
    decode_access_token,
    hash_password,
    verify_password,
)

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


def test_get_login_unauthenticated(client: TestClient):
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign In" in response.text
    assert '<input type="email"' in response.text
    assert '<input type="password"' in response.text


def test_get_login_already_authenticated(client: TestClient, db_session):
    user = models.User(
        email="auth_user@test.com",
        hashed_password=hash_password("password123"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/studio"


def test_post_login_success(client: TestClient, db_session):
    user = models.User(
        email="login_success@test.com",
        hashed_password=hash_password("Secret12345!"),
        role="organizer",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/login",
        data={"email": "login_success@test.com", "password": "Secret12345!"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio"
    assert "veditor_session" in response.cookies


def test_post_login_case_insensitive_email(client: TestClient, db_session):
    user = models.User(
        email="case_test@test.com",
        hashed_password=hash_password("Secret12345!"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/login",
        data={"email": "CASE_TEST@test.com", "password": "Secret12345!"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/studio"


def test_post_login_invalid_password(client: TestClient, db_session):
    user = models.User(
        email="wrong_pass@test.com",
        hashed_password=hash_password("correct-password"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/login",
        data={"email": "wrong_pass@test.com", "password": "incorrect-password"},
    )
    assert response.status_code == 400
    assert "Invalid email or password" in response.text
    assert "wrong_pass@test.com" in response.text


def test_post_login_inactive_user(client: TestClient, db_session):
    user = models.User(
        email="inactive@test.com",
        hashed_password=hash_password("password123"),
        role="user",
        is_active=False,
    )
    db_session.add(user)
    db_session.commit()

    response = client.post(
        "/login",
        data={"email": "inactive@test.com", "password": "password123"},
    )
    assert response.status_code == 400
    assert "Invalid email or password" in response.text


def test_post_login_unknown_user(client: TestClient):
    response = client.post(
        "/login",
        data={"email": "nonexistent@test.com", "password": "password123"},
    )
    assert response.status_code == 400
    assert "Invalid email or password" in response.text


def test_post_login_missing_fields(client: TestClient):
    response = client.post("/login", data={"email": "", "password": ""})
    assert response.status_code == 400
    assert "Invalid email or password" in response.text


def test_get_signup_unauthenticated(client: TestClient):
    response = client.get("/signup")
    assert response.status_code == 200
    assert "Create Account" in response.text
    assert "password_confirm" in response.text


def test_get_signup_already_authenticated(client: TestClient, db_session):
    user = models.User(
        email="signup_auth@test.com",
        hashed_password=hash_password("password123"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    response = client.get("/signup", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/studio"


def test_post_signup_validation_errors(client: TestClient):
    # Invalid email regex
    res = client.post(
        "/signup",
        data={
            "email": "invalid-email",
            "password": "validpassword8",
            "password_confirm": "validpassword8",
        },
    )
    assert res.status_code == 400
    assert "email" in res.text.lower()

    # Email too long (> 255 chars)
    res = client.post(
        "/signup",
        data={
            "email": f"{'a' * 250}@test.com",
            "password": "validpassword8",
            "password_confirm": "validpassword8",
        },
    )
    assert res.status_code == 400
    assert "valid email" in res.text.lower()

    # Password too short (< 8 chars)
    res = client.post(
        "/signup",
        data={
            "email": "valid@test.com",
            "password": "short",
            "password_confirm": "short",
        },
    )
    assert res.status_code == 400
    assert "8 characters" in res.text

    # Password too long (> 256 chars)
    res = client.post(
        "/signup",
        data={
            "email": "valid@test.com",
            "password": "p" * 257,
            "password_confirm": "p" * 257,
        },
    )
    assert res.status_code == 400
    assert "256 characters" in res.text

    # Password mismatch
    res = client.post(
        "/signup",
        data={
            "email": "valid@test.com",
            "password": "password123",
            "password_confirm": "mismatch123",
        },
    )
    assert res.status_code == 400
    assert "match" in res.text.lower()


def test_post_signup_duplicate_email(client: TestClient, db_session):
    user = models.User(
        email="dup_user@test.com",
        hashed_password=hash_password("password123"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    res = client.post(
        "/signup",
        data={
            "email": "DUP_USER@test.com",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert res.status_code == 400
    assert "already exists" in res.text.lower()


def test_post_signup_creates_user_role(client: TestClient, db_session):
    res = client.post(
        "/signup",
        data={
            "email": "first_user@test.com",
            "password": "userpassword123",
            "password_confirm": "userpassword123",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/studio"
    assert "veditor_session" in res.cookies

    user = (
        db_session.query(models.User)
        .filter(models.User.email == "first_user@test.com")
        .first()
    )
    assert user is not None
    assert user.role == "user"
    assert verify_password("userpassword123", user.hashed_password)


def test_post_signup_subsequent_user_becomes_user(client: TestClient, db_session):
    # Ensure at least one user exists
    first = models.User(
        email="existing_admin@test.com",
        hashed_password=hash_password("password123"),
        role="admin",
        is_active=True,
    )
    db_session.add(first)
    db_session.commit()

    res = client.post(
        "/signup",
        data={
            "email": "second_user@test.com",
            "password": "userpassword123",
            "password_confirm": "userpassword123",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/studio"

    user = (
        db_session.query(models.User)
        .filter(models.User.email == "second_user@test.com")
        .first()
    )
    assert user is not None
    assert user.role == "user"


def test_logout_post_and_get(client: TestClient):
    client.cookies.set("veditor_session", "dummy-session-token")

    # POST /logout
    res_post = client.post("/logout", follow_redirects=False)
    assert res_post.status_code == 303
    assert res_post.headers["location"] == "/login"

    # GET /logout
    res_get = client.get("/logout", follow_redirects=False)
    assert res_get.status_code == 303
    assert res_get.headers["location"] == "/login"


def test_api_auth_token_json_success(client: TestClient, db_session):
    user = models.User(
        email="api_user@test.com",
        hashed_password=hash_password("api_pass123"),
        role="organizer",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    # Using email
    res = client.post(
        "/api/auth/token",
        json={"email": "api_user@test.com", "password": "api_pass123"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["expires_in"] > 0

    claims = decode_access_token(data["access_token"])
    assert claims is not None
    assert claims["user_id"] == user.id
    assert claims["role"] == "organizer"

    # Using username
    res_user = client.post(
        "/api/auth/token",
        json={"username": "api_user@test.com", "password": "api_pass123"},
    )
    assert res_user.status_code == 200


def test_api_auth_token_form_success(client: TestClient, db_session):
    user = models.User(
        email="form_user@test.com",
        hashed_password=hash_password("form_pass123"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    res = client.post(
        "/api/auth/token",
        data={"username": "form_user@test.com", "password": "form_pass123"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_api_auth_token_invalid_credentials(client: TestClient, db_session):
    user = models.User(
        email="token_invalid@test.com",
        hashed_password=hash_password("correct-password"),
        role="user",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    # Wrong password
    res = client.post(
        "/api/auth/token",
        json={"email": "token_invalid@test.com", "password": "wrong-password"},
    )
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"

    # Inactive user
    user.is_active = False
    db_session.commit()
    res_inactive = client.post(
        "/api/auth/token",
        json={"email": "token_invalid@test.com", "password": "correct-password"},
    )
    assert res_inactive.status_code == 401
    assert res_inactive.headers["www-authenticate"] == "Bearer"

    # Nonexistent user
    res_unknown = client.post(
        "/api/auth/token",
        json={"email": "nonexistent@test.com", "password": "password123"},
    )
    assert res_unknown.status_code == 401
    assert res_unknown.headers["www-authenticate"] == "Bearer"


def test_api_auth_token_fallback_handles_runtime_error(client: TestClient, monkeypatch):
    from starlette.requests import Request

    async def mock_json(self):
        raise RuntimeError("Stream consumed")

    monkeypatch.setattr(Request, "json", mock_json)

    res = client.post(
        "/api/auth/token",
        data={"unrelated": "data"},
    )
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"


def test_templating_auth_context_processor(client: TestClient, db_session):
    user = models.User(
        email="context_user@test.com",
        hashed_password=hash_password("password123"),
        role="admin",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()

    token = create_session_token(user.id, user.role)
    client.cookies.set("veditor_session", token)

    # When accessing /studio, base.html is rendered and user email should appear
    res = client.get("/studio")
    assert res.status_code == 200
    assert "context_user@test.com" in res.text
    assert "Admin" in res.text
    assert "Log out" in res.text
    assert "api-key-btn" not in res.text
    assert "api-key-indicator" not in res.text
    assert "modal-api-key" not in res.text


def test_templating_unauthenticated_navbar(client: TestClient):
    # Unauthenticated studio access shows Log in and Sign up, without API key badge or modal
    res = client.get("/studio")
    assert res.status_code == 200
    assert "Log in" in res.text
    assert "Sign up" in res.text
    assert "api-key-btn" not in res.text
    assert "api-key-indicator" not in res.text
    assert "modal-api-key" not in res.text


def test_post_signup_always_defaults_to_user_role(client: TestClient, db_session):
    res = client.post(
        "/signup",
        data={
            "email": "organizer_signup@test.com",
            "password": "orgpassword123",
            "password_confirm": "orgpassword123",
            "role": "organizer",
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert res.headers["location"] == "/studio"

    user = (
        db_session.query(models.User)
        .filter(models.User.email == "organizer_signup@test.com")
        .first()
    )
    assert user is not None
    assert user.role == "user"
