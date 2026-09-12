from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import models
from app.auth import CurrentUser, hash_api_key
from app.db import Base, SessionLocal, engine, get_db
from app.main import app
from app.security import create_access_token, create_session_token, hash_password


@pytest.fixture(scope="module", autouse=True)
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def db_session():
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
            for obj in reversed(created):
                try:
                    db.delete(obj)
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
            try:
                db.query(models.User).filter(
                    models.User.email.like("%@admin-test.com")
                ).delete()
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.close()


def _create_test_user(
    db, email: str, role: str = "user", is_active: bool = True
) -> models.User:
    user = models.User(
        email=email,
        hashed_password=hash_password("Password123!"),
        role=role,
        is_active=is_active,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_admin_routes_unauthenticated(client: TestClient):
    # GET /admin/users
    res = client.get("/admin/users")
    assert res.status_code == 401

    # POST /admin/users/{id}/promote
    res = client.post("/admin/users/1/promote", json={"role": "organizer"})
    assert res.status_code == 401

    # POST /admin/users/{id}/deactivate
    res = client.post("/admin/users/1/deactivate")
    assert res.status_code == 401


def test_admin_routes_forbidden_for_non_admin(client: TestClient, db_session):
    regular_user = _create_test_user(db_session, "regular@admin-test.com", role="user")
    token = create_session_token(regular_user.id, regular_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get("/admin/users")
    assert res.status_code == 403
    assert "admin" in res.text

    organizer_user = _create_test_user(
        db_session, "organizer@admin-test.com", role="organizer"
    )
    token_org = create_session_token(organizer_user.id, organizer_user.role)
    client.cookies.set("veditor_session", token_org)

    res = client.get("/admin/users")
    assert res.status_code == 403


def test_admin_users_list_success_and_pagination(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin1@admin-test.com", role="admin")
    u1 = _create_test_user(db_session, "alpha@admin-test.com", role="user")
    u2 = _create_test_user(db_session, "beta@admin-test.com", role="organizer")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get("/admin/users?skip=0&limit=10")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    user_ids = [u["id"] for u in data]
    assert admin_user.id in user_ids
    assert u1.id in user_ids
    assert u2.id in user_ids
    # Check ordering by id
    assert user_ids == sorted(user_ids)

    # Check serialization fields
    target_data = next(u for u in data if u["id"] == u1.id)
    assert target_data["email"] == "alpha@admin-test.com"
    assert target_data["role"] == "user"
    assert target_data["is_active"] is True
    assert "created_at" in target_data

    # Test limit validation
    res_invalid_limit = client.get("/admin/users?limit=101")
    assert res_invalid_limit.status_code == 422

    res_invalid_skip = client.get("/admin/users?skip=-1")
    assert res_invalid_skip.status_code == 422


def test_admin_routes_reject_api_key(client: TestClient, db_session):
    client_obj = models.Client(
        hashed_key=hash_api_key("test-admin-api-key"),
        event_ids=[],
    )
    db_session.add(client_obj)
    db_session.commit()

    res = client.get(
        "/admin/users",
        headers={"X-API-Key": "test-admin-api-key"},
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Operation requires a human administrator"


def test_promote_user_not_found(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_p1@admin-test.com", role="admin")
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post("/admin/users/999999/promote", json={"role": "organizer"})
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_promote_user_invalid_role_payload(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_p2@admin-test.com", role="admin")
    target_user = _create_test_user(db_session, "target_p2@admin-test.com", role="user")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Only "organizer" and "admin" are permitted in schemas.UserPromoteRequest
    res = client.post(
        f"/admin/users/{target_user.id}/promote", json={"role": "invalid"}
    )
    assert res.status_code == 422


def test_promote_user_success(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_p3@admin-test.com", role="admin")
    target_user = _create_test_user(db_session, "target_p3@admin-test.com", role="user")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post(
        f"/admin/users/{target_user.id}/promote", json={"role": "organizer"}
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == target_user.id
    assert data["role"] == "organizer"

    db_session.refresh(target_user)
    assert target_user.role == "organizer"

    # Now promote to admin
    res2 = client.post(f"/admin/users/{target_user.id}/promote", json={"role": "admin"})
    assert res2.status_code == 200
    assert res2.json()["role"] == "admin"


def test_promote_guard_cannot_demote_last_admin(client: TestClient, db_session):
    # Ensure only 1 active admin exists in db
    db_session.query(models.User).filter(models.User.role == "admin").delete()
    db_session.commit()

    admin_user = _create_test_user(
        db_session, "sole_admin@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Demoting the sole active admin
    res = client.post(
        f"/admin/users/{admin_user.id}/promote", json={"role": "organizer"}
    )
    assert res.status_code == 400
    assert (
        res.json()["detail"]
        == "Cannot demote user; operation would leave zero active administrators"
    )


def test_promote_demote_admin_allowed_if_another_admin_exists(
    client: TestClient, db_session
):
    admin1 = _create_test_user(db_session, "admin_a@admin-test.com", role="admin")
    admin2 = _create_test_user(db_session, "admin_b@admin-test.com", role="admin")

    token = create_session_token(admin1.id, admin1.role)
    client.cookies.set("veditor_session", token)

    # Demoting admin2 should succeed because admin1 remains active
    res = client.post(f"/admin/users/{admin2.id}/promote", json={"role": "organizer"})
    assert res.status_code == 200
    assert res.json()["role"] == "organizer"

    db_session.refresh(admin2)
    assert admin2.role == "organizer"


def test_deactivate_self_prevented(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "self_deact@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post(f"/admin/users/{admin_user.id}/deactivate")
    assert res.status_code == 400
    assert res.json()["detail"] == "Cannot deactivate your own account"


def test_deactivate_user_not_found(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_d1@admin-test.com", role="admin")
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post("/admin/users/999999/deactivate")
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_deactivate_guard_cannot_deactivate_last_admin(client: TestClient, db_session):
    # Ensure only 1 active admin
    db_session.query(models.User).filter(models.User.role == "admin").delete()
    db_session.commit()

    sole_admin = _create_test_user(
        db_session, "sole_adm_d@admin-test.com", role="admin"
    )

    from app.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=99999, role="admin", source="cookie"
    )
    try:
        res = client.post(f"/admin/users/{sole_admin.id}/deactivate")
        assert res.status_code == 400
        assert res.json()["detail"] == "Cannot deactivate the last active administrator"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_deactivate_user_success(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_d2@admin-test.com", role="admin")
    target_user = _create_test_user(db_session, "target_d2@admin-test.com", role="user")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post(f"/admin/users/{target_user.id}/deactivate")
    assert res.status_code == 200
    assert res.json()["is_active"] is False

    db_session.refresh(target_user)
    assert target_user.is_active is False


def test_deactivate_admin_when_another_admin_exists(client: TestClient, db_session):
    admin1 = _create_test_user(db_session, "admin_x@admin-test.com", role="admin")
    admin2 = _create_test_user(db_session, "admin_y@admin-test.com", role="admin")

    token = create_session_token(admin1.id, admin1.role)
    client.cookies.set("veditor_session", token)

    res = client.post(f"/admin/users/{admin2.id}/deactivate")
    assert res.status_code == 200
    assert res.json()["is_active"] is False

    db_session.refresh(admin2)
    assert admin2.is_active is False


def test_deactivated_user_immediate_session_and_login_rejection(
    client: TestClient, db_session
):
    admin_user = _create_test_user(
        db_session, "admin_deact_mgr@admin-test.com", role="admin"
    )
    user_password = "KnownUserPassword123!"
    user = models.User(
        email="deact_target@admin-test.com",
        hashed_password=hash_password(user_password),
        role="user",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    # Generate session cookie and bearer JWT for the user
    cookie_token = create_session_token(user.id, user.role)
    bearer_token = create_access_token(user.id, user.email, user.role)

    # Verify that prior to deactivation, the user can authenticate with the cookie or bearer token
    # On an admin route, regular user auth succeeds (403 Forbidden due to role check, not 401 Unauthorized)
    client.cookies.set("veditor_session", cookie_token)
    res_cookie_pre = client.get("/admin/users")
    client.cookies.clear()
    assert res_cookie_pre.status_code == 403
    assert res_cookie_pre.json()["detail"] == "Operation requires a human administrator"

    res_bearer_pre = client.get(
        "/admin/users", headers={"Authorization": f"Bearer {bearer_token}"}
    )
    assert res_bearer_pre.status_code == 403
    assert res_bearer_pre.json()["detail"] == "Operation requires a human administrator"

    # Also verify login with password succeeds prior to deactivation
    res_login_pre = client.post(
        "/login",
        data={"email": user.email, "password": user_password},
        follow_redirects=False,
    )
    assert res_login_pre.status_code == 303

    res_token_pre = client.post(
        "/api/auth/token",
        json={"email": user.email, "password": user_password},
    )
    assert res_token_pre.status_code == 200

    # Admin calls POST /admin/users/{user.id}/deactivate
    admin_token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", admin_token)
    res_deact = client.post(f"/admin/users/{user.id}/deactivate")
    assert res_deact.status_code == 200
    assert res_deact.json()["is_active"] is False

    # Clear admin cookie from client
    client.cookies.clear()

    # Verify that immediately after deactivation:
    # - Request with the pre-existing session cookie returns HTTP 401 Unauthorized ("User account not found or inactive")
    client.cookies.set("veditor_session", cookie_token)
    res_cookie_post = client.get("/admin/users")
    client.cookies.clear()
    assert res_cookie_post.status_code == 401
    assert res_cookie_post.json()["detail"] == "User account not found or inactive"

    # - Request with the pre-existing JWT bearer token returns HTTP 401 Unauthorized
    res_bearer_post = client.get(
        "/admin/users", headers={"Authorization": f"Bearer {bearer_token}"}
    )
    assert res_bearer_post.status_code == 401
    assert res_bearer_post.json()["detail"] == "User account not found or inactive"

    # - POST /login with the user's password returns HTTP 400 Bad Request ("Invalid email or password.")
    res_login_post = client.post(
        "/login",
        data={"email": user.email, "password": user_password},
        follow_redirects=False,
    )
    assert res_login_post.status_code == 400
    assert "Invalid email or password." in res_login_post.text

    # - POST /api/auth/token returns HTTP 401 Unauthorized ("Invalid credentials")
    res_token_post = client.post(
        "/api/auth/token",
        json={"email": user.email, "password": user_password},
    )
    assert res_token_post.status_code == 401
    assert res_token_post.json()["detail"] == "Invalid credentials"


def test_concurrent_admin_demotion_prevents_zero_admins():
    import threading

    with SessionLocal() as s:
        s.query(models.User).filter(
            models.User.email.like("%@concurrent-test.com")
        ).delete()
        s.commit()
        admin1 = models.User(
            email="admin1@concurrent-test.com",
            hashed_password=hash_password("pw1"),
            role="admin",
            is_active=True,
        )
        admin2 = models.User(
            email="admin2@concurrent-test.com",
            hashed_password=hash_password("pw2"),
            role="admin",
            is_active=True,
        )
        s.add_all([admin1, admin2])
        s.commit()
        id1, id2 = admin1.id, admin2.id

    token1 = create_session_token(id1, "admin")
    token2 = create_session_token(id2, "admin")

    results = []

    def demote_request(token, target_id):
        t_client = TestClient(app)
        t_client.cookies.set("veditor_session", token)
        res = t_client.post(
            f"/admin/users/{target_id}/promote", json={"role": "organizer"}
        )
        results.append((target_id, res.status_code, res.json()))

    t1 = threading.Thread(target=demote_request, args=(token1, id2))
    t2 = threading.Thread(target=demote_request, args=(token2, id1))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # One succeeds (200), and the other fails (either 400 or 403)
    status_codes = [r[1] for r in results]
    assert 200 in status_codes
    assert any(code in (400, 403) for code in status_codes)

    with SessionLocal() as s:
        remaining_admins = (
            s.query(models.User)
            .filter(
                models.User.email.like("%@concurrent-test.com"),
                models.User.role == "admin",
                models.User.is_active.is_(True),
            )
            .all()
        )
        assert len(remaining_admins) == 1
        s.query(models.User).filter(
            models.User.email.like("%@concurrent-test.com")
        ).delete()
        s.commit()
