from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.auth import CurrentUser, hash_api_key
from app.db import Base, SessionLocal, engine, get_db
from app.main import app
from app.security import create_access_token, create_session_token, hash_password

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

    # GET /admin/events
    res = client.get("/admin/events")
    assert res.status_code == 401

    # GET /admin/events/{id}
    res = client.get("/admin/events/1")
    assert res.status_code == 401


def test_admin_routes_forbidden_for_non_admin(client: TestClient, db_session):
    regular_user = _create_test_user(db_session, "regular@admin-test.com", role="user")
    token = create_session_token(regular_user.id, regular_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get("/admin/users")
    assert res.status_code == 403
    assert "admin" in res.text

    res = client.get("/admin/events")
    assert res.status_code == 403

    res = client.get("/admin/events/1")
    assert res.status_code == 403

    organizer_user = _create_test_user(
        db_session, "organizer@admin-test.com", role="organizer"
    )
    token_org = create_session_token(organizer_user.id, organizer_user.role)
    client.cookies.set("veditor_session", token_org)

    res = client.get("/admin/users")
    assert res.status_code == 403

    res = client.get("/admin/events")
    assert res.status_code == 403

    res = client.get("/admin/events/1")
    assert res.status_code == 403


def test_admin_users_list_success_and_pagination(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin1@admin-test.com", role="admin")
    u1 = _create_test_user(db_session, "alpha@admin-test.com", role="user")
    u2 = _create_test_user(db_session, "beta@admin-test.com", role="organizer")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    earlier_count = (
        db_session.query(models.User)
        .filter(models.User.id < min(admin_user.id, u1.id, u2.id))
        .count()
    )
    res = client.get(f"/admin/users?skip={earlier_count}&limit=10")
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

    res = client.get(
        "/admin/events",
        headers={"X-API-Key": "test-admin-api-key"},
    )
    assert res.status_code == 403
    assert res.json()["detail"] == "Operation requires a human administrator"

    res = client.get(
        "/admin/events/1",
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
    # Ensure only 1 active admin exists during the test without deleting pre-existing admins
    existing_admin_ids = []
    try:
        existing_admin_ids = [
            uid
            for (uid,) in db_session.query(models.User.id)
            .filter(models.User.role == "admin", models.User.is_active.is_(True))
            .all()
        ]
        if existing_admin_ids:
            db_session.query(models.User).filter(
                models.User.id.in_(existing_admin_ids)
            ).update({"is_active": False}, synchronize_session=False)
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
    finally:
        if existing_admin_ids:
            db_session.query(models.User).filter(
                models.User.id.in_(existing_admin_ids)
            ).update({"is_active": True}, synchronize_session=False)
            db_session.commit()


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
    # Ensure only 1 active admin exists during the test without deleting pre-existing admins
    from app.auth import get_current_user

    existing_admin_ids = []
    try:
        existing_admin_ids = [
            uid
            for (uid,) in db_session.query(models.User.id)
            .filter(models.User.role == "admin", models.User.is_active.is_(True))
            .all()
        ]
        if existing_admin_ids:
            db_session.query(models.User).filter(
                models.User.id.in_(existing_admin_ids)
            ).update({"is_active": False}, synchronize_session=False)
            db_session.commit()

        sole_admin = _create_test_user(
            db_session, "sole_adm_d@admin-test.com", role="admin"
        )

        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id=99999, role="admin", source="cookie"
        )
        res = client.post(f"/admin/users/{sole_admin.id}/deactivate")
        assert res.status_code == 400
        assert res.json()["detail"] == "Cannot deactivate the last active administrator"
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        if existing_admin_ids:
            db_session.query(models.User).filter(
                models.User.id.in_(existing_admin_ids)
            ).update({"is_active": True}, synchronize_session=False)
            db_session.commit()


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

    other_admin_ids = []
    with SessionLocal() as s:
        s.query(models.User).filter(
            models.User.email.like("%@concurrent-test.com")
        ).delete()
        other_admins = (
            s.query(models.User)
            .filter(
                models.User.role == "admin",
                ~models.User.email.like("%@concurrent-test.com"),
            )
            .all()
        )
        other_admin_ids = [u.id for u in other_admins]
        if other_admin_ids:
            s.query(models.User).filter(models.User.id.in_(other_admin_ids)).update(
                {"role": "organizer"}, synchronize_session=False
            )
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

    try:
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
    finally:
        with SessionLocal() as s:
            s.query(models.User).filter(
                models.User.email.like("%@concurrent-test.com")
            ).delete()
            if other_admin_ids:
                s.query(models.User).filter(models.User.id.in_(other_admin_ids)).update(
                    {"role": "admin"}, synchronize_session=False
                )
            s.commit()


def test_admin_events_list_empty(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_e1@admin-test.com", role="admin")
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    class EmptyDbSession:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def query(self, *args, **kwargs):
            if args and args[0] is models.User:
                return db_session.query(*args, **kwargs)

            class QueryMock:
                def outerjoin(self, *a, **kw):
                    return self

                def group_by(self, *a, **kw):
                    return self

                def order_by(self, *a, **kw):
                    return self

                def offset(self, *a, **kw):
                    return self

                def limit(self, *a, **kw):
                    return self

                def scalar(self):
                    return 0

                def first(self):
                    return None

                def all(self):
                    return []

            return QueryMock()

    app.dependency_overrides[get_db] = lambda: EmptyDbSession()
    try:
        res = client.get("/admin/events")
        assert res.status_code == 200
        assert "Events Overview" in res.text
        assert "Platform Events" in res.text
        assert "0 events" in res.text
        assert "No events found" in res.text
    finally:
        app.dependency_overrides[get_db] = lambda: db_session


def test_admin_events_list_and_aggregation(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_e2@admin-test.com", role="admin")
    creator_user = _create_test_user(
        db_session, "creator@admin-test.com", role="organizer"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    event1 = models.Event(
        name="FOSSASIA Summit 2026",
        created_by_user_id=creator_user.id,
    )
    event2 = models.Event(
        name="Empty Event 2026",
        created_by_user_id=creator_user.id,
    )
    db_session.add_all([event1, event2])
    db_session.commit()

    now = datetime.now(tz=UTC)
    talks = [
        models.Talk(
            event_id=event1.id,
            title="Keynote Talk",
            room="Main Hall",
            start=now,
            end=now + timedelta(minutes=45),
            status="done",
        ),
        models.Talk(
            event_id=event1.id,
            title="FastAPI Deep Dive",
            room="Room A",
            start=now + timedelta(hours=1),
            end=now + timedelta(hours=1, minutes=45),
            status="done",
        ),
        models.Talk(
            event_id=event1.id,
            title="Video Pipeline Architecture",
            room="Room B",
            start=now + timedelta(hours=2),
            end=now + timedelta(hours=2, minutes=45),
            status="cutting",
        ),
        models.Talk(
            event_id=event1.id,
            title="PyAV Bindings Review",
            room="Room C",
            start=now + timedelta(hours=3),
            end=now + timedelta(hours=3, minutes=45),
            status="pending_approval",
        ),
        models.Talk(
            event_id=event1.id,
            title="Hardware Acceleration Issues",
            room="Room D",
            start=now + timedelta(hours=4),
            end=now + timedelta(hours=4, minutes=45),
            status="broken",
        ),
    ]
    db_session.add_all(talks)
    db_session.commit()

    res = client.get("/admin/events")
    assert res.status_code == 200
    html = res.text

    # Verify event names and creator email
    assert "FOSSASIA Summit 2026" in html
    assert "Empty Event 2026" in html
    assert "creator@admin-test.com" in html

    # Verify aggregated stats for event 1 (5 talks, 2 done => 40.0%)
    assert "5 talks registered" in html
    assert 'value="40.0"' in html
    assert "40.0%" in html
    assert "2 Done" in html
    assert "1 Processing" in html
    assert "1 Pending" in html
    assert "1 Broken" in html

    # Verify empty event
    assert "0 talks registered" in html
    assert 'value="0"' in html
    assert "0%" in html
    assert "No talks" in html

    # Verify drilldown links
    assert f"/admin/events/{event1.id}" in html
    assert f"/admin/events/{event2.id}" in html


def test_admin_event_detail_not_found(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_e3@admin-test.com", role="admin")
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get("/admin/events/999999")
    assert res.status_code == 404
    assert res.json()["detail"] == "Event not found"


def test_admin_event_detail_success(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_e4@admin-test.com", role="admin")
    creator_user = _create_test_user(
        db_session, "creator2@admin-test.com", role="organizer"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    event = models.Event(
        name="Open Source Festival",
        created_by_user_id=creator_user.id,
    )
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk1 = models.Talk(
        event_id=event.id,
        title="Intro to Rust",
        room="Theater 1",
        start=now,
        end=now + timedelta(minutes=30),
        status="done",
    )
    talk2 = models.Talk(
        event_id=event.id,
        title="Scaling Microservices",
        room="Theater 2",
        start=now + timedelta(hours=1),
        end=now + timedelta(hours=1, minutes=30),
        status="preview",
    )
    db_session.add_all([talk1, talk2])
    db_session.commit()

    res = client.get(f"/admin/events/{event.id}")
    assert res.status_code == 200
    html = res.text

    # Header and metadata
    assert "Open Source Festival" in html
    assert f"#{event.id}" in html
    assert "creator2@admin-test.com" in html
    assert "2 talks" in html
    assert "50.0% Done" in html

    # Talk rows
    assert "Intro to Rust" in html
    assert "Theater 1" in html
    assert "Published" in html

    assert "Scaling Microservices" in html
    assert "Theater 2" in html
    assert "Preview Ready" in html

    # Quick action links to studio with from=admin query parameter
    assert f"/studio/talks/{talk1.id}?from=admin" in html
    assert f"/studio/talks/{talk2.id}?from=admin" in html


def test_admin_studio_editor_bypass(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_e5@admin-test.com", role="admin")
    other_organizer = _create_test_user(
        db_session, "other_org@admin-test.com", role="organizer"
    )

    event = models.Event(
        name="Independent Conference",
        created_by_user_id=other_organizer.id,
    )
    db_session.add(event)
    db_session.commit()

    now = datetime.now(tz=UTC)
    talk = models.Talk(
        event_id=event.id,
        title="Community Keynote",
        room="Auditorium",
        start=now,
        end=now + timedelta(minutes=30),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    # Admin visits Studio Editor for a talk they didn't create
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get(f"/studio/talks/{talk.id}")
    assert res.status_code == 200
    assert "Community Keynote" in res.text
    assert "Auditorium" in res.text


def test_admin_event_detail_empty_talks(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_empty_talks@admin-test.com", role="admin"
    )
    creator_user = _create_test_user(
        db_session, "creator_empty@admin-test.com", role="organizer"
    )
    event = models.Event(
        name="Empty Talks Conference",
        created_by_user_id=creator_user.id,
    )
    db_session.add(event)
    db_session.commit()

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get(f"/admin/events/{event.id}")
    assert res.status_code == 200
    assert "Empty Talks Conference" in res.text
    assert "0 talks" in res.text
    assert "0% Done" in res.text
    assert "No talks found" in res.text
    assert "This event does not have any talks scheduled or imported yet." in res.text


def test_admin_events_pagination(client: TestClient, db_session):
    admin_user = _create_test_user(db_session, "admin_pag@admin-test.com", role="admin")
    creator_user = _create_test_user(
        db_session, "creator_pag@admin-test.com", role="organizer"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Create 5 distinct events for this test
    created_events = []
    for i in range(5):
        ev = models.Event(
            name=f"Pagination Test Event {i:02d}",
            created_by_user_id=creator_user.id,
        )
        created_events.append(ev)
    db_session.add_all(created_events)
    db_session.commit()

    # Query with limit=2, page=1
    res1 = client.get("/admin/events?page=1&limit=2")
    assert res1.status_code == 200
    html1 = res1.text
    assert "Showing 1" in html1
    assert "Page 1 of" in html1
    assert "Next &rarr;" in html1
    assert "page=2&limit=2" in html1

    # Query with limit=2, page=2
    res2 = client.get("/admin/events?page=2&limit=2")
    assert res2.status_code == 200
    html2 = res2.text
    assert "Showing 3" in html2
    assert "Page 2 of" in html2
    assert "&larr; Previous" in html2
    assert "page=1&limit=2" in html2

    # Page exceeding total_pages returns 404
    res_page_overflow = client.get("/admin/events?page=999&limit=2")
    assert res_page_overflow.status_code == 404

    # Query parameter validation
    res_bad_page = client.get("/admin/events?page=0")
    assert res_bad_page.status_code == 422

    res_bad_limit = client.get("/admin/events?limit=101")
    assert res_bad_limit.status_code == 422

    res_bad_limit_zero = client.get("/admin/events?limit=0")
    assert res_bad_limit_zero.status_code == 422


def test_admin_users_html_view_rendering(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_ui_user@admin-test.com", role="admin"
    )
    managed_user = _create_test_user(
        db_session, "managed_one@admin-test.com", role="user"
    )
    _create_test_user(
        db_session, "managed_two@admin-test.com", role="organizer", is_active=False
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.get("/admin/users", headers={"Accept": "text/html"})
    assert res.status_code == 200
    html = res.text

    # Page structure & headers
    assert "Users Overview" in html
    assert "Platform Users" in html
    assert 'href="/admin/users"' in html

    # Macro stats
    assert "Total Users" in html
    assert "Active Accounts" in html
    assert "Inactive / Revoked" in html
    assert "Administrators" in html
    assert "Organizers" in html

    # Table columns
    assert "Email" in html
    assert "Role" in html
    assert "Status" in html
    assert "Created At" in html
    assert "Actions" in html

    # User rows & elements
    assert "admin_ui_user@admin-test.com" in html
    assert "managed_one@admin-test.com" in html
    assert "managed_two@admin-test.com" in html
    assert "badge-current-user" in html  # "(You)" badge for signed-in admin
    assert "Active" in html
    assert "Inactive" in html

    # Role action menu and toggle active buttons
    assert f'id="role-select-{managed_user.id}"' in html
    assert f'id="toggle-active-btn-{managed_user.id}"' in html
    assert "Deactivate" in html
    assert "Activate" in html

    # Script tag present
    assert '<script src="/static/js/admin_users.js"></script>' in html


def test_admin_users_search_filter_html_and_json(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_search_mgr@admin-test.com", role="admin"
    )
    _create_test_user(db_session, "findme_special@admin-test.com", role="user")
    _create_test_user(db_session, "other_normal@admin-test.com", role="user")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Search via JSON API
    res_json = client.get("/admin/users?search=findme_special")
    assert res_json.status_code == 200
    data = res_json.json()
    assert isinstance(data, list)
    matching_emails = [u["email"] for u in data]
    assert "findme_special@admin-test.com" in matching_emails
    assert "other_normal@admin-test.com" not in matching_emails

    # Search via HTML UI
    res_html = client.get(
        "/admin/users?search=findme_special", headers={"Accept": "text/html"}
    )
    assert res_html.status_code == 200
    html = res_html.text
    assert "findme_special@admin-test.com" in html
    assert "other_normal@admin-test.com" not in html
    assert 'matching "findme_special"' in html


def test_admin_users_search_empty_state(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_search_empty@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Empty search JSON
    res_json = client.get("/admin/users?search=thisemaildoesnotexistatall12345")
    assert res_json.status_code == 200
    assert res_json.json() == []

    # Empty search HTML
    res_html = client.get(
        "/admin/users?search=thisemaildoesnotexistatall12345",
        headers={"Accept": "text/html"},
    )
    assert res_html.status_code == 200
    html = res_html.text
    assert 'No users found matching "thisemaildoesnotexistatall12345"' in html
    assert "Clear search filter" in html


def test_activate_user_endpoint_success(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_act_mgr@admin-test.com", role="admin"
    )
    inactive_user = _create_test_user(
        db_session, "inactive_test@admin-test.com", role="user", is_active=False
    )
    assert inactive_user.is_active is False

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post(f"/admin/users/{inactive_user.id}/activate")
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == inactive_user.id
    assert data["is_active"] is True

    db_session.refresh(inactive_user)
    assert inactive_user.is_active is True


def test_activate_user_not_found(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_act_nf@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    res = client.post("/admin/users/999999/activate")
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"


def test_promote_demote_to_user_role(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_demote_mgr@admin-test.com", role="admin"
    )
    target_user = _create_test_user(
        db_session, "target_demote@admin-test.com", role="organizer"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Demote from organizer to user
    res = client.post(f"/admin/users/{target_user.id}/promote", json={"role": "user"})
    assert res.status_code == 200
    assert res.json()["role"] == "user"

    db_session.refresh(target_user)
    assert target_user.role == "user"


def test_promote_rejects_speaker_role(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_no_spk@admin-test.com", role="admin"
    )
    target_user = _create_test_user(
        db_session, "target_no_spk@admin-test.com", role="user"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Promoting to speaker via admin area must be rejected
    res = client.post(
        f"/admin/users/{target_user.id}/promote", json={"role": "speaker"}
    )
    assert res.status_code == 422


def test_admin_users_role_alias_endpoint(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_alias@admin-test.com", role="admin"
    )
    target_user = _create_test_user(
        db_session, "target_alias@admin-test.com", role="user"
    )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Test POST /admin/users/{id}/role alias
    res = client.post(f"/admin/users/{target_user.id}/role", json={"role": "organizer"})
    assert res.status_code == 200
    assert res.json()["role"] == "organizer"

    db_session.refresh(target_user)
    assert target_user.role == "organizer"


def test_admin_nav_includes_users_link(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_nav_test@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Verify /admin has Users link
    res_dash = client.get("/admin")
    assert res_dash.status_code == 200
    assert 'href="/admin/users"' in res_dash.text
    assert "Users" in res_dash.text

    # Verify /admin/events has Users link
    res_events = client.get("/admin/events")
    assert res_events.status_code == 200
    assert 'href="/admin/users"' in res_events.text


def test_admin_users_search_wildcard_escaping(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_escape_test@admin-test.com", role="admin"
    )
    _create_test_user(db_session, "user_abc@admin-test.com", role="user")
    _create_test_user(db_session, "userXabc@admin-test.com", role="user")

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Search with '_' which should only match literal '_' and NOT 'userXabc'
    res = client.get("/admin/users?search=user_abc")
    assert res.status_code == 200
    data = res.json()
    emails = [u["email"] for u in data]
    assert "user_abc@admin-test.com" in emails
    assert "userXabc@admin-test.com" not in emails

    # Search with '%' which should match nothing when no user has a literal '%'
    res_pct = client.get("/admin/users?search=%")
    assert res_pct.status_code == 200
    assert res_pct.json() == []


def test_admin_users_pagination_url_encoding(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_urlenc@admin-test.com", role="admin"
    )
    _create_test_user(db_session, "other_normal@admin-test.com", role="user")
    # Create 3 users matching a tag search
    for i in range(3):
        _create_test_user(
            db_session, f"tag_user_{i}+special@admin-test.com", role="user"
        )

    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # Search with '+' (URL encoded as %2B in the query string) and limit=1 to trigger pagination
    res = client.get(
        "/admin/users?page=1&limit=1&search=%2Bspecial",
        headers={"Accept": "text/html"},
    )
    assert res.status_code == 200
    html = res.text
    # Check that the Next link properly encodes '+special' as '%2Bspecial'
    assert "search=%2Bspecial" in html
    assert "other_normal@admin-test.com" not in html

    res_page2 = client.get(
        "/admin/users?page=2&limit=1&search=%2Bspecial",
        headers={"Accept": "text/html"},
    )
    assert res_page2.status_code == 200
    html_page2 = res_page2.text
    assert "tag_user_1+special@admin-test.com" in html_page2
    assert "other_normal@admin-test.com" not in html_page2
    assert 'of 3 users matching "+special"' in html_page2
    assert "search=%2Bspecial" in html_page2


def test_admin_users_html_skip_out_of_range_raises_404(client: TestClient, db_session):
    admin_user = _create_test_user(
        db_session, "admin_skip_test@admin-test.com", role="admin"
    )
    token = create_session_token(admin_user.id, admin_user.role)
    client.cookies.set("veditor_session", token)

    # HTML request with skip exceeding total users must return 404
    res = client.get("/admin/users?skip=999999", headers={"Accept": "text/html"})
    assert res.status_code == 404
    assert res.json()["detail"] == "Page not found"

    # HTML request where skip is within page 1 bounds (calculated_page=1 <= total_pages=1) but offset >= total_users
    res_search = client.get(
        f"/admin/users?search={admin_user.email}&skip=1",
        headers={"Accept": "text/html"},
    )
    assert res_search.status_code == 404
    assert res_search.json()["detail"] == "Page not found"
