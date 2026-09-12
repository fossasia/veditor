from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key, require_event_access
from app.cli import create_admin
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
                db.query(models.Review).filter(
                    models.Review.talk.has(
                        models.Talk.event.has(models.Event.name.like("%Pipeline%"))
                    )
                ).delete(synchronize_session=False)
                db.query(models.Talk).filter(
                    models.Talk.event.has(models.Event.name.like("%Pipeline%"))
                ).delete(synchronize_session=False)
                db.query(models.Event).filter(
                    models.Event.name.like("%Pipeline%")
                ).delete(synchronize_session=False)
                db.query(models.Client).filter(
                    models.Client.hashed_key == hash_api_key("scoped-pipeline-api-key")
                ).delete(synchronize_session=False)
                db.query(models.User).filter(
                    models.User.email.like("%@pipeline-test.com")
                ).delete(synchronize_session=False)
                db.query(models.User).filter(
                    models.User.email == "root_admin@test.com"
                ).delete(synchronize_session=False)
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.close()


# Mount event endpoint using require_event_access dependency for integration gating
if not any(
    getattr(route, "path", None) == "/events/{event_id}" for route in app.routes
):

    @app.get("/events/{event_id}")
    def get_event_endpoint(
        event: Annotated[models.Event, Depends(require_event_access())],
    ):
        return {
            "id": event.id,
            "name": event.name,
            "created_by_user_id": event.created_by_user_id,
        }


def test_complete_user_lifecycle_and_pipeline_auth(
    client: TestClient, db_session: Session
):
    """
    End-to-end integration test covering complete user lifecycle and pipeline authentication/authorization:
    a) Public registration: POST /signup creates user with role="user"
    b) Initial admin assignment: Provision admin via create_admin
    c) Admin lists users via GET /admin/users
    d) Role elevation: Admin calls POST /admin/users/{user1.id}/promote with role="organizer"
    e) Event & talk creation: Organizer 1 creates an event and a talk (Event.created_by_user_id == user1.id)
    f) Permission denial boundaries:
       - Unauthenticated request returns 401
       - Organizer 2 (different organizer) is denied access (403 Forbidden)
       - Regular user is denied access (403 Forbidden)
       - Admin has unconditional access (200 OK)
       - Scoped API key client access: in-scope event returns 200, out-of-scope event returns 403
    g) Review attribution: models.Review with talk_id and user_id, verified in DB
    h) Invalidation: Admin deactivates Organizer 1, pre-existing cookies and bearer tokens rejected (401)
    """
    # -------------------------------------------------------------------------
    # a) Public registration: POST /signup creates user with role="user"
    # -------------------------------------------------------------------------
    signup_resp = client.post(
        "/signup",
        data={
            "email": "user1@pipeline-test.com",
            "password": "User1Password123!",
            "password_confirm": "User1Password123!",
        },
        follow_redirects=False,
    )
    assert signup_resp.status_code == 303
    client.cookies.clear()

    user1 = (
        db_session.query(models.User)
        .filter(models.User.email == "user1@pipeline-test.com")
        .first()
    )
    assert user1 is not None
    assert user1.email == "user1@pipeline-test.com"
    assert user1.role == "user"
    assert user1.is_active is True

    # -------------------------------------------------------------------------
    # b) Initial admin assignment: Provision admin via create_admin
    # -------------------------------------------------------------------------
    create_admin(db_session, "root_admin@test.com", "SecretPass123!")
    admin = (
        db_session.query(models.User)
        .filter(models.User.email == "root_admin@test.com")
        .first()
    )
    assert admin is not None
    assert admin.email == "root_admin@test.com"
    assert admin.role == "admin"
    assert admin.is_active is True

    # -------------------------------------------------------------------------
    # c) Admin lists users via GET /admin/users
    # -------------------------------------------------------------------------
    admin_token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", admin_token)
    list_resp = client.get("/admin/users")
    client.cookies.clear()

    assert list_resp.status_code == 200
    users_data = list_resp.json()
    emails = [u["email"] for u in users_data]
    assert "root_admin@test.com" in emails
    assert "user1@pipeline-test.com" in emails

    # -------------------------------------------------------------------------
    # d) Role elevation: Admin calls POST /admin/users/{user1.id}/promote with role="organizer"
    # -------------------------------------------------------------------------
    client.cookies.set("veditor_session", admin_token)
    promote_resp = client.post(
        f"/admin/users/{user1.id}/promote",
        json={"role": "organizer"},
    )
    client.cookies.clear()

    assert promote_resp.status_code == 200
    assert promote_resp.json()["role"] == "organizer"
    db_session.refresh(user1)
    assert user1.role == "organizer"

    # -------------------------------------------------------------------------
    # e) Event & talk creation: Organizer 1 creates an event and a talk
    # -------------------------------------------------------------------------
    event1 = models.Event(
        name="Pipeline Test Event 1",
        created_by_user_id=user1.id,
    )
    db_session.add(event1)
    db_session.commit()
    db_session.refresh(event1)

    assert event1.created_by_user_id == user1.id
    assert event1.created_by_user == user1
    assert event1 in user1.events

    now = datetime.now(UTC)
    talk1 = models.Talk(
        event_id=event1.id,
        title="Pipeline Test Talk 1",
        room="Auditorium A",
        start=now,
        end=now + timedelta(hours=1),
        status="preview",
    )
    db_session.add(talk1)
    db_session.commit()
    db_session.refresh(talk1)

    assert talk1.id is not None
    assert talk1.event_id == event1.id
    assert talk1.event == event1

    # -------------------------------------------------------------------------
    # f) Permission denial boundaries:
    # -------------------------------------------------------------------------
    # 1. Unauthenticated request to event/talk endpoint returns 401
    res_unauth = client.get(f"/events/{event1.id}")
    assert res_unauth.status_code == 401
    assert res_unauth.headers.get("www-authenticate") == "Bearer"

    # Organizer 1 (creator) has access with cookie and bearer token
    org1_cookie = create_session_token(user1.id, user1.role)
    client.cookies.set("veditor_session", org1_cookie)
    res_org1_cookie = client.get(f"/events/{event1.id}")
    client.cookies.clear()
    assert res_org1_cookie.status_code == 200
    assert res_org1_cookie.json()["id"] == event1.id
    assert res_org1_cookie.json()["created_by_user_id"] == user1.id

    org1_bearer = create_access_token(user1.id, user1.email, user1.role)
    res_org1_bearer = client.get(
        f"/events/{event1.id}", headers={"Authorization": f"Bearer {org1_bearer}"}
    )
    assert res_org1_bearer.status_code == 200
    assert res_org1_bearer.json()["id"] == event1.id

    # 2. Organizer 2 (different organizer) is denied access (403 Forbidden) to Organizer 1's event
    org2 = models.User(
        email="org2@pipeline-test.com",
        hashed_password=hash_password("Pass12345!"),
        role="organizer",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(org2)
    db_session.commit()
    db_session.refresh(org2)

    org2_cookie = create_session_token(org2.id, org2.role)
    client.cookies.set("veditor_session", org2_cookie)
    res_org2_cookie = client.get(f"/events/{event1.id}")
    client.cookies.clear()
    assert res_org2_cookie.status_code == 403
    assert (
        res_org2_cookie.json()["detail"]
        == "User is not authorized to access this event"
    )

    org2_bearer = create_access_token(org2.id, org2.email, org2.role)
    res_org2_bearer = client.get(
        f"/events/{event1.id}", headers={"Authorization": f"Bearer {org2_bearer}"}
    )
    assert res_org2_bearer.status_code == 403
    assert (
        res_org2_bearer.json()["detail"]
        == "User is not authorized to access this event"
    )

    # 3. Regular user is denied access (403 Forbidden)
    regular_user = models.User(
        email="regular@pipeline-test.com",
        hashed_password=hash_password("Pass12345!"),
        role="user",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(regular_user)
    db_session.commit()
    db_session.refresh(regular_user)

    reg_cookie = create_session_token(regular_user.id, regular_user.role)
    client.cookies.set("veditor_session", reg_cookie)
    res_reg_cookie = client.get(f"/events/{event1.id}")
    client.cookies.clear()
    assert res_reg_cookie.status_code == 403
    assert (
        res_reg_cookie.json()["detail"] == "User is not authorized to access this event"
    )

    reg_bearer = create_access_token(
        regular_user.id, regular_user.email, regular_user.role
    )
    res_reg_bearer = client.get(
        f"/events/{event1.id}", headers={"Authorization": f"Bearer {reg_bearer}"}
    )
    assert res_reg_bearer.status_code == 403
    assert (
        res_reg_bearer.json()["detail"] == "User is not authorized to access this event"
    )

    # 4. Admin has unconditional access (200 OK)
    client.cookies.set("veditor_session", admin_token)
    res_admin_cookie = client.get(f"/events/{event1.id}")
    client.cookies.clear()
    assert res_admin_cookie.status_code == 200
    assert res_admin_cookie.json()["id"] == event1.id

    admin_bearer = create_access_token(admin.id, admin.email, admin.role)
    res_admin_bearer = client.get(
        f"/events/{event1.id}", headers={"Authorization": f"Bearer {admin_bearer}"}
    )
    assert res_admin_bearer.status_code == 200

    # 5. Scoped API key client access: in-scope event returns 200, out-of-scope event returns 403
    api_key_scoped = "scoped-pipeline-api-key"
    client_scoped = models.Client(
        hashed_key=hash_api_key(api_key_scoped),
        event_ids=[event1.id],
    )
    db_session.add(client_scoped)

    event2 = models.Event(
        name="Pipeline Test Event 2",
        created_by_user_id=org2.id,
    )
    db_session.add(event2)
    db_session.commit()
    db_session.refresh(client_scoped)
    db_session.refresh(event2)

    # In-scope event returns 200
    res_scoped_in = client.get(
        f"/events/{event1.id}", headers={"X-API-Key": api_key_scoped}
    )
    assert res_scoped_in.status_code == 200
    assert res_scoped_in.json()["id"] == event1.id

    # Out-of-scope event returns 403
    res_scoped_out = client.get(
        f"/events/{event2.id}", headers={"X-API-Key": api_key_scoped}
    )
    assert res_scoped_out.status_code == 403
    assert (
        res_scoped_out.json()["detail"]
        == "Client is not authorized to access this event"
    )

    # -------------------------------------------------------------------------
    # g) Review attribution:
    # -------------------------------------------------------------------------
    review = models.Review(
        talk_id=talk1.id,
        decision="approve",
        user_id=user1.id,
    )
    db_session.add(review)
    db_session.commit()
    db_session.refresh(review)

    assert review.user_id == user1.id
    assert review.user is not None
    assert review.user.email == user1.email
    assert review in user1.reviews

    # -------------------------------------------------------------------------
    # h) Invalidation:
    # -------------------------------------------------------------------------
    # Admin deactivates Organizer 1
    client.cookies.set("veditor_session", admin_token)
    deact_resp = client.post(f"/admin/users/{user1.id}/deactivate")
    client.cookies.clear()

    assert deact_resp.status_code == 200
    assert deact_resp.json()["is_active"] is False
    db_session.refresh(user1)
    assert user1.is_active is False

    # Pre-existing cookies and bearer tokens for Organizer 1 are immediately rejected (401)
    client.cookies.set("veditor_session", org1_cookie)
    res_deact_cookie = client.get(f"/events/{event1.id}")
    client.cookies.clear()
    assert res_deact_cookie.status_code == 401
    assert res_deact_cookie.json()["detail"] == "User account not found or inactive"

    res_deact_bearer = client.get(
        f"/events/{event1.id}", headers={"Authorization": f"Bearer {org1_bearer}"}
    )
    assert res_deact_bearer.status_code == 401
    assert res_deact_bearer.json()["detail"] == "User account not found or inactive"
