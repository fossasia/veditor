"""Integration tests for Platform client and external event/talk integration (Issue #238).

Covers:
- Platform API client provisioning via CLI and DB.
- Platform client wildcard authorization across events and talks.
- External event resolution by slug (source="platform", external_id=slug) and auto-provisioning.
- In-memory batch deduplication and idempotent talk upsert by external_id.
- Event SSO token minting via external slug and talk SSO token via external_id.
- Isolation and standalone client compatibility.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models
from app.auth import hash_api_key
from app.db import SessionLocal, get_db
from app.main import app
from app.security import create_session_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_dependency_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def db_session():
    db = SessionLocal()
    app.dependency_overrides[get_db] = lambda: db
    created = []
    orig_add = db.add

    def track_add(instance, *args, **kwargs):
        created.append(instance)
        return orig_add(instance, *args, **kwargs)

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
        finally:
            db.close()


@pytest.fixture
def platform_client_and_key(db_session: Session) -> tuple[models.Client, str]:
    raw_key = "platform_secret_key_1234567890"
    hashed = hash_api_key(raw_key)
    c = models.Client(
        name="Platform Integration",
        is_platform=True,
        hashed_key=hashed,
        event_ids=[],
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c, raw_key


@pytest.fixture
def scoped_client_and_key(db_session: Session) -> tuple[models.Client, str]:
    raw_key = "scoped_secret_key_1234567890"
    hashed = hash_api_key(raw_key)
    c = models.Client(
        name="Scoped Client",
        is_platform=False,
        hashed_key=hashed,
        event_ids=[],
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c, raw_key


def test_schedule_import_auto_provisions_event_by_slug(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """POST /talks/schedule/import with an external event slug dynamically provisions the event."""
    _, api_key = platform_client_and_key

    payload = {
        "event_id": "codemania-2026",
        "event_name": "Codemania Auckland 2026",
        "source": "platform",
        "talks": [
            {
                "external_id": "TALK_01",
                "title": "Async Python Mastery",
                "room": "Room A",
                "start": "2026-09-20T10:00:00Z",
                "end": "2026-09-20T11:00:00Z",
            }
        ],
    }

    response = client.post(
        "/talks/schedule/import",
        json=payload,
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "ok"
    assert data["imported_count"] == 1
    assert data["external_id"] == "codemania-2026"
    assert data["source"] == "platform"
    internal_event_id = data["event_id"]

    event = (
        db_session.query(models.Event)
        .filter(models.Event.id == internal_event_id)
        .first()
    )
    assert event is not None
    assert event.name == "Codemania Auckland 2026"
    assert event.external_id == "codemania-2026"
    assert event.source == "platform"

    talk = (
        db_session.query(models.Talk)
        .filter(
            models.Talk.event_id == internal_event_id,
            models.Talk.external_id == "TALK_01",
        )
        .first()
    )
    assert talk is not None
    assert talk.title == "Async Python Mastery"
    assert talk.room == "Room A"


def test_schedule_import_is_idempotent_and_updates_in_place(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """Re-syncing an updated schedule updates talk title, room, and times in place without duplicates."""
    _, api_key = platform_client_and_key

    # 1. Initial sync
    initial_payload = {
        "event_id": "pycon-apac-2026",
        "event_name": "PyCon APAC 2026",
        "talks": [
            {
                "external_id": "3DDH7B",
                "title": "Original Title",
                "room": "Room 101",
                "start": "2026-10-01T09:00:00Z",
                "end": "2026-10-01T09:45:00Z",
            },
            {
                "external_id": "8KLP9Q",
                "title": "Another Keynote",
                "room": "Main Stage",
                "start": "2026-10-01T10:00:00Z",
                "end": "2026-10-01T10:45:00Z",
            },
        ],
    }
    resp1 = client.post(
        "/talks/schedule/import",
        json=initial_payload,
        headers={"X-API-Key": api_key},
    )
    assert resp1.status_code == 200
    ev_id = resp1.json()["event_id"]

    orig_talk1 = (
        db_session.query(models.Talk)
        .filter(models.Talk.event_id == ev_id, models.Talk.external_id == "3DDH7B")
        .first()
    )
    orig_talk1_id = orig_talk1.id

    # 2. Re-sync with modified title, changed room, and rescheduled time for 3DDH7B
    updated_payload = {
        "event_id": "pycon-apac-2026",
        "event_name": "PyCon APAC 2026",
        "talks": [
            {
                "external_id": "3DDH7B",
                "title": "Renamed & Advanced Title",
                "room": "Auditorium Grand",
                "start": "2026-10-01T14:00:00Z",
                "end": "2026-10-01T15:00:00Z",
            },
            {
                "external_id": "8KLP9Q",
                "title": "Another Keynote",
                "room": "Main Stage",
                "start": "2026-10-01T10:00:00Z",
                "end": "2026-10-01T10:45:00Z",
            },
        ],
    }
    resp2 = client.post(
        "/talks/schedule/import",
        json=updated_payload,
        headers={"X-API-Key": api_key},
    )
    assert resp2.status_code == 200
    assert resp2.json()["imported_count"] == 2

    # Verify total talk count is unchanged
    all_talks = (
        db_session.query(models.Talk).filter(models.Talk.event_id == ev_id).all()
    )
    assert len(all_talks) == 2

    # Verify talk 3DDH7B was updated in place with identical primary key
    db_session.expire_all()
    updated_talk1 = (
        db_session.query(models.Talk)
        .filter(models.Talk.event_id == ev_id, models.Talk.external_id == "3DDH7B")
        .first()
    )
    assert updated_talk1.id == orig_talk1_id
    assert updated_talk1.title == "Renamed & Advanced Title"
    assert updated_talk1.room == "Auditorium Grand"
    assert updated_talk1.start == datetime(2026, 10, 1, 14, 0, 0, tzinfo=UTC)
    assert updated_talk1.end == datetime(2026, 10, 1, 15, 0, 0, tzinfo=UTC)


def test_schedule_import_batch_deduplication(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """Duplicate talks in incoming payload are deduplicated in-memory, retaining the latest entry."""
    _, api_key = platform_client_and_key

    payload = {
        "event_id": "dedup-conf-2026",
        "talks": [
            {
                "external_id": "DUP01",
                "title": "First Version",
                "room": "Room 1",
                "start": "2026-11-01T09:00:00Z",
                "end": "2026-11-01T10:00:00Z",
            },
            {
                "external_id": "DUP01",
                "title": "Overwritten Latest Version",
                "room": "Room 2",
                "start": "2026-11-01T09:30:00Z",
                "end": "2026-11-01T10:30:00Z",
            },
        ],
    }

    response = client.post(
        "/talks/schedule/import",
        json=payload,
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["imported_count"] == 1

    ev_id = data["event_id"]
    talks = db_session.query(models.Talk).filter(models.Talk.event_id == ev_id).all()
    assert len(talks) == 1
    assert talks[0].title == "Overwritten Latest Version"
    assert talks[0].room == "Room 2"


def test_event_sso_token_via_slug_with_platform_client(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """POST /events/{event_slug}/sso-token resolves event by slug and issues organizer SSO token."""
    _, api_key = platform_client_and_key

    # Provision event
    event = models.Event(
        name="FOSSASIA 2026",
        source="platform",
        external_id="fossasia-2026",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    response = client.post(
        "/events/fossasia-2026/sso-token",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["scope_type"] == "event"
    assert data["scope_id"] == event.id
    assert data["role"] == "organizer"
    assert f"/studio?event_id={event.id}&sso_token=" in data["url"]


def test_talk_sso_token_via_external_id_with_platform_client(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """POST /talks/{external_id}/sso-token resolves talk by external_id and issues speaker SSO token."""
    _, api_key = platform_client_and_key

    event = models.Event(
        name="Global Summit",
        source="platform",
        external_id="summit-2026",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event.id,
        external_id="TALK_KEYNOTE_99",
        title="Opening Keynote",
        room="Hall 1",
        start=now,
        end=now + timedelta(hours=1),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()
    db_session.refresh(talk)

    response = client.post(
        "/talks/TALK_KEYNOTE_99/sso-token",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["scope_type"] == "talk"
    assert data["scope_id"] == talk.id
    assert data["role"] == "speaker"
    assert f"/studio/talks/{talk.id}?sso_token=" in data["url"]


def test_get_talk_by_external_id(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """GET /talks/{external_id} allows retrieving talk details via external_id."""
    _, api_key = platform_client_and_key

    event = models.Event(name="Demo Conf", source="platform", external_id="demo-2026")
    db_session.add(event)
    db_session.commit()

    now = datetime.now(UTC)
    talk = models.Talk(
        event_id=event.id,
        external_id="EXT_TALK_123",
        title="Demo Session",
        room="Room 42",
        start=now,
        end=now + timedelta(minutes=45),
        status="waiting_for_files",
    )
    db_session.add(talk)
    db_session.commit()

    response = client.get(
        "/talks/EXT_TALK_123",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == talk.id
    assert data["title"] == "Demo Session"
    assert data["external_id"] == "EXT_TALK_123"


def test_scoped_client_cannot_access_arbitrary_event_or_slug(
    db_session: Session, scoped_client_and_key: tuple[models.Client, str]
):
    """Non-platform scoped clients cannot access events outside their client.event_ids."""
    scoped_client, api_key = scoped_client_and_key

    event = models.Event(
        name="Private Conference",
        source="platform",
        external_id="private-2026",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    # Scoped client does not have event.id in event_ids
    assert event.id not in scoped_client.event_ids

    response = client.post(
        "/events/private-2026/sso-token",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 403
    assert "Client is not authorized" in response.json()["detail"]


def test_platform_client_lists_all_events(
    db_session: Session, platform_client_and_key: tuple[models.Client, str]
):
    """Platform client calling GET /events retrieves all events across the instance."""
    _, api_key = platform_client_and_key

    e1 = models.Event(name="Event 1", source="platform", external_id="ev-1")
    e2 = models.Event(name="Event 2", source="platform", external_id="ev-2")
    db_session.add_all([e1, e2])
    db_session.commit()

    response = client.get(
        "/events",
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 200
    event_names = [e["name"] for e in response.json()]
    assert "Event 1" in event_names
    assert "Event 2" in event_names


def test_talk_composite_external_id_scoping_per_event(db_session: Session):
    """Ensure composite ('event_id', 'external_id') talk resolution does not cross-contaminate scoped clients."""
    e1 = models.Event(name="Conference A", source="eventyay", external_id="conf-a")
    e2 = models.Event(name="Conference B", source="eventyay", external_id="conf-b")
    db_session.add_all([e1, e2])
    db_session.commit()

    now = datetime.now(UTC)
    t1 = models.Talk(
        title="Keynote A",
        event_id=e1.id,
        external_id="SHARED-TALK",
        status="waiting_for_files",
        start=now,
        end=now + timedelta(hours=1),
    )
    t2 = models.Talk(
        title="Keynote B",
        event_id=e2.id,
        external_id="SHARED-TALK",
        status="waiting_for_files",
        start=now,
        end=now + timedelta(hours=1),
    )
    db_session.add_all([t1, t2])

    raw_key_b = "test-scoped-key-b-12345"
    c_b = models.Client(
        name="Client B",
        hashed_key=hash_api_key(raw_key_b),
        is_platform=False,
        event_ids=[e2.id],
    )
    db_session.add(c_b)
    db_session.commit()

    # Client B should resolve Talk 2 (not Talk 1) despite both sharing external_id "SHARED-TALK"
    resp = client.post(
        "/talks/SHARED-TALK/sso-token",
        headers={"X-API-Key": raw_key_b},
        json={
            "role": "speaker",
            "email": "speaker@test.com",
            "display_name": "Speaker",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope_id"] == t2.id
    assert f"/studio/talks/{t2.id}" in data["url"]

    # GET /talks/SHARED-TALK with Client B's API key resolves Talk 2
    get_resp = client.get(
        "/talks/SHARED-TALK",
        headers={"X-API-Key": raw_key_b},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == t2.id
    assert get_resp.json()["title"] == "Keynote B"


def test_create_event_api_key_returns_created_at(db_session: Session):
    """Ensure ApiKeyCreatedResponse returns non-null created_at timestamp upon key generation."""
    organizer = models.User(
        email="org_apikey@test.com",
        hashed_password="hash",
        role="organizer",
        is_active=True,
    )
    db_session.add(organizer)
    db_session.commit()

    event = models.Event(
        name="API Key Event",
        source="manual",
        external_id="key-ev-1",
        created_by_user_id=organizer.id,
    )
    db_session.add(event)
    db_session.commit()

    session_token = create_session_token(organizer.id, organizer.role)
    resp = client.post(
        f"/events/{event.id}/api-keys",
        cookies={"veditor_session": session_token},
        json={"name": "Integration Key"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["id"] is not None
    assert data["api_key"] is not None
    assert data["created_at"] is not None
