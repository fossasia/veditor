import socket
from datetime import UTC, datetime
from email.message import Message
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, schemas
from app.auth import CurrentUser, get_client, get_current_user
from app.consts import AuthRole, TalkStatus
from app.main import app
from app.review_handlers import handle_approve, handle_needs_work, handle_reject
from app.storage import get_storage_backend

client = TestClient(app)


def _create_user(
    db: Session, *, email: str | None = None, role: AuthRole = AuthRole.ORGANIZER
) -> models.User:
    user = models.User(
        email=email or f"user-{uuid4().hex}@example.com",
        hashed_password="x",
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _create_event(
    db: Session,
    *,
    name: str = "Test Event",
    created_by_user_id: int | None = None,
    **kwargs: Any,
) -> models.Event:
    event = models.Event(name=name, created_by_user_id=created_by_user_id, **kwargs)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _create_talk(
    db: Session,
    event_id: int,
    *,
    title: str | None = None,
    status: str = "preview",
    **kwargs: Any,
) -> models.Talk:
    talk = models.Talk(
        event_id=event_id,
        title=title or f"Talk {uuid4().hex[:8]}",
        start=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        end=datetime(2026, 9, 1, 10, 30, tzinfo=UTC),
        status=status,
        cut_start=10.0,
        cut_end=60.0,
        **kwargs,
    )
    db.add(talk)
    db.commit()
    db.refresh(talk)
    return talk


def _create_client(
    db: Session, event_ids: list[int], *, name: str = "Test Client", **kwargs: Any
) -> models.Client:
    client_row = models.Client(
        name=name, hashed_key=uuid4().hex, event_ids=list(event_ids), **kwargs
    )
    db.add(client_row)
    db.commit()
    db.refresh(client_row)
    return client_row


@pytest.fixture
def owned_event(db_session: Session) -> models.Event:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )
    return event


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------


def test_event_read_schema_includes_created_by_user_id() -> None:
    ev = schemas.EventRead(id=1, name="Test Event", created_by_user_id=42)
    assert ev.id == 1
    assert ev.name == "Test Event"
    assert ev.created_by_user_id == 42

    ev_none = schemas.EventRead(id=2, name="Machine Event")
    assert ev_none.created_by_user_id is None


def test_review_read_schema_includes_user_id() -> None:
    rev = schemas.ReviewRead(
        id=1,
        talk_id=10,
        decision="approve",
        created_at=datetime.now(UTC),
        user_id=7,
    )
    assert rev.id == 1
    assert rev.talk_id == 10
    assert rev.decision == "approve"
    assert rev.user_id == 7

    rev_none = schemas.ReviewRead(
        id=2,
        talk_id=10,
        decision="reject",
        created_at=datetime.now(UTC),
    )
    assert rev_none.user_id is None


# ---------------------------------------------------------------------------
# Review Handlers Unit Tests
# ---------------------------------------------------------------------------


def test_review_handlers_forward_user_id(db_session: Session) -> None:
    approve_user = _create_user(db_session, email="approve@example.com")
    work_user = _create_user(db_session, email="work@example.com")
    reject_user = _create_user(db_session, email="reject@example.com")
    event = _create_event(db_session, created_by_user_id=approve_user.id)
    talk = _create_talk(db_session, event.id)

    payload = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.approve, note="LGTM"
    )
    with patch("app.tasks.dispatch_assembly"):
        resp = handle_approve(talk, payload, db_session, user_id=approve_user.id)
    assert resp.talk.status == TalkStatus.ASSEMBLING
    approve_review = db_session.scalar(
        select(models.Review).where(
            models.Review.talk_id == talk.id,
            models.Review.decision == "approve",
        )
    )
    assert approve_review is not None
    assert approve_review.user_id == approve_user.id

    talk.status = TalkStatus.PREVIEW
    db_session.commit()
    payload_nw = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.needs_work, note="Fix bounds"
    )
    resp_nw = handle_needs_work(talk, payload_nw, db_session, user_id=work_user.id)
    assert resp_nw.talk.status == TalkStatus.PENDING_BOUNDS
    work_review = db_session.scalar(
        select(models.Review).where(
            models.Review.talk_id == talk.id,
            models.Review.decision == "needs_work",
        )
    )
    assert work_review is not None
    assert work_review.user_id == work_user.id

    talk.status = TalkStatus.PREVIEW
    talk.cut_start = 10.0
    talk.cut_end = 60.0
    db_session.commit()
    payload_rej = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.reject, note="Rejected"
    )
    resp_rej = handle_reject(talk, payload_rej, db_session, user_id=reject_user.id)
    assert resp_rej.talk.status == TalkStatus.PENDING_BOUNDS
    reject_review = db_session.scalar(
        select(models.Review).where(
            models.Review.talk_id == talk.id,
            models.Review.decision == "reject",
        )
    )
    assert reject_review is not None
    assert reject_review.user_id == reject_user.id


# ---------------------------------------------------------------------------
# POST /events Tests
# ---------------------------------------------------------------------------


def test_post_events_unauthorized() -> None:
    res = client.post("/events", json={"name": "Conf 2026"})
    assert res.status_code == 401


def test_post_events_forbidden_for_regular_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, email="user@example.com", role="user", source="jwt"
    )
    res = client.post("/events", json={"name": "Conf 2026"})
    assert res.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res.json()["detail"]


def test_post_events_success_organizer(db_session: Session) -> None:
    user = _create_user(db_session, email="organizer@example.com")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=user.id, email=user.email, role="organizer", source="jwt"
    )

    res = client.post("/events", json={"name": "PyCon 2026"})
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "PyCon 2026"
    assert data["created_by_user_id"] == user.id

    db_session.expire_all()
    created_event = db_session.get(models.Event, data["id"])
    assert created_event is not None
    assert created_event.created_by_user_id == user.id
    assert created_event.name == "PyCon 2026"


def test_post_events_machine_client_sets_created_by_none(db_session: Session) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[]
    )

    res = client.post("/events", json={"name": "Machine Event"})
    assert res.status_code == 201
    db_session.expire_all()
    created_event = db_session.get(models.Event, res.json()["id"])
    assert created_event is not None
    assert created_event.created_by_user_id is None


# ---------------------------------------------------------------------------
# GET /events Tests
# ---------------------------------------------------------------------------


def test_get_events_unauthorized() -> None:
    res = client.get("/events")
    assert res.status_code == 401


def test_get_events_forbidden_for_user() -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, email="user@example.com", role="user", source="jwt"
    )
    res = client.get("/events")
    assert res.status_code == 403


def test_get_events_admin_returns_only_own(db_session: Session) -> None:
    owner = _create_user(db_session, email="admin@example.com", role=AuthRole.ADMIN)
    other_user = _create_user(db_session, email="other@example.com")
    own_event = _create_event(db_session, name="Event 2", created_by_user_id=owner.id)
    _create_event(db_session, name="Other Event", created_by_user_id=other_user.id)

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, email=owner.email, role="admin", source="jwt"
    )

    res = client.get("/events")
    assert res.status_code == 200
    data = res.json()
    assert [event["id"] for event in data] == [own_event.id]


def test_get_events_organizer_filters_by_user_id(db_session: Session) -> None:
    owner = _create_user(db_session, email="org@example.com")
    other_user = _create_user(db_session, email="other-org@example.com")
    own_event = _create_event(db_session, name="Event 1", created_by_user_id=owner.id)
    _create_event(db_session, name="Other Event", created_by_user_id=other_user.id)

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, email=owner.email, role="organizer", source="jwt"
    )

    res = client.get("/events")
    assert res.status_code == 200
    data = res.json()
    assert [event["id"] for event in data] == [own_event.id]


# ---------------------------------------------------------------------------
# POST /talks/{id}/review with user recording
# ---------------------------------------------------------------------------


def test_review_records_human_user_id(db_session: Session) -> None:
    reviewer = _create_user(db_session, email="reviewer@example.com")
    event = _create_event(
        db_session, name="Keynote Event", created_by_user_id=reviewer.id
    )
    talk = _create_talk(db_session, event.id, title="Keynote")
    mock_storage = MagicMock()

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=reviewer.id, email=reviewer.email, role="organizer", source="jwt"
    )

    with patch("app.tasks.dispatch_assembly"):
        res = client.post(
            f"/talks/{talk.id}/review",
            json={"decision": "approve", "note": "Great!"},
        )
    assert res.status_code == 200
    data = res.json()
    assert data["review"]["user_id"] == reviewer.id


def test_review_records_none_for_machine_client(db_session: Session) -> None:
    event = _create_event(db_session, name="Keynote Event")
    talk = _create_talk(db_session, event.id, title="Keynote")
    mock_storage = MagicMock()

    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[event.id]
    )

    with patch("app.tasks.dispatch_assembly"):
        res = client.post(f"/talks/{talk.id}/review", json={"decision": "approve"})
    assert res.status_code == 200
    data = res.json()
    assert data["review"]["user_id"] is None


def test_review_machine_client_forbidden_event(db_session: Session) -> None:
    event = _create_event(db_session, name="Keynote Event")
    talk = _create_talk(db_session, event.id, title="Keynote")

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[event.id + 1000]
    )

    res = client.post(f"/talks/{talk.id}/review", json={"decision": "approve"})
    assert res.status_code == 403
    assert res.json()["detail"] == "Client is not authorized to access this event"


# ---------------------------------------------------------------------------
# get_client override compatibility in get_current_user
# ---------------------------------------------------------------------------


def test_legacy_get_client_override_in_get_current_user(db_session: Session) -> None:
    event = _create_event(db_session, name="Legacy Event")
    talk = _create_talk(db_session, event.id, title="Legacy Test")
    mock_client = models.Client(id=1, event_ids=[event.id])
    app.dependency_overrides[get_client] = lambda: mock_client

    with patch("app.tasks.dispatch_assembly"):
        res = client.post(f"/talks/{talk.id}/review", json={"decision": "approve"})
    assert res.status_code == 200
    assert res.json()["review"]["user_id"] is None


# ---------------------------------------------------------------------------
# Write Endpoints Gating Tests (POST /talks, /recordings, /approve)
# ---------------------------------------------------------------------------


def test_post_talks_forbidden_for_user_role() -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="user", source="jwt"
    )
    res = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Unauthorized Talk",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res.json()["detail"]


def test_post_talks_forbidden_for_non_owning_organizer(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    actor = _create_user(db_session, email="actor@example.com")
    event = _create_event(db_session, name="Other Event", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=actor.id, role="organizer", source="jwt"
    )

    res = client.post(
        "/talks",
        json={
            "event_id": event.id,
            "title": "Talk In Other Event",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_post_talks_success_for_owning_organizer(db_session: Session) -> None:
    organizer = _create_user(db_session, email="organizer@example.com")
    event = _create_event(db_session, name="My Event", created_by_user_id=organizer.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=organizer.id, role="organizer", source="jwt"
    )

    res = client.post(
        "/talks",
        json={
            "event_id": event.id,
            "title": "Talk In My Event",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 201
    assert res.json()["event_id"] == event.id

    db_session.expire_all()
    created_talk = db_session.scalar(
        select(models.Talk).where(models.Talk.title == "Talk In My Event")
    )
    assert created_talk is not None
    assert created_talk.event_id == event.id


def test_write_endpoints_forbidden_for_user_role() -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="user", source="jwt"
    )
    res_rec = client.post("/talks/1/recordings", json={"source_path": "/tmp/rec.mp4"})
    assert res_rec.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res_rec.json()["detail"]

    res_app = client.post("/talks/1/approve")
    assert res_app.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res_app.json()["detail"]


def test_write_endpoints_forbidden_for_non_owning_organizer(
    db_session: Session,
) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    actor = _create_user(db_session, email="actor@example.com")
    event = _create_event(db_session, name="Event 1", created_by_user_id=owner.id)
    talk = _create_talk(
        db_session,
        event.id,
        title="Test Talk",
        status="waiting_for_files",
        room="Room 1",
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=actor.id, role="organizer", source="jwt"
    )

    res = client.post(
        f"/talks/{talk.id}/recordings", json={"source_path": "/tmp/rec.mp4"}
    )
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Event Update & Delete Tests
# ---------------------------------------------------------------------------


def test_update_event_forbidden_for_user_role() -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="user", source="jwt"
    )
    res = client.patch("/events/1", json={"name": "New Name"})
    assert res.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res.json()["detail"]


def test_update_event_forbidden_for_non_owning_organizer(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    actor = _create_user(db_session, email="actor@example.com")
    event = _create_event(db_session, name="Old Name", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=actor.id, role="organizer", source="jwt"
    )

    res = client.patch(f"/events/{event.id}", json={"name": "New Name"})
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_update_event_empty_name(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Old Name", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.patch(f"/events/{event.id}", json={"name": "   "})
    assert res.status_code == 400
    assert "Event name cannot be empty" in res.json()["detail"]


def test_update_event_success_for_owner(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Old Name", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.patch(f"/events/{event.id}", json={"name": "Updated Summit 2026"})
    assert res.status_code == 200
    assert res.json()["name"] == "Updated Summit 2026"

    db_session.expire_all()
    reloaded_event = db_session.get(models.Event, event.id)
    assert reloaded_event is not None
    assert reloaded_event.name == "Updated Summit 2026"


def test_update_event_success_for_admin(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    admin_user = _create_user(
        db_session, email="admin@example.com", role=AuthRole.ADMIN
    )
    event = _create_event(db_session, name="Old Name", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=admin_user.id, role="admin", source="jwt"
    )

    res = client.patch(f"/events/{event.id}", json={"name": "Admin Renamed"})
    assert res.status_code == 200
    assert res.json()["name"] == "Admin Renamed"

    db_session.expire_all()
    reloaded_event = db_session.get(models.Event, event.id)
    assert reloaded_event is not None
    assert reloaded_event.name == "Admin Renamed"


def test_delete_event_forbidden_for_non_owning_organizer(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    actor = _create_user(db_session, email="actor@example.com")
    event = _create_event(db_session, name="Other Event", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=actor.id, role="organizer", source="jwt"
    )

    res = client.delete(f"/events/{event.id}")
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_delete_event_success_for_owner(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="My Event", created_by_user_id=owner.id)
    talk = _create_talk(db_session, event.id, title="Sample Talk")
    fake_storage = MagicMock()

    app.dependency_overrides[get_storage_backend] = lambda: fake_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    with patch("app.routes.events._cancel_talk_jobs") as mock_cancel:
        res = client.delete(f"/events/{event.id}")

    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": event.id}
    mock_cancel.assert_called_once_with(talk.id, fake_storage)

    db_session.expire_all()
    assert db_session.get(models.Event, event.id) is None
    assert db_session.get(models.Talk, talk.id) is None


def test_delete_event_success_for_admin(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    admin_user = _create_user(
        db_session, email="admin@example.com", role=AuthRole.ADMIN
    )
    event = _create_event(db_session, name="Any Event", created_by_user_id=owner.id)
    fake_storage = MagicMock()

    app.dependency_overrides[get_storage_backend] = lambda: fake_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=admin_user.id, role="admin", source="jwt"
    )

    with patch("app.routes.events._cancel_talk_jobs"):
        res = client.delete(f"/events/{event.id}")

    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": event.id}

    db_session.expire_all()
    assert db_session.get(models.Event, event.id) is None


# ---------------------------------------------------------------------------
# API Key Management & SSO Tests
# ---------------------------------------------------------------------------


def test_list_event_api_keys(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    other_event = _create_event(
        db_session, name="Other Event", created_by_user_id=owner.id
    )
    c1 = _create_client(db_session, [event.id], name="Web Sync Key")
    _create_client(db_session, [other_event.id], name="Other Key")

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.get(f"/events/{event.id}/api-keys")
    assert res.status_code == 200
    keys = res.json()
    assert [key["id"] for key in keys] == [c1.id]
    assert keys[0]["name"] == "Web Sync Key"
    assert "created_at" in keys[0]
    assert "last_used_at" in keys[0]


def test_create_event_api_key(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.post(f"/events/{event.id}/api-keys", json={"name": "My New Key"})
    assert res.status_code == 201
    data = res.json()
    assert data["name"] == "My New Key"
    assert "api_key" in data

    db_session.expire_all()
    created = db_session.scalar(
        select(models.Client).where(models.Client.name == "My New Key")
    )
    assert created is not None
    assert created.event_ids == [event.id]


def test_api_key_management_forbidden_for_sso(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="organizer", source="sso", event_ids=[event.id]
    )

    res_get = client.get(f"/events/{event.id}/api-keys")
    assert res_get.status_code == 403
    assert (
        "SSO sessions are not permitted to manage API keys" in res_get.json()["detail"]
    )

    res_post = client.post(f"/events/{event.id}/api-keys", json={"name": "Blocked"})
    assert res_post.status_code == 403
    assert (
        "SSO sessions are not permitted to manage API keys" in res_post.json()["detail"]
    )


def test_revoke_event_api_key(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    c1 = _create_client(db_session, [event.id], name="Web Sync Key")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.delete(f"/events/{event.id}/api-keys/{c1.id}")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": c1.id}

    db_session.expire_all()
    assert db_session.get(models.Client, c1.id) is None


def test_create_event_api_key_revokes_existing_keys(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    existing_c = _create_client(db_session, [event.id], name="Old Key")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.post(f"/events/{event.id}/api-keys", json={"name": "Rotated Key"})
    assert res.status_code == 201

    db_session.expire_all()
    assert db_session.get(models.Client, existing_c.id) is None
    new_client = db_session.scalar(
        select(models.Client).where(models.Client.name == "Rotated Key")
    )
    assert new_client is not None
    assert new_client.event_ids == [event.id]


def test_revoke_multi_event_api_key_retains_other_events(db_session: Session) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    other_event = _create_event(
        db_session, name="Other Event", created_by_user_id=owner.id
    )
    c1 = _create_client(db_session, [event.id, other_event.id], name="Shared Key")
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.delete(f"/events/{event.id}/api-keys/{c1.id}")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": c1.id}

    db_session.expire_all()
    reloaded = db_session.get(models.Client, c1.id)
    assert reloaded is not None
    assert reloaded.event_ids == [other_event.id]


def test_create_event_api_key_multi_event_retains_other_events(
    db_session: Session,
) -> None:
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(db_session, name="Test Event", created_by_user_id=owner.id)
    other_event = _create_event(
        db_session, name="Other Event", created_by_user_id=owner.id
    )
    existing_c = _create_client(
        db_session, [event.id, other_event.id], name="Shared Key"
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    res = client.post(f"/events/{event.id}/api-keys", json={"name": "Rotated Key"})
    assert res.status_code == 201

    db_session.expire_all()
    reloaded = db_session.get(models.Client, existing_c.id)
    assert reloaded is not None
    assert reloaded.event_ids == [other_event.id]


# ── Outbound Webhook Tests ───────────────────────────────────────
def test_get_event_webhook_none_configured(owned_event: models.Event) -> None:
    res = client.get(f"/events/{owned_event.id}/webhook")
    assert res.status_code == 200
    data = res.json()
    assert data["url"] is None
    assert data["has_secret"] is False
    assert data["masked_secret"] is None


def test_get_event_webhook_configured(
    db_session: Session, owned_event: models.Event
) -> None:
    _create_client(
        db_session,
        [owned_event.id],
        name="Event Client",
        webhook_url="https://subscriber.example.com/webhook",
        webhook_secret="super-secret-key-12345",
    )

    res = client.get(f"/events/{owned_event.id}/webhook")
    assert res.status_code == 200
    data = res.json()
    assert data["url"] == "https://subscriber.example.com/webhook"
    assert data["has_secret"] is True
    assert data["masked_secret"] == "supe...2345"


def test_update_event_webhook_creates_client_and_persists(
    db_session: Session, owned_event: models.Event
) -> None:
    res = client.post(
        f"/events/{owned_event.id}/webhook",
        json={
            "url": "https://subscriber.example.com/hook",
            "secret": "my-custom-secret",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "registered"
    assert data["url"] == "https://subscriber.example.com/hook"
    assert data["secret"] == "my-custom-secret"

    db_session.expire_all()
    created = db_session.scalar(
        select(models.Client).where(
            models.Client.name == f"Event #{owned_event.id} Integration"
        )
    )
    assert created is not None
    assert created.webhook_url == "https://subscriber.example.com/hook"
    assert created.webhook_secret == "my-custom-secret"


def test_update_event_webhook_generates_secret_if_omitted(
    db_session: Session, owned_event: models.Event
) -> None:
    client_row = _create_client(
        db_session,
        [owned_event.id],
        name="Event Client",
        webhook_url=None,
        webhook_secret=None,
    )

    res = client.post(
        f"/events/{owned_event.id}/webhook",
        json={"url": "https://subscriber.example.com/hook"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "registered"
    assert data["url"] == "https://subscriber.example.com/hook"
    assert len(data["secret"]) >= 32

    db_session.expire_all()
    reloaded = db_session.get(models.Client, client_row.id)
    assert reloaded is not None
    assert reloaded.webhook_url == "https://subscriber.example.com/hook"
    assert reloaded.webhook_secret == data["secret"]


def test_update_event_webhook_forbidden_for_sso(owned_event: models.Event) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="organizer", source="sso", event_ids=[owned_event.id]
    )

    res = client.post(
        f"/events/{owned_event.id}/webhook",
        json={"url": "https://subscriber.example.com/hook"},
    )
    assert res.status_code == 403
    assert "SSO sessions are not permitted" in res.json()["detail"]


def test_delete_event_webhook(db_session: Session, owned_event: models.Event) -> None:
    client_row = _create_client(
        db_session,
        [owned_event.id],
        name="Event Client",
        webhook_url="https://subscriber.example.com/hook",
        webhook_secret="secret",
    )

    res = client.delete(f"/events/{owned_event.id}/webhook")
    assert res.status_code == 204

    db_session.expire_all()
    reloaded = db_session.get(models.Client, client_row.id)
    assert reloaded is not None
    assert reloaded.webhook_url is None
    assert reloaded.webhook_secret is None


def test_test_event_webhook_success_with_payload_args(
    owned_event: models.Event,
) -> None:
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch(
            "app.routes.events._webhook_opener.open", return_value=mock_resp
        ) as mock_open,
        patch(
            "app.routes.events.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
            ],
        ),
    ):
        res = client.post(
            f"/events/{owned_event.id}/webhook/test",
            json={
                "url": "https://subscriber.example.com/hook",
                "secret": "test-secret",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["status_code"] == 200
        assert "delivered successfully" in data["message"]
        assert mock_open.called
        req = mock_open.call_args[0][0]
        assert req.get_header("X-veditor-signature").startswith("sha256=")


def test_test_event_webhook_uses_stored_client_config(
    db_session: Session, owned_event: models.Event
) -> None:
    _create_client(
        db_session,
        [owned_event.id],
        name="Event Client",
        webhook_url="https://stored.example.com/hook",
        webhook_secret="stored-secret",
    )

    mock_resp = MagicMock()
    mock_resp.status = 204
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch(
            "app.routes.events._webhook_opener.open", return_value=mock_resp
        ) as mock_open,
        patch(
            "app.routes.events.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
            ],
        ),
    ):
        res = client.post(f"/events/{owned_event.id}/webhook/test", json={})
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["status_code"] == 204
        req = mock_open.call_args[0][0]
        assert req.full_url == "https://stored.example.com/hook"


def test_test_event_webhook_http_error(owned_event: models.Event) -> None:
    http_error = HTTPError(
        url="https://subscriber.example.com/hook",
        code=404,
        msg="Not Found",
        hdrs=Message(),
        fp=None,
    )

    with (
        patch("app.routes.events._webhook_opener.open", side_effect=http_error),
        patch(
            "app.routes.events.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
            ],
        ),
    ):
        res = client.post(
            f"/events/{owned_event.id}/webhook/test",
            json={
                "url": "https://subscriber.example.com/hook",
                "secret": "test-secret",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is False
        assert data["status_code"] == 404
        assert "404: Not Found" in data["message"]


def test_test_event_webhook_connection_error(owned_event: models.Event) -> None:
    url_error = URLError(reason="Connection refused")

    with (
        patch("app.routes.events._webhook_opener.open", side_effect=url_error),
        patch(
            "app.routes.events.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))
            ],
        ),
    ):
        res = client.post(
            f"/events/{owned_event.id}/webhook/test",
            json={
                "url": "https://subscriber.example.com/hook",
                "secret": "test-secret",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is False
        assert data["status_code"] is None
        assert "Connection failed: Connection refused" in data["message"]


def test_test_event_webhook_missing_url_raises_400(owned_event: models.Event) -> None:
    res = client.post(
        f"/events/{owned_event.id}/webhook/test",
        json={"secret": "secret-without-url"},
    )
    assert res.status_code == 400
    assert "No webhook URL provided or configured" in res.json()["detail"]


def test_test_event_webhook_sso_rejected(owned_event: models.Event) -> None:
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        role="organizer", source="sso", event_ids=[owned_event.id]
    )

    res = client.post(
        f"/events/{owned_event.id}/webhook/test",
        json={"url": "https://example.com/webhook", "secret": "sec"},
    )
    assert res.status_code == 403
    assert "SSO sessions are not permitted" in res.json()["detail"]


def test_test_event_webhook_different_url_does_not_reuse_stored_secret(
    db_session: Session, owned_event: models.Event
) -> None:
    _create_client(
        db_session,
        [owned_event.id],
        webhook_url="https://configured.example.com/webhook",
        webhook_secret="super-secret-12345",
    )

    # Calling with a different URL without supplying secret must fail with 400
    res = client.post(
        f"/events/{owned_event.id}/webhook/test",
        json={"url": "https://attacker.example.com/steal-secret"},
    )
    assert res.status_code == 400
    assert "No webhook secret provided or configured" in res.json()["detail"]


def test_api_key_create_webhook_secret_length_limit() -> None:
    from pydantic import ValidationError

    from app.schemas import ApiKeyCreate

    valid = ApiKeyCreate(webhook_secret="a" * 255)
    assert valid.webhook_secret == "a" * 255

    import pytest

    with pytest.raises(ValidationError):
        ApiKeyCreate(webhook_secret="a" * 256)


def test_test_event_webhook_ssrf_rejects_loopback_and_private(
    owned_event: models.Event,
) -> None:
    for blocked_url in [
        "http://127.0.0.1:8000/webhook",
        "http://localhost:5000/webhook",
        "http://10.0.0.5/webhook",
        "http://192.168.1.1/webhook",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]:8000/webhook",
        "http://224.0.0.1/webhook",
        "http://240.0.0.1/webhook",
        "http://[ff02::1]/webhook",
    ]:
        res = client.post(
            f"/events/{owned_event.id}/webhook/test",
            json={"url": blocked_url, "secret": "test-secret-1234"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is False
        assert "SSRF validation failed" in data["message"]


def test_update_event_webhook_masks_unchanged_secret(
    db_session: Session, owned_event: models.Event
) -> None:
    _create_client(
        db_session,
        [owned_event.id],
        webhook_url="https://example.com/old-hook",
        webhook_secret="super-secret-token-1234",
    )

    # Updating only the URL without providing secret must mask the secret in response
    res = client.post(
        f"/events/{owned_event.id}/webhook",
        json={"url": "https://example.com/new-hook"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "registered"
    assert data["url"] == "https://example.com/new-hook"
    assert data["secret"] == "supe...1234"
    assert data["secret"] != "super-secret-token-1234"


def test_update_event_webhook_decouples_multi_event_client(
    db_session: Session, owned_event: models.Event
) -> None:
    other_event = _create_event(
        db_session,
        name="Other Event",
        created_by_user_id=owned_event.created_by_user_id,
    )
    shared_client = _create_client(
        db_session,
        [owned_event.id, other_event.id],
        name="Shared Client",
        webhook_url="https://shared.example.com/hook",
        webhook_secret="shared-secret-1234",
    )

    res = client.post(
        f"/events/{owned_event.id}/webhook",
        json={
            "url": "https://event1.example.com/hook",
            "secret": "event1-secret-9999",
        },
    )
    assert res.status_code == 200

    db_session.expire_all()
    reloaded_shared = db_session.get(models.Client, shared_client.id)
    assert reloaded_shared is not None
    assert reloaded_shared.event_ids == [other_event.id]

    all_clients = db_session.scalars(select(models.Client)).all()
    decoupled = next(
        c
        for c in all_clients
        if c.id != shared_client.id and owned_event.id in (c.event_ids or [])
    )
    assert decoupled.webhook_url == "https://event1.example.com/hook"
    assert decoupled.webhook_secret == "event1-secret-9999"


def test_delete_event_webhook_decouples_multi_event_client(
    db_session: Session, owned_event: models.Event
) -> None:
    other_event = _create_event(
        db_session,
        name="Other Event",
        created_by_user_id=owned_event.created_by_user_id,
    )
    shared_client = _create_client(
        db_session,
        [owned_event.id, other_event.id],
        name="Shared Client",
        webhook_url="https://shared.example.com/hook",
        webhook_secret="shared-secret-2",
    )

    res = client.delete(f"/events/{owned_event.id}/webhook")
    assert res.status_code == 204

    db_session.expire_all()
    reloaded = db_session.get(models.Client, shared_client.id)
    assert reloaded is not None
    assert reloaded.event_ids == [other_event.id]
    assert reloaded.webhook_url == "https://shared.example.com/hook"


def test_event_create_and_patch_speaker_magic_link_expire_hours(
    db_session: Session,
) -> None:
    """Event create and patch support speaker_magic_link_expire_hours."""
    owner = _create_user(db_session, email="owner@example.com")
    event = _create_event(
        db_session,
        name="Test Expiry Event",
        created_by_user_id=owner.id,
        speaker_magic_link_expire_hours=72,
    )
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=owner.id, role="organizer", source="jwt"
    )

    response = client.patch(
        f"/events/{event.id}",
        json={"speaker_magic_link_expire_hours": 336},
    )
    assert response.status_code == 200

    db_session.expire_all()
    reloaded_event = db_session.get(models.Event, event.id)
    assert reloaded_event is not None
    assert reloaded_event.speaker_magic_link_expire_hours == 336

    # Boolean values must be rejected (StrictPositiveInt)
    res_bool_patch = client.patch(
        f"/events/{event.id}",
        json={"speaker_magic_link_expire_hours": True},
    )
    assert res_bool_patch.status_code == 422

    res_bool_post = client.post(
        "/events",
        json={"name": "Strict Int Event", "speaker_magic_link_expire_hours": True},
    )
    assert res_bool_post.status_code == 422
