from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.auth import CurrentUser, get_current_user
from app.config import settings
from app.db import Base, get_db
from app.main import app
from app.routes.events import delete_event_room, list_event_rooms, update_event_room
from app.routes.talks import _ensure_event_room

engine = create_engine(settings.database_url)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
client = TestClient(app)


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def _event(db, name):
    event = models.Event(name=name)
    db.add(event)
    db.flush()
    return event


def test_room_names_are_event_scoped_and_case_insensitive(db_session):
    first = _event(db_session, "Rooms event one")
    second = _event(db_session, "Rooms event two")
    db_session.add(models.Room(event_id=first.id, name="Hall A"))
    db_session.flush()

    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(models.Room(event_id=first.id, name="hall a"))
        db_session.flush()

    db_session.add(models.Room(event_id=second.id, name="hall a"))
    db_session.flush()


def test_room_rename_and_delete_keep_talk_assignments_consistent(db_session):
    event = _event(db_session, "Room assignment event")
    room = models.Room(event_id=event.id, name="Hall A")
    db_session.add(room)
    db_session.flush()
    talks = [
        models.Talk(
            event_id=event.id,
            title=f"Talk {index}",
            room="Hall A",
            start=datetime(2026, 1, 1, index, tzinfo=UTC),
            end=datetime(2026, 1, 1, index + 1, tzinfo=UTC),
        )
        for index in (1, 2)
    ]
    db_session.add_all(talks)
    db_session.flush()

    room.name = "Main Hall"
    db_session.query(models.Talk).filter(
        models.Talk.event_id == event.id, models.Talk.room == "Hall A"
    ).update({models.Talk.room: room.name}, synchronize_session=False)
    db_session.flush()
    db_session.expire_all()
    assert {
        talk.room
        for talk in db_session.query(models.Talk).filter(
            models.Talk.event_id == event.id
        )
    } == {"Main Hall"}

    db_session.query(models.Talk).filter(
        models.Talk.event_id == event.id, models.Talk.room == room.name
    ).update({models.Talk.room: None}, synchronize_session=False)
    db_session.delete(room)
    db_session.flush()
    db_session.expire_all()
    assert all(
        talk.room is None
        for talk in db_session.query(models.Talk)
        .filter(models.Talk.event_id == event.id)
        .all()
    )


def test_room_assignment_normalizes_blank_and_reuses_pending_room(db_session):
    event = _event(db_session, "Room normalization event")
    assert _ensure_event_room(db_session, event.id, "   ") is None
    assert _ensure_event_room(db_session, event.id, None) is None

    first = _ensure_event_room(db_session, event.id, "  Hall A  ")
    second = _ensure_event_room(db_session, event.id, "hall a")
    assert first == "Hall A"
    assert second == "Hall A"
    assert db_session.query(models.Room).filter_by(event_id=event.id).count() == 1


def test_authenticated_room_endpoints_preserve_event_isolation(db_session):
    owner_id = 7001
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7001@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "Endpoint room event")
    event.created_by_user_id = owner_id
    other_event = _event(db_session, "Other endpoint room event")
    room = models.Room(event_id=event.id, name="Hall A")
    db_session.add(room)
    other_talk = models.Talk(
        event_id=other_event.id,
        title="Other event talk",
        room="Hall A",
        start=datetime(2026, 2, 1, 10, tzinfo=UTC),
        end=datetime(2026, 2, 1, 11, tzinfo=UTC),
    )
    db_session.add(other_talk)
    db_session.flush()
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")

    updated = update_event_room(
        event.id,
        room.id,
        schemas.RoomUpdate(name="Main Hall"),
        user,
        db_session,
    )
    assert updated.name == "Main Hall"
    assert other_talk.room == "Hall A"

    listed = list_event_rooms(event.id, user, db_session)
    assert [item.name for item in listed] == ["Main Hall"]
    delete_event_room(event.id, room.id, user, db_session)
    db_session.expire_all()
    assert db_session.get(models.Talk, other_talk.id).room == "Hall A"


def test_room_listing_does_not_mutate_legacy_talk_room_values(db_session):
    owner_id = 7002
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7002@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "Legacy room event")
    event.created_by_user_id = owner_id
    talk = models.Talk(
        event_id=event.id,
        title="Legacy room talk",
        room="  Legacy Hall  ",
        start=datetime(2026, 3, 1, 10, tzinfo=UTC),
        end=datetime(2026, 3, 1, 11, tzinfo=UTC),
    )
    db_session.add(talk)
    db_session.flush()
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")

    listed = list_event_rooms(event.id, user, db_session)
    assert listed == []
    assert db_session.query(models.Room).filter_by(event_id=event.id).count() == 0
    assert db_session.get(models.Talk, talk.id).room == "  Legacy Hall  "


def test_room_http_endpoints_enforce_event_isolation(db_session):
    owner_id = 7003
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7003@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "HTTP room event")
    event.created_by_user_id = owner_id
    other_event = _event(db_session, "HTTP other room event")
    room = models.Room(event_id=event.id, name="Hall A")
    other_room = models.Room(event_id=other_event.id, name="Hall A")
    db_session.add_all([room, other_room])
    db_session.flush()
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        response = client.patch(
            f"/events/{event.id}/rooms/{room.id}",
            json={"name": "Main Hall"},
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Main Hall"

        forbidden_patch = client.patch(
            f"/events/{other_event.id}/rooms/{other_room.id}",
            json={"name": "Unauthorized Hall"},
        )
        assert forbidden_patch.status_code == 403

        forbidden_delete = client.delete(
            f"/events/{other_event.id}/rooms/{other_room.id}"
        )
        assert forbidden_delete.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_room_http_mutations_normalize_legacy_assignments(db_session):
    owner_id = 7004
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7004@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "Legacy mutation event")
    event.created_by_user_id = owner_id
    other_event = _event(db_session, "Legacy other event")
    room = models.Room(event_id=event.id, name="Hall A")
    other_room = models.Room(event_id=other_event.id, name="Hall A")
    db_session.add_all([room, other_room])
    db_session.flush()
    target_talk = models.Talk(
        event_id=event.id,
        title="Legacy target",
        room="  hall a  ",
        start=datetime(2026, 4, 1, 10, tzinfo=UTC),
        end=datetime(2026, 4, 1, 11, tzinfo=UTC),
    )
    other_talk = models.Talk(
        event_id=other_event.id,
        title="Other event target",
        room=" hall a ",
        start=datetime(2026, 4, 1, 10, tzinfo=UTC),
        end=datetime(2026, 4, 1, 11, tzinfo=UTC),
    )
    db_session.add_all([target_talk, other_talk])
    db_session.flush()
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        renamed = client.patch(
            f"/events/{event.id}/rooms/{room.id}",
            json={"name": "Main Hall"},
        )
        assert renamed.status_code == 200
        db_session.expire_all()
        assert db_session.get(models.Talk, target_talk.id).room == "Main Hall"
        assert db_session.get(models.Talk, other_talk.id).room == " hall a "

        deleted = client.delete(f"/events/{event.id}/rooms/{room.id}")
        assert deleted.status_code == 200
        db_session.expire_all()
        assert db_session.get(models.Talk, target_talk.id).room is None
        assert db_session.get(models.Talk, other_talk.id).room == " hall a "
    finally:
        app.dependency_overrides.clear()


def test_room_listing_is_read_only_for_case_and_whitespace_variants(db_session):
    owner_id = 7005
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7005@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "Duplicate legacy room event")
    event.created_by_user_id = owner_id
    other_event = _event(db_session, "Other legacy room event")
    talks = [
        models.Talk(
            event_id=event.id,
            title=f"Legacy variant {index}",
            room=room_name,
            start=datetime(2026, 5, 1, index, tzinfo=UTC),
            end=datetime(2026, 5, 1, index + 1, tzinfo=UTC),
        )
        for index, room_name in enumerate(("Hall A", " hall a ", "HALL A"), 10)
    ]
    other_talk = models.Talk(
        event_id=other_event.id,
        title="Other event room",
        room=" hall a ",
        start=datetime(2026, 5, 1, 10, tzinfo=UTC),
        end=datetime(2026, 5, 1, 11, tzinfo=UTC),
    )
    db_session.add_all([*talks, other_talk])
    db_session.flush()
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        first = client.get(f"/events/{event.id}/rooms")
        assert first.status_code == 200
        assert first.json() == []
        assert db_session.query(models.Room).filter_by(event_id=event.id).count() == 0
        assert {
            talk.room
            for talk in db_session.query(models.Talk)
            .filter(models.Talk.event_id == event.id)
            .all()
        } == {"Hall A", " hall a ", "HALL A"}
        assert db_session.get(models.Talk, other_talk.id).room == " hall a "

        second = client.get(f"/events/{event.id}/rooms")
        assert second.status_code == 200
        assert second.json() == []
        assert db_session.query(models.Room).filter_by(event_id=event.id).count() == 0
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("duplicate_name", ["hall a", " HALL A "])
def test_room_http_creation_validates_uniqueness_and_event_scope(
    db_session, duplicate_name
):
    owner_id = 7006
    db_session.add(
        models.User(
            id=owner_id,
            email="room-owner-7006@example.com",
            hashed_password="test",
            role="organizer",
        )
    )
    db_session.flush()
    event = _event(db_session, "Room creation event")
    event.created_by_user_id = owner_id
    other_event = _event(db_session, "Other room creation event")
    other_event.created_by_user_id = owner_id
    event_id = event.id
    other_event_id = other_event.id
    user = CurrentUser(user_id=owner_id, role="organizer", source="jwt")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        created = client.post(
            f"/events/{event_id}/rooms",
            json={"name": "  Hall A  ", "description": "Main venue"},
        )
        assert created.status_code == 201
        assert created.json()["event_id"] == event_id
        assert created.json()["name"] == "Hall A"
        assert created.json()["description"] == "Main venue"

        invalid = client.post(f"/events/{event_id}/rooms", json={"name": "   "})
        assert invalid.status_code == 422

        same_name_other_event = client.post(
            f"/events/{other_event_id}/rooms", json={"name": "Hall A"}
        )
        assert same_name_other_event.status_code == 201
        assert same_name_other_event.json()["event_id"] == other_event_id
        assert (
            db_session.query(models.Room)
            .filter(models.Room.event_id == other_event_id)
            .count()
            == 1
        )

        duplicate = client.post(
            f"/events/{event_id}/rooms", json={"name": duplicate_name}
        )
        assert duplicate.status_code == 409
    finally:
        app.dependency_overrides.clear()
