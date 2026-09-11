from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Client, Event, Job, Review, Talk

# Use the postgres instance from docker-compose, but we will wrap tests in a transaction
engine = create_engine(settings.database_url)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope="module")
def setup_database():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db_session(setup_database):
    connection = engine.connect()
    transaction = connection.begin()
    session = TestingSessionLocal(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def test_create_event_and_talk_relationships(db_session):
    event = Event(name="FOSSASIA 2026")
    db_session.add(event)
    db_session.flush()

    talk = Talk(
        event_id=event.id,
        title="Keynote",
        room="Main Hall",
        start=datetime(2026, 3, 20, 9, 0, tzinfo=UTC),
        end=datetime(2026, 3, 20, 10, 0, tzinfo=UTC),
    )
    db_session.add(talk)
    db_session.flush()

    # Test relationships
    assert talk.event == event
    assert event.retention_overrides is None
    assert len(event.talks) == 1
    assert event.talks[0] == talk
    assert talk.include_intro is False
    assert talk.include_outro is False
    assert talk.intro_source is None
    assert talk.outro_source is None
    assert talk.custom_intro_path is None
    assert talk.custom_outro_path is None

    job = Job(talk_id=talk.id, kind="cut", status="running")
    db_session.add(job)

    review = Review(talk_id=talk.id, decision="approved", note="Looks good")
    db_session.add(review)
    db_session.flush()

    assert job.talk == talk
    assert review.talk == talk
    assert len(talk.jobs) == 1
    assert talk.jobs[0] == job
    assert len(talk.reviews) == 1
    assert talk.reviews[0] == review
    assert review.created_at is not None


def test_required_fields_enforced(db_session):
    # Event missing name
    with pytest.raises(IntegrityError):
        event = Event()
        db_session.add(event)
        db_session.flush()
    db_session.rollback()

    event = Event(name="Test")
    db_session.add(event)
    db_session.flush()

    # Talk missing title
    with pytest.raises(IntegrityError):
        talk = Talk(
            event_id=event.id,
            start=datetime.now(UTC),
            end=datetime.now(UTC),
        )
        db_session.add(talk)
        db_session.flush()
    db_session.rollback()


def test_client_model(db_session):
    client = Client(hashed_key="hash123", event_ids=[1, 2, 3])
    db_session.add(client)
    db_session.flush()

    assert client.id is not None
    assert client.hashed_key == "hash123"
    assert client.event_ids == [1, 2, 3]


def test_talk_unique_constraint_enforced(db_session):
    event = Event(name="Unique Test Event")
    db_session.add(event)
    db_session.flush()

    start_time = datetime(2026, 5, 10, 9, 0, tzinfo=UTC)
    end_time = datetime(2026, 5, 10, 10, 0, tzinfo=UTC)

    talk1 = Talk(
        event_id=event.id,
        title="Duplicate Check",
        room="Room 1",
        start=start_time,
        end=end_time,
    )
    db_session.add(talk1)
    db_session.flush()

    talk2 = Talk(
        event_id=event.id,
        title="Duplicate Check",
        room="Room 2",
        start=start_time,
        end=end_time,
    )
    db_session.add(talk2)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_event_retention_overrides_persistence(db_session):
    event = Event(
        name="Retention Test Event",
        retention_overrides={"final_retention_days": 45, "extra": "data"},
    )
    db_session.add(event)
    db_session.flush()

    assert event.id is not None
    assert event.retention_overrides == {"final_retention_days": 45, "extra": "data"}

    db_session.expire_all()
    reloaded = db_session.query(Event).filter(Event.id == event.id).first()
    assert reloaded is not None
    assert reloaded.retention_overrides == {"final_retention_days": 45, "extra": "data"}

    reloaded.retention_overrides["final_retention_days"] = 60
    reloaded.retention_overrides["new_key"] = "persisted"
    db_session.flush()

    db_session.expire_all()
    reloaded_again = db_session.query(Event).filter(Event.id == event.id).first()
    assert reloaded_again is not None
    assert reloaded_again.retention_overrides == {
        "final_retention_days": 60,
        "extra": "data",
        "new_key": "persisted",
    }

    reloaded_again.retention_overrides |= {
        "final_retention_days": 90,
        "ior_key": "persisted_ior",
    }
    db_session.flush()

    db_session.expire_all()
    reloaded_third = db_session.query(Event).filter(Event.id == event.id).first()
    assert reloaded_third is not None
    assert reloaded_third.retention_overrides == {
        "final_retention_days": 90,
        "extra": "data",
        "new_key": "persisted",
        "ior_key": "persisted_ior",
    }
