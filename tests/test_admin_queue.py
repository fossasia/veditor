from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, engine, get_db
from app.main import app
from app.security import create_session_token, hash_password

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


def _create_user(db, email: str, role: str = "admin") -> models.User:
    user = models.User(
        email=email,
        hashed_password=hash_password("Password123!"),
        role=role,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_event(db, name: str) -> models.Event:
    event = models.Event(name=name)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db,
    event_id: int,
    title: str,
    status: str = "cutting",
    priority_rank: int | None = None,
) -> models.Talk:
    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event_id,
        title=title,
        room="Main Hall",
        start=now,
        end=now + timedelta(minutes=45),
        status=status,
        priority_rank=priority_rank,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def test_admin_queue_page_auth(client: TestClient, db_session):
    # Unauthenticated
    res = client.get("/admin/queue", follow_redirects=False)
    assert res.status_code in (401, 303)

    # Non-admin user (forbidden)
    regular = _create_user(db_session, "user@test.org", role="user")
    token = create_session_token(regular.id, regular.role)
    client.cookies.set("veditor_session", token)
    res = client.get("/admin/queue")
    assert res.status_code == 403

    # Admin user
    admin = _create_user(db_session, "admin@test.org", role="admin")
    admin_token = create_session_token(admin.id, admin.role)
    client.cookies.set("veditor_session", admin_token)
    res = client.get("/admin/queue")
    assert res.status_code == 200
    assert "Live Processing Queues" in res.text
    assert "Light Queue" in res.text
    assert "Heavy Queue" in res.text
    assert "Priority Queue" in res.text


def test_admin_api_queue_categorization(client: TestClient, db_session):
    admin = _create_user(db_session, "admin_api@test.org", role="admin")
    client.cookies.set("veditor_session", create_session_token(admin.id, admin.role))

    event = _create_event(db_session, "Test Conf 2026")
    t_light = _create_talk(db_session, event.id, "Light Talk 1", status="cutting")
    t_heavy = _create_talk(db_session, event.id, "Heavy Talk 1", status="transcoding")
    t_priority = _create_talk(
        db_session, event.id, "Priority Talk 1", status="cutting", priority_rank=1
    )
    # Talk that is done and not priority -> should vanish/not appear
    _create_talk(db_session, event.id, "Finished Talk", status="done")

    res = client.get("/admin/api/queue")
    assert res.status_code == 200
    data = res.json()

    assert "light" in data
    assert "heavy" in data
    assert "priority" in data

    light_ids = [t["id"] for t in data["light"]]
    heavy_ids = [t["id"] for t in data["heavy"]]
    priority_ids = [t["id"] for t in data["priority"]]

    assert t_light.id in light_ids
    assert t_heavy.id in heavy_ids
    assert t_priority.id in priority_ids

    # Validate talk item fields
    pri_item = next(t for t in data["priority"] if t["id"] == t_priority.id)
    assert pri_item["title"] == "Priority Talk 1"
    assert pri_item["priority_rank"] == 1
    assert "now_playing" in pri_item
    assert "upcoming_stages" in pri_item


def test_prioritize_and_deprioritize_api(client: TestClient, db_session):
    admin = _create_user(db_session, "admin_actions@test.org", role="admin")
    client.cookies.set("veditor_session", create_session_token(admin.id, admin.role))

    event = _create_event(db_session, "Action Conf 2026")
    talk1 = _create_talk(db_session, event.id, "Talk A", status="cutting")
    talk2 = _create_talk(db_session, event.id, "Talk B", status="cutting")

    # Prioritize talk1
    res = client.post(f"/admin/api/queue/talks/{talk1.id}/prioritize")
    assert res.status_code == 200
    assert res.json()["priority_rank"] == 1

    # Prioritize talk2
    res = client.post(f"/admin/api/queue/talks/{talk2.id}/prioritize")
    assert res.status_code == 200
    assert res.json()["priority_rank"] == 2

    # Deprioritize talk1 -> talk2 should become #1
    res = client.post(f"/admin/api/queue/talks/{talk1.id}/deprioritize")
    assert res.status_code == 200
    assert res.json()["priority_rank"] is None

    db_session.refresh(talk2)
    assert talk2.priority_rank == 1


def test_reorder_priority_queue_api(client: TestClient, db_session):
    admin = _create_user(db_session, "admin_reorder@test.org", role="admin")
    client.cookies.set("veditor_session", create_session_token(admin.id, admin.role))

    event = _create_event(db_session, "Reorder Conf 2026")
    t1 = _create_talk(db_session, event.id, "T1", status="cutting", priority_rank=1)
    t2 = _create_talk(db_session, event.id, "T2", status="cutting", priority_rank=2)
    t3 = _create_talk(db_session, event.id, "T3", status="cutting", priority_rank=3)

    # Reverse order: t3 -> t1 -> t2
    res = client.put(
        "/admin/api/queue/reorder",
        json={"ordered_talk_ids": [t3.id, t1.id, t2.id]},
    )
    assert res.status_code == 200

    db_session.refresh(t3)
    db_session.refresh(t1)
    db_session.refresh(t2)

    assert t3.priority_rank == 1
    assert t1.priority_rank == 2
    assert t2.priority_rank == 3


def test_reorder_ignores_inactive_talks(client: TestClient, db_session):
    admin = _create_user(db_session, "admin_inactive@test.org", role="admin")
    client.cookies.set("veditor_session", create_session_token(admin.id, admin.role))

    event = _create_event(db_session, "Inactive Test Conf")
    active_talk = _create_talk(db_session, event.id, "Active", status="cutting")
    done_talk = _create_talk(db_session, event.id, "Done", status="done")

    res = client.put(
        "/admin/api/queue/reorder",
        json={"ordered_talk_ids": [done_talk.id, active_talk.id]},
    )
    assert res.status_code == 200

    db_session.refresh(active_talk)
    db_session.refresh(done_talk)

    # Done talk must not be prioritized
    assert done_talk.priority_rank is None
    # Active talk should be assigned rank 1
    assert active_talk.priority_rank == 1
