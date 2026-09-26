from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Event, Talk
from app.retention import (
    DEFAULT_FINAL_RETENTION_DAYS,
    INDEFINITE,
    RetentionPolicy,
    enqueue_retention_sweep,
    get_retention,
    register_periodic_retention_sweep,
    run_retention_sweep,
    validate_final_retention_days,
    validate_retention_overrides,
)
from app.schemas import EventCreate, EventRead

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


def test_default_retention_policy():
    policy = RetentionPolicy()
    assert policy.final_retention_days == DEFAULT_FINAL_RETENTION_DAYS
    assert policy.final_retention_days == 14
    assert policy.is_final_indefinite is False

    # None event
    assert get_retention(None) == RetentionPolicy()

    # Event without overrides attribute or with None
    event_no_overrides = Event(name="Default Event")
    assert get_retention(event_no_overrides).final_retention_days == 14
    assert get_retention(event_no_overrides).is_final_indefinite is False

    # Event with empty overrides dict
    event_empty_overrides = Event(name="Empty Overrides", retention_overrides={})
    assert get_retention(event_empty_overrides).final_retention_days == 14

    # Plain dict without overrides
    assert get_retention({}).final_retention_days == 14


def test_event_override_final_retention_days():
    event_custom = Event(
        name="Custom Event",
        retention_overrides={"final_retention_days": 30},
    )
    policy = get_retention(event_custom)
    assert policy.final_retention_days == 30
    assert policy.is_final_indefinite is False

    # Zero days is valid non-negative integer
    event_zero = Event(
        name="Immediate Expire",
        retention_overrides={"final_retention_days": 0},
    )
    assert get_retention(event_zero).final_retention_days == 0
    assert get_retention(event_zero).is_final_indefinite is False

    # Plain dict override
    assert get_retention({"final_retention_days": 7}).final_retention_days == 7


def test_override_isolation_between_events():
    event_a = Event(name="A", retention_overrides={"final_retention_days": 7})
    event_b = Event(name="B")
    event_c = Event(name="C", retention_overrides={"final_retention_days": 60})

    assert get_retention(event_a).final_retention_days == 7
    assert get_retention(event_b).final_retention_days == 14
    assert get_retention(event_c).final_retention_days == 60


def test_indefinite_sentinel():
    event_indefinite = Event(
        name="Archive Event",
        retention_overrides={"final_retention_days": INDEFINITE},
    )
    policy = get_retention(event_indefinite)
    assert policy.final_retention_days == "indefinite"
    assert policy.is_final_indefinite is True

    # RetentionPolicy direct instantiation with indefinite sentinel
    direct_policy = RetentionPolicy(final_retention_days=INDEFINITE)
    assert direct_policy.is_final_indefinite is True


@pytest.mark.parametrize(
    "invalid_days",
    [-1, -100, 14.5, True, False, "forever", "never", None, [], {}],
)
def test_validation_rejects_invalid_final_retention_days(invalid_days):
    with pytest.raises(ValueError):
        validate_final_retention_days(invalid_days)

    with pytest.raises(ValueError):
        RetentionPolicy(final_retention_days=invalid_days)

    with pytest.raises(ValueError):
        validate_retention_overrides({"final_retention_days": invalid_days})


def test_validation_rejects_non_dict_overrides():
    with pytest.raises(TypeError, match="must be a dictionary or None"):
        validate_retention_overrides("not a dict")

    with pytest.raises(TypeError, match="must be a dictionary or None"):
        validate_retention_overrides([14])


def test_model_write_time_validation():
    # Constructor write-time rejection
    with pytest.raises(ValueError):
        Event(name="Bad", retention_overrides={"final_retention_days": -1})

    with pytest.raises(ValueError):
        Event(name="Bad", retention_overrides={"final_retention_days": "permanent"})

    # Attribute mutation write-time rejection
    event = Event(name="Mutable Event")
    with pytest.raises(ValueError):
        event.retention_overrides = {"final_retention_days": -5}

    with pytest.raises((ValueError, TypeError)):
        event.retention_overrides = "invalid_string"


def test_pydantic_schema_validation():
    # Valid schemas
    create_schema = EventCreate(
        name="Conference", retention_overrides={"final_retention_days": 21}
    )
    assert create_schema.retention_overrides == {"final_retention_days": 21}

    read_schema = EventRead(
        id=1,
        name="Conference",
        retention_overrides={"final_retention_days": "indefinite"},
    )
    assert read_schema.retention_overrides == {"final_retention_days": "indefinite"}

    # Invalid schemas
    with pytest.raises(ValidationError):
        EventCreate(
            name="Bad Event",
            retention_overrides={"final_retention_days": -1},
        )

    with pytest.raises(ValidationError):
        EventCreate(
            name="Bad Event",
            retention_overrides={"final_retention_days": "unlimited"},
        )

    with pytest.raises(ValidationError):
        EventCreate(
            name="Bad Event",
            retention_overrides="string",
        )

    with pytest.raises(ValidationError):
        EventCreate(
            name="Bad Event",
            retention_overrides={123: 14},
        )


def test_model_in_place_mutation_validation():
    event = Event(
        name="Mutable Event",
        retention_overrides={"final_retention_days": 14},
    )

    # In-place __setitem__ invalid value raises ValueError
    with pytest.raises(ValueError):
        event.retention_overrides["final_retention_days"] = -1

    with pytest.raises(ValueError):
        event.retention_overrides["final_retention_days"] = "invalid_string"

    # In-place __setitem__ non-string key raises TypeError
    with pytest.raises(TypeError):
        event.retention_overrides[123] = 14

    # In-place update rejection
    with pytest.raises(ValueError):
        event.retention_overrides.update({"final_retention_days": -10})

    with pytest.raises(TypeError):
        event.retention_overrides.update({456: 30})

    # In-place setdefault rejection when setting new key with non-string key
    with pytest.raises(TypeError):
        event.retention_overrides.setdefault(789, 14)

    # In-place setdefault rejection when setting new invalid final_retention_days
    event_empty = Event(name="Empty Overrides Event", retention_overrides={})
    with pytest.raises(ValueError):
        event_empty.retention_overrides.setdefault("final_retention_days", -5)

    # Valid in-place mutations succeed
    event.retention_overrides["final_retention_days"] = 30
    assert event.retention_overrides["final_retention_days"] == 30

    event.retention_overrides.update({"extra": "allowed"})
    assert event.retention_overrides["extra"] == "allowed"

    val = event.retention_overrides.setdefault("new_key", "default_val")
    assert val == "default_val"
    assert event.retention_overrides["new_key"] == "default_val"

    # In-place |= operator rejection
    with pytest.raises(ValueError):
        event.retention_overrides |= {"final_retention_days": -20}

    with pytest.raises(TypeError):
        event.retention_overrides |= {999: 10}

    # In-place |= operator valid mutation succeeds
    event.retention_overrides |= {"final_retention_days": 45, "or_key": "val"}
    assert event.retention_overrides["final_retention_days"] == 45
    assert event.retention_overrides["or_key"] == "val"


def test_sparse_overrides_forward_compatibility():
    sparse_data = {
        "final_retention_days": 30,
        "future_knob": "something_else",
    }
    event = Event(name="Future Proof", retention_overrides=sparse_data)
    assert event.retention_overrides == sparse_data
    policy = get_retention(event)
    assert policy.final_retention_days == 30

    # Without final_retention_days but other future keys
    event_no_final = Event(name="Other Keys", retention_overrides={"other_key": 123})
    assert get_retention(event_no_final).final_retention_days == 14


def test_validation_rejects_non_string_keys():
    with pytest.raises(TypeError, match="must be strings"):
        validate_retention_overrides({123: 14})

    with pytest.raises(TypeError):
        Event(name="Invalid Keys", retention_overrides={123: 14})


def test_get_retention_from_talk_model():
    event = Event(name="Conf", retention_overrides={"final_retention_days": 30})
    talk = Talk(title="Keynote", event=event)
    policy = get_retention(talk)
    assert policy.final_retention_days == 30
    assert policy.is_final_indefinite is False


def test_sweep_removes_final_past_default_window(db_session, fake_storage):
    event = Event(name="Default Retention Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=15)
    talk = Talk(
        event_id=event.id,
        title="Old Done Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    raw_key = f"{talk.id}/raw/raw.mp4"
    fake_storage.put(final_key, b"final media data")
    fake_storage.put(raw_key, b"raw media data")

    swept = run_retention_sweep(db=db_session, storage=fake_storage)

    assert talk.id in swept
    assert not fake_storage.exists(final_key)
    assert fake_storage.exists(raw_key)


def test_sweep_preserves_final_within_window(db_session, fake_storage):
    event = Event(name="Default Retention Event")
    db_session.add(event)
    db_session.flush()

    recent_date = datetime.now(UTC) - timedelta(days=10)
    talk = Talk(
        event_id=event.id,
        title="Recent Done Talk",
        start=recent_date,
        end=recent_date,
        status="done",
        updated_at=recent_date,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    fake_storage.put(final_key, b"final media data")

    swept = run_retention_sweep(db=db_session, storage=fake_storage)

    assert talk.id not in swept
    assert fake_storage.exists(final_key)


def test_sweep_honors_custom_override(db_session, fake_storage):
    # Event with 30-day retention
    event_30 = Event(
        name="30 Day Event",
        retention_overrides={"final_retention_days": 30},
    )
    db_session.add(event_30)
    db_session.flush()

    date_20_days_ago = datetime.now(UTC) - timedelta(days=20)
    talk_30 = Talk(
        event_id=event_30.id,
        title="Talk 20 Days Old",
        start=date_20_days_ago,
        end=date_20_days_ago,
        status="done",
        updated_at=date_20_days_ago,
    )
    db_session.add(talk_30)

    # Event with 7-day retention
    event_7 = Event(
        name="7 Day Event",
        retention_overrides={"final_retention_days": 7},
    )
    db_session.add(event_7)
    db_session.flush()

    date_8_days_ago = datetime.now(UTC) - timedelta(days=8)
    talk_7 = Talk(
        event_id=event_7.id,
        title="Talk 8 Days Old",
        start=date_8_days_ago,
        end=date_8_days_ago,
        status="done",
        updated_at=date_8_days_ago,
    )
    db_session.add(talk_7)
    db_session.flush()

    key_30 = f"{talk_30.id}/final/final.mp4"
    key_7 = f"{talk_7.id}/final/final.mp4"
    fake_storage.put(key_30, b"data 30")
    fake_storage.put(key_7, b"data 7")

    swept = run_retention_sweep(db=db_session, storage=fake_storage)

    assert talk_30.id not in swept
    assert talk_7.id in swept
    assert fake_storage.exists(key_30)
    assert not fake_storage.exists(key_7)


def test_sweep_honors_indefinite_override(db_session, fake_storage):
    event = Event(
        name="Indefinite Event",
        retention_overrides={"final_retention_days": INDEFINITE},
    )
    db_session.add(event)
    db_session.flush()

    ancient_date = datetime.now(UTC) - timedelta(days=365)
    talk = Talk(
        event_id=event.id,
        title="Ancient Talk",
        start=ancient_date,
        end=ancient_date,
        status="done",
        updated_at=ancient_date,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    fake_storage.put(final_key, b"preserved indefinitely")

    swept = run_retention_sweep(db=db_session, storage=fake_storage)

    assert talk.id not in swept
    assert fake_storage.exists(final_key)


def test_sweep_never_touches_rejected_talks(db_session, fake_storage):
    event = Event(name="Default Event")
    db_session.add(event)
    db_session.flush()

    old_date = datetime.now(UTC) - timedelta(days=30)
    talk = Talk(
        event_id=event.id,
        title="Rejected Talk",
        start=old_date,
        end=old_date,
        status="rejected",
        updated_at=old_date,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    fake_storage.put(final_key, b"should never be touched")

    swept = run_retention_sweep(db=db_session, storage=fake_storage)

    assert talk.id not in swept
    assert fake_storage.exists(final_key)


def test_sweep_idempotency_on_already_cleaned_paths(db_session, fake_storage):
    event = Event(name="Idempotent Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=20)
    talk = Talk(
        event_id=event.id,
        title="Idempotency Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    fake_storage.put(final_key, b"data")

    # Run 1: sweeps talk
    swept1 = run_retention_sweep(db=db_session, storage=fake_storage)
    assert talk.id in swept1
    assert not fake_storage.exists(final_key)

    # Run 2: safe, produces no errors and does not re-sweep
    swept2 = run_retention_sweep(db=db_session, storage=fake_storage)
    assert swept2 == []


def test_sweep_dynamic_override_takes_effect_without_redeploy(db_session, fake_storage):
    event = Event(
        name="Dynamic Override Event",
        retention_overrides={"final_retention_days": 30},
    )
    db_session.add(event)
    db_session.flush()

    date_15_days_ago = datetime.now(UTC) - timedelta(days=15)
    talk = Talk(
        event_id=event.id,
        title="Dynamic Talk",
        start=date_15_days_ago,
        end=date_15_days_ago,
        status="done",
        updated_at=date_15_days_ago,
    )
    db_session.add(talk)
    db_session.flush()

    final_key = f"{talk.id}/final/final.mp4"
    fake_storage.put(final_key, b"data")

    # Run 1: 15 days elapsed < 30 days override -> untouched
    swept1 = run_retention_sweep(db=db_session, storage=fake_storage)
    assert talk.id not in swept1
    assert fake_storage.exists(final_key)

    # Organizer dynamically changes override to 10 days
    event.retention_overrides = {"final_retention_days": 10}
    db_session.flush()

    # Run 2: 15 days elapsed >= 10 days override -> swept immediately without restart
    swept2 = run_retention_sweep(db=db_session, storage=fake_storage)
    assert talk.id in swept2
    assert not fake_storage.exists(final_key)


def test_enqueue_retention_sweep():
    mock_queue = MagicMock()
    enqueue_retention_sweep(queue=mock_queue)
    mock_queue.enqueue.assert_called_once_with(
        run_retention_sweep,
        job_timeout=600,
        description="Retention sweep for expired final artifacts",
    )


def test_register_periodic_retention_sweep():
    mock_queue = MagicMock()
    mock_queue.connection = MagicMock()

    job = register_periodic_retention_sweep(queue=mock_queue, interval_seconds=1800)
    assert job is not None
    assert mock_queue.enqueue_in.called
    args, kwargs = mock_queue.enqueue_in.call_args
    assert args[0] == timedelta(seconds=1800)
    assert args[1] == run_retention_sweep
    assert kwargs["job_id"] == "retention_sweep"
    assert kwargs["job_timeout"] == 600
    assert kwargs["repeat"].intervals == [1800]


def test_register_periodic_retention_sweep_rejects_non_positive_interval():
    mock_queue = MagicMock()
    with pytest.raises(ValueError, match="interval must be positive"):
        register_periodic_retention_sweep(queue=mock_queue, interval_seconds=0)

    with pytest.raises(ValueError, match="interval must be positive"):
        register_periodic_retention_sweep(queue=mock_queue, interval_seconds=-10)


def test_settings_retention_sweep_interval_validation():
    from app.config import Settings

    with pytest.raises(
        ValueError, match="retention_sweep_interval_seconds must be positive"
    ):
        Settings(retention_sweep_interval_seconds=0)

    with pytest.raises(
        ValueError, match="retention_sweep_interval_seconds must be positive"
    ):
        Settings(retention_sweep_interval_seconds=-1)


def test_sweep_handles_list_keys_failure_gracefully(db_session):
    event = Event(name="Resilience Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=20)
    talk1 = Talk(
        event_id=event.id,
        title="Failing Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    talk2 = Talk(
        event_id=event.id,
        title="Succeeding Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add_all([talk1, talk2])
    db_session.flush()

    mock_storage = MagicMock()

    def mock_list_keys(prefix):
        if prefix.startswith(f"{talk1.id}/"):
            raise OSError("Simulated disk I/O error")
        return [f"{prefix}/final.mp4"]

    mock_storage.list_keys.side_effect = mock_list_keys

    swept = run_retention_sweep(db=db_session, storage=mock_storage)

    # talk1 should fail without crashing sweep; talk2 should succeed
    assert talk1.id not in swept
    assert talk2.id in swept
    mock_storage.delete.assert_called_once_with(f"{talk2.id}/final")
    assert talk1.final_cleaned_at is None
    assert talk2.final_cleaned_at is not None


def test_sweep_sets_final_cleaned_at_and_skips_subsequent(db_session, fake_storage):
    event = Event(name="Cleaned Tracking Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=20)
    talk = Talk(
        event_id=event.id,
        title="Tracking Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add(talk)
    db_session.flush()

    fake_storage.put(f"{talk.id}/final/test.mp4", b"data")
    assert talk.final_cleaned_at is None

    swept = run_retention_sweep(db=db_session, storage=fake_storage)
    assert talk.id in swept
    assert talk.final_cleaned_at is not None

    # Subsequent sweep does not re-process the talk
    swept2 = run_retention_sweep(db=db_session, storage=fake_storage)
    assert swept2 == []


@patch("rq.job.Job.fetch")
def test_register_periodic_retention_sweep_deletes_stale_job(mock_fetch):
    mock_stale_job = MagicMock()
    mock_stale_job.get_status.return_value = "finished"
    mock_fetch.return_value = mock_stale_job

    mock_queue = MagicMock()
    mock_queue.connection = MagicMock()

    register_periodic_retention_sweep(queue=mock_queue, interval_seconds=1800)
    mock_stale_job.delete.assert_called_once()
    assert mock_queue.enqueue_in.called


def test_register_periodic_retention_sweep_times_parameter():
    mock_queue = MagicMock()
    mock_queue.connection = MagicMock()

    job = register_periodic_retention_sweep(
        queue=mock_queue, interval_seconds=600, times=5
    )
    assert job is not None
    _, kwargs = mock_queue.enqueue_in.call_args
    assert kwargs["repeat"].times == 5

    with pytest.raises(ValueError, match="times must be positive"):
        register_periodic_retention_sweep(
            queue=mock_queue, interval_seconds=600, times=0
        )


def test_sweep_raises_and_stops_on_db_flush_failure(db_session):
    from sqlalchemy.exc import SQLAlchemyError

    event = Event(name="Flush Failure Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=20)
    talk1 = Talk(
        event_id=event.id,
        title="First Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    talk2 = Talk(
        event_id=event.id,
        title="Second Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add_all([talk1, talk2])
    db_session.flush()

    mock_storage = MagicMock()
    mock_storage.list_keys.return_value = ["dummy.mp4"]

    original_flush = db_session.flush

    def failing_flush():
        raise SQLAlchemyError("Simulated database flush failure")

    db_session.flush = failing_flush
    try:
        with pytest.raises(SQLAlchemyError, match="Simulated database flush failure"):
            run_retention_sweep(db=db_session, storage=mock_storage)
    finally:
        db_session.flush = original_flush

    # Storage delete should have been called for talk1 before flush failed,
    # but NOT for talk2 since the sweep stops immediately
    assert mock_storage.delete.call_count == 1
    assert mock_storage.delete.call_args[0][0] == f"{talk1.id}/final"


def test_register_periodic_retention_sweep_retry():
    from rq import Retry

    mock_queue = MagicMock()
    mock_queue.connection = MagicMock()

    job = register_periodic_retention_sweep(queue=mock_queue, interval_seconds=1800)
    assert job is not None
    _, kwargs = mock_queue.enqueue_in.call_args
    assert "retry" in kwargs
    assert kwargs["retry"].max == 3

    custom_retry = Retry(max=5)
    register_periodic_retention_sweep(
        queue=mock_queue, interval_seconds=1800, retry=custom_retry
    )
    _, kwargs = mock_queue.enqueue_in.call_args
    assert kwargs["retry"] == custom_retry


def test_sweep_deletes_when_storage_lacks_list_keys(db_session):
    event = Event(name="No List Keys Event")
    db_session.add(event)
    db_session.flush()

    past_date = datetime.now(UTC) - timedelta(days=20)
    talk = Talk(
        event_id=event.id,
        title="No ListKeys Talk",
        start=past_date,
        end=past_date,
        status="done",
        updated_at=past_date,
    )
    db_session.add(talk)
    db_session.flush()

    # Minimal storage mock without list_keys attribute
    class StorageWithoutListKeys:
        def __init__(self):
            self.deleted_prefixes = []

        def delete(self, prefix: str) -> None:
            self.deleted_prefixes.append(prefix)

    mock_storage = StorageWithoutListKeys()
    assert not hasattr(mock_storage, "list_keys")

    swept = run_retention_sweep(db=db_session, storage=mock_storage)
    assert talk.id in swept
    assert mock_storage.deleted_prefixes == [f"{talk.id}/final"]
    assert talk.final_cleaned_at is not None
