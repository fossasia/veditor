from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import models, schemas
from app.auth import CurrentUser, get_client, get_current_user
from app.db import get_db
from app.main import app
from app.review_handlers import handle_approve, handle_needs_work, handle_reject
from app.storage import get_storage_backend

client = TestClient(app)


@pytest.fixture(autouse=True)
def cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_db():
    db = MagicMock()

    def fake_flush():
        for call in db.add.call_args_list:
            obj = call[0][0]
            if getattr(obj, "id", None) is None:
                obj.id = 1
            if getattr(obj, "created_at", None) is None:
                obj.created_at = datetime.now(UTC)

    db.flush.side_effect = fake_flush

    def fake_refresh(obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(UTC)

    db.refresh.side_effect = fake_refresh
    mock_filter = db.query.return_value.filter.return_value
    mock_filter.with_for_update.return_value = mock_filter
    return db


# ---------------------------------------------------------------------------
# Schema Tests
# ---------------------------------------------------------------------------


def test_event_read_schema_includes_created_by_user_id():
    ev = schemas.EventRead(id=1, name="Test Event", created_by_user_id=42)
    assert ev.id == 1
    assert ev.name == "Test Event"
    assert ev.created_by_user_id == 42

    ev_none = schemas.EventRead(id=2, name="Machine Event")
    assert ev_none.created_by_user_id is None


def test_review_read_schema_includes_user_id():
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


def test_review_handlers_forward_user_id(mock_db):
    talk = models.Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    payload = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.approve, note="LGTM"
    )

    resp = handle_approve(talk, payload, mock_db, user_id=99)
    assert resp.talk.status == "pending_intro_outro"
    assert mock_db.add.called
    added_review = mock_db.add.call_args[0][0]
    assert added_review.user_id == 99
    assert added_review.decision == "approve"

    # Needs work
    mock_db.reset_mock()
    talk.status = "preview"
    payload_nw = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.needs_work, note="Fix bounds"
    )
    resp_nw = handle_needs_work(talk, payload_nw, mock_db, user_id=88)
    assert resp_nw.talk.status == "pending_bounds"
    added_review_nw = mock_db.add.call_args[0][0]
    assert added_review_nw.user_id == 88

    # Reject
    mock_db.reset_mock()
    talk.status = "preview"
    payload_rej = schemas.ReviewRequest(
        decision=schemas.ReviewDecision.reject, note="Rejected"
    )
    resp_rej = handle_reject(talk, payload_rej, mock_db, user_id=77)
    assert resp_rej.talk.status == "rejected"
    added_review_rej = mock_db.add.call_args[0][0]
    assert added_review_rej.user_id == 77


# ---------------------------------------------------------------------------
# POST /events Tests
# ---------------------------------------------------------------------------


def test_post_events_unauthorized():
    res = client.post("/events", json={"name": "Conf 2026"})
    assert res.status_code == 401


def test_post_events_forbidden_for_regular_user():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, email="user@example.com", role="user", source="jwt"
    )
    res = client.post("/events", json={"name": "Conf 2026"})
    assert res.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res.json()["detail"]


def test_post_events_success_organizer(mock_db):
    def fake_refresh(obj):
        obj.id = 100

    mock_db.refresh.side_effect = fake_refresh
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, email="organizer@example.com", role="organizer", source="jwt"
    )

    res = client.post("/events", json={"name": "PyCon 2026"})
    assert res.status_code == 201
    data = res.json()
    assert data["id"] == 100
    assert data["name"] == "PyCon 2026"
    assert data["created_by_user_id"] == 5

    assert mock_db.add.called
    created_event = mock_db.add.call_args[0][0]
    assert created_event.created_by_user_id == 5
    assert created_event.name == "PyCon 2026"


def test_post_events_machine_client_sets_created_by_none(mock_db):
    def fake_refresh(obj):
        obj.id = 101

    mock_db.refresh.side_effect = fake_refresh
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[1, 2]
    )

    res = client.post("/events", json={"name": "Machine Event"})
    assert res.status_code == 201
    created_event = mock_db.add.call_args[0][0]
    assert created_event.created_by_user_id is None


# ---------------------------------------------------------------------------
# GET /events Tests
# ---------------------------------------------------------------------------


def test_get_events_unauthorized():
    res = client.get("/events")
    assert res.status_code == 401


def test_get_events_forbidden_for_user():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, email="user@example.com", role="user", source="jwt"
    )
    res = client.get("/events")
    assert res.status_code == 403


def test_get_events_admin_returns_all(mock_db):
    ev1 = models.Event(id=1, name="Event 1", created_by_user_id=1)
    ev2 = models.Event(id=2, name="Event 2", created_by_user_id=2)
    mock_db.query.return_value.all.return_value = [ev1, ev2]

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=99, email="admin@example.com", role="admin", source="jwt"
    )

    res = client.get("/events")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    assert data[0]["id"] == 1
    assert data[1]["id"] == 2


def test_get_events_organizer_filters_by_user_id(mock_db):
    ev1 = models.Event(id=1, name="Event 1", created_by_user_id=5)
    mock_db.query.return_value.filter.return_value.all.return_value = [ev1]

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, email="org@example.com", role="organizer", source="jwt"
    )

    res = client.get("/events")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["id"] == 1
    assert mock_db.query.return_value.filter.called


# ---------------------------------------------------------------------------
# POST /talks/{id}/review with user recording
# ---------------------------------------------------------------------------


def test_review_records_human_user_id(mock_db):
    mock_storage = MagicMock()
    talk = models.Talk(
        id=5,
        event_id=1,
        title="Keynote",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    event = models.Event(id=1, name="Keynote Event", created_by_user_id=42)

    def mock_query(model):
        m = MagicMock()
        if model == models.Event:
            m.filter.return_value.first.return_value = event
        else:
            m.filter.return_value.with_for_update.return_value.first.return_value = talk
        return m

    mock_db.query.side_effect = mock_query

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=42, email="reviewer@example.com", role="organizer", source="jwt"
    )

    res = client.post("/talks/5/review", json={"decision": "approve", "note": "Great!"})
    assert res.status_code == 200
    data = res.json()
    assert data["review"]["user_id"] == 42


def test_review_records_none_for_machine_client(mock_db):
    mock_storage = MagicMock()
    talk = models.Talk(
        id=5,
        event_id=1,
        title="Keynote",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = talk

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_storage_backend] = lambda: mock_storage
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[1]
    )

    res = client.post("/talks/5/review", json={"decision": "approve"})
    assert res.status_code == 200
    data = res.json()
    assert data["review"]["user_id"] is None


def test_review_machine_client_forbidden_event(mock_db):
    talk = models.Talk(
        id=5,
        event_id=2,  # Machine only has access to event 1
        title="Keynote",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = talk

    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=None, role="admin", source="api_key", event_ids=[1]
    )

    res = client.post("/talks/5/review", json={"decision": "approve"})
    assert res.status_code == 403
    assert res.json()["detail"] == "Client is not authorized to access this event"


# ---------------------------------------------------------------------------
# get_client override compatibility in get_current_user
# ---------------------------------------------------------------------------


def test_legacy_get_client_override_in_get_current_user(mock_db):
    mock_client = models.Client(id=1, event_ids=[10, 20])
    app.dependency_overrides[get_client] = lambda: mock_client
    app.dependency_overrides[get_db] = lambda: mock_db

    talk = models.Talk(
        id=1,
        event_id=10,
        title="Legacy Test",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="preview",
    )
    mock_db.query.return_value.filter.return_value.with_for_update.return_value.first.return_value = talk

    res = client.post("/talks/1/review", json={"decision": "approve"})
    assert res.status_code == 200
    assert res.json()["review"]["user_id"] is None


# ---------------------------------------------------------------------------
# Write Endpoints Gating Tests (POST /talks, /recordings, /approve)
# ---------------------------------------------------------------------------


def test_post_talks_forbidden_for_user_role():
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


def test_post_talks_forbidden_for_non_owning_organizer(mock_db):
    event = models.Event(id=1, name="Other Event", created_by_user_id=99)
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Talk In Other Event",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_post_talks_success_for_owning_organizer(mock_db):
    event = models.Event(id=1, name="My Event", created_by_user_id=5)
    # 1st query: Event check; 2nd query: Talk lookup (None => create)
    mock_db.query.return_value.filter.return_value.first.side_effect = [event, None]
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.post(
        "/talks",
        json={
            "event_id": 1,
            "title": "Talk In My Event",
            "room": "Hall A",
            "start": datetime.now(UTC).isoformat(),
            "end": datetime.now(UTC).isoformat(),
        },
    )
    assert res.status_code == 201
    assert res.json()["event_id"] == 1


def test_write_endpoints_forbidden_for_user_role():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="user", source="jwt"
    )
    res_rec = client.post("/talks/1/recordings", json={"source_path": "/tmp/rec.mp4"})
    assert res_rec.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res_rec.json()["detail"]

    res_app = client.post("/talks/1/approve")
    assert res_app.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res_app.json()["detail"]


def test_write_endpoints_forbidden_for_non_owning_organizer(mock_db):
    event = models.Event(id=1, name="Event 1", created_by_user_id=99)
    talk = models.Talk(
        id=1,
        event_id=1,
        title="Test Talk",
        room="Room 1",
        start=datetime.now(UTC),
        end=datetime.now(UTC),
        status="waiting_for_files",
    )
    mock_db.query.return_value.filter.return_value.first.side_effect = [talk, event]
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.post("/talks/1/recordings", json={"source_path": "/tmp/rec.mp4"})
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Event Update & Delete Tests
# ---------------------------------------------------------------------------


def test_update_event_forbidden_for_user_role():
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="user", source="jwt"
    )
    res = client.patch("/events/1", json={"name": "New Name"})
    assert res.status_code == 403
    assert "Operation requires minimum role 'organizer'" in res.json()["detail"]


def test_update_event_forbidden_for_non_owning_organizer(mock_db):
    event = models.Event(id=1, name="Old Name", created_by_user_id=10)
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.patch("/events/1", json={"name": "New Name"})
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_update_event_empty_name(mock_db):
    event = models.Event(id=1, name="Old Name", created_by_user_id=5)
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.patch("/events/1", json={"name": "   "})
    assert res.status_code == 400
    assert "Event name cannot be empty" in res.json()["detail"]


def test_update_event_success_for_owner(mock_db):
    event = models.Event(id=1, name="Old Name", created_by_user_id=5)
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.patch("/events/1", json={"name": "Updated Summit 2026"})
    assert res.status_code == 200
    assert res.json()["name"] == "Updated Summit 2026"
    assert event.name == "Updated Summit 2026"


def test_update_event_success_for_admin(mock_db):
    event = models.Event(id=1, name="Old Name", created_by_user_id=10)
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="admin", source="jwt"
    )

    res = client.patch("/events/1", json={"name": "Admin Renamed"})
    assert res.status_code == 200
    assert res.json()["name"] == "Admin Renamed"
    assert event.name == "Admin Renamed"


def test_delete_event_forbidden_for_non_owning_organizer(mock_db):
    event = models.Event(id=1, name="Other Event", created_by_user_id=10, talks=[])
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    res = client.delete("/events/1")
    assert res.status_code == 403
    assert "User is not authorized to access this event" in res.json()["detail"]


def test_delete_event_success_for_owner(mock_db):
    talk = models.Talk(id=42, event_id=1, title="Sample Talk")
    event = models.Event(id=1, name="My Event", created_by_user_id=5, talks=[talk])
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=5, role="organizer", source="jwt"
    )

    fake_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    res = client.delete("/events/1")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": 1}
    assert mock_db.query.return_value.filter.return_value.with_for_update.called
    fake_storage.delete.assert_called_with("42")
    mock_db.delete.assert_called_with(event)


def test_delete_event_success_for_admin(mock_db):
    event = models.Event(id=1, name="Any Event", created_by_user_id=99, talks=[])
    mock_db.query.return_value.filter.return_value.first.return_value = event
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=1, role="admin", source="jwt"
    )

    fake_storage = MagicMock()
    app.dependency_overrides[get_storage_backend] = lambda: fake_storage

    res = client.delete("/events/1")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "deleted_id": 1}
    mock_db.delete.assert_called_with(event)
