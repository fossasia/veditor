import pytest
from pydantic import ValidationError

from app.models import Event, Talk
from app.retention import (
    DEFAULT_FINAL_RETENTION_DAYS,
    INDEFINITE,
    RetentionPolicy,
    get_retention,
    validate_final_retention_days,
    validate_retention_overrides,
)
from app.schemas import EventCreate, EventRead


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
