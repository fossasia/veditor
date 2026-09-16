from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, engine, get_db
from app.main import app
from app.routes import admin as admin_routes
from app.security import create_session_token, hash_password
from app.storage import get_storage_backend
from tests.conftest import override_storage_backend

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


def create_user(db_session, email: str, role: str) -> models.User:
    user = models.User(
        email=email,
        hashed_password=hash_password("testpass123"),
        role=role,
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


def authenticate(client: TestClient, user: models.User) -> None:
    client.cookies.set("veditor_session", create_session_token(user.id, user.role))


def add_event(db_session, name: str, owner: models.User | None, statuses: list[str]):
    event = models.Event(name=name, created_by_user_id=owner.id if owner else None)
    db_session.add(event)
    db_session.flush()
    start = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    for i, talk_status in enumerate(statuses):
        db_session.add(
            models.Talk(
                event_id=event.id,
                title=f"{name} Talk {i}",
                start=start + timedelta(hours=i),
                end=start + timedelta(hours=i, minutes=45),
                status=talk_status,
            )
        )
    db_session.commit()
    return event


def stats_for(db_session, event_id: int) -> dict:
    row = db_session.execute(
        admin_routes._event_talk_stats_query().where(models.Event.id == event_id)
    ).one()
    return admin_routes._with_derived_stats(row)


def test_stats_query_aggregates_talk_statuses_per_event(db_session):
    organizer = create_user(db_session, "agg_org@example.com", "organizer")
    event = add_event(
        db_session,
        "Aggregation Summit",
        organizer,
        [
            "done",
            "done",
            "broken",
            "rejected",
            "transcoding",
            "cutting",
            "waiting_for_files",
            "pending_approval",
            "preview",
        ],
    )
    other = add_event(db_session, "Other Summit", organizer, ["broken", "broken"])

    stats = stats_for(db_session, event.id)
    assert stats["name"] == "Aggregation Summit"
    assert stats["owner_email"] == "agg_org@example.com"
    assert stats["total"] == 9
    assert stats["done"] == 2
    assert stats["broken"] == 1
    assert stats["rejected"] == 1
    assert stats["processing"] == 2
    assert stats["pending"] == 3

    other_stats = stats_for(db_session, other.id)
    assert other_stats["total"] == 2
    assert other_stats["broken"] == 2
    assert other_stats["pending"] == 0


def test_stats_query_keeps_events_without_talks_or_owner(db_session):
    event = add_event(db_session, "Empty Orphan Summit", None, [])

    stats = stats_for(db_session, event.id)
    assert stats["owner_email"] is None
    assert stats["total"] == 0
    assert stats["done"] == stats["broken"] == stats["pending"] == 0


def test_admin_events_requires_authentication(client: TestClient, db_session):
    assert client.get("/admin/events").status_code == 401
    assert client.get("/admin/events/1").status_code == 401


@pytest.mark.parametrize("role", ["user", "organizer"])
def test_admin_events_forbidden_for_non_admins(client, db_session, role):
    user = create_user(db_session, f"non_admin_{role}@example.com", role)
    event = add_event(db_session, f"{role} Own Summit", user, ["done"])
    authenticate(client, user)

    assert client.get("/admin/events").status_code == 403
    assert client.get(f"/admin/events/{event.id}").status_code == 403


def test_admin_sees_all_events_regardless_of_owner(client, db_session):
    admin = create_user(db_session, "global_admin@example.com", "admin")
    org1 = create_user(db_session, "global_org1@example.com", "organizer")
    org2 = create_user(db_session, "global_org2@example.com", "organizer")
    add_event(db_session, "Org1 Global Summit", org1, ["done", "broken"])
    add_event(db_session, "Org2 Global Summit", org2, ["cutting"])
    authenticate(client, admin)

    response = client.get("/admin/events")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Org1 Global Summit" in response.text
    assert "Org2 Global Summit" in response.text
    assert "global_org1@example.com" in response.text
    assert "1 broken" in response.text
    assert "1 processing" in response.text
    # Org1's bar is split evenly between its done and broken talks.
    assert 'class="seg-done" x="0" y="0" width="50.0"' in response.text
    assert 'class="seg-broken" x="50.0" y="0" width="50.0"' in response.text
    assert 'id="nav-admin-events-link"' in response.text


def test_admin_events_list_is_paginated(client, db_session, monkeypatch):
    monkeypatch.setattr(admin_routes, "EVENTS_PAGE_SIZE", 2)
    admin = create_user(db_session, "page_admin@example.com", "admin")
    for i in range(3):
        add_event(db_session, f"Paged Summit {i}", admin, [])
    authenticate(client, admin)

    total = db_session.query(models.Event).count()
    last_page = (total + 1) // 2

    # Newest events come first.
    first = client.get("/admin/events")
    assert "Paged Summit 2" in first.text
    assert "Paged Summit 1" in first.text
    assert "Paged Summit 0" not in first.text
    assert "/admin/events?page=2" in first.text

    # Out-of-range pages clamp to the last page instead of rendering empty.
    clamped = client.get("/admin/events?page=9999")
    assert clamped.status_code == 200
    assert f"Page {last_page} of {last_page}" in clamped.text


def test_admin_event_detail_lists_talks_with_studio_links(client, db_session):
    admin = create_user(db_session, "detail_admin@example.com", "admin")
    organizer = create_user(db_session, "detail_org@example.com", "organizer")
    event = add_event(
        db_session,
        "Drilldown Summit",
        organizer,
        ["done", "broken", "preview", "assembling"],
    )
    talks = db_session.query(models.Talk).filter_by(event_id=event.id).all()
    authenticate(client, admin)

    response = client.get(f"/admin/events/{event.id}")

    assert response.status_code == 200
    for talk in talks:
        assert talk.title in response.text
        assert f'href="/studio/talks/{talk.id}"' in response.text
    assert "Published" in response.text
    assert "Broken" in response.text
    assert "Preview Ready" in response.text
    assert (
        '<span class="badge badge-processing">'
        '<span class="spinner spinner-sm"></span>Assembling</span>'
    ) in response.text


def test_admin_event_detail_404_for_unknown_event(client, db_session):
    admin = create_user(db_session, "missing_admin@example.com", "admin")
    authenticate(client, admin)

    assert client.get("/admin/events/999999999").status_code == 404


def test_studio_link_honors_admin_bypass_for_foreign_event(
    client, db_session, fake_storage
):
    admin = create_user(db_session, "bypass_admin@example.com", "admin")
    organizer = create_user(db_session, "bypass_org@example.com", "organizer")
    event = add_event(db_session, "Bypass Summit", organizer, ["preview"])
    talk = db_session.query(models.Talk).filter_by(event_id=event.id).one()
    authenticate(client, admin)

    override_storage_backend(app, fake_storage)
    try:
        response = client.get(f"/studio/talks/{talk.id}")
    finally:
        app.dependency_overrides.pop(get_storage_backend, None)

    assert response.status_code == 200
    assert "Bypass Summit Talk 0" in response.text
