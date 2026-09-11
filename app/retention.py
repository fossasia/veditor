from dataclasses import dataclass
from typing import Any, Final, Literal

DEFAULT_FINAL_RETENTION_DAYS: Final[int] = 14
INDEFINITE: Final = "indefinite"

RetentionDays = int | Literal["indefinite"]


def validate_final_retention_days(value: Any) -> RetentionDays:
    """Validate final_retention_days is a non-negative int or 'indefinite'."""
    if value == INDEFINITE:
        return INDEFINITE
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError(
        f"final_retention_days must be a non-negative integer or '{INDEFINITE}', got {value!r}"
    )


def validate_retention_overrides(overrides: Any) -> dict[str, Any] | None:
    """Validate retention overrides dictionary or None."""
    if overrides is None:
        return None
    if not isinstance(overrides, dict):
        raise TypeError("retention_overrides must be a dictionary or None")

    for key in overrides:
        if not isinstance(key, str):
            raise TypeError(
                f"All keys in retention_overrides must be strings, got {type(key).__name__}"
            )

    if "final_retention_days" in overrides:
        validate_final_retention_days(overrides["final_retention_days"])

    return overrides


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention configuration for intermediate and final media stages.

    Note: preview/ and cut/ deletion is lifecycle event-driven (e.g. done/rejected)
    and does not expose a day-count knob.
    """

    final_retention_days: RetentionDays = DEFAULT_FINAL_RETENTION_DAYS

    def __post_init__(self) -> None:
        validate_final_retention_days(self.final_retention_days)

    @property
    def is_final_indefinite(self) -> bool:
        """Whether final artifacts are retained indefinitely."""
        return self.final_retention_days == INDEFINITE


def get_retention(event: Any | None = None) -> RetentionPolicy:
    """Resolve retention policy for an event or talk, falling back to defaults."""
    if event is None:
        return RetentionPolicy()

    if isinstance(event, dict):
        overrides = event.get("retention_overrides", event)
    else:
        overrides = getattr(event, "retention_overrides", None)
        if overrides is None and hasattr(event, "event"):
            overrides = getattr(event.event, "retention_overrides", None)

    if not overrides or not isinstance(overrides, dict):
        return RetentionPolicy()

    final_days = overrides.get("final_retention_days")
    if final_days is None:
        return RetentionPolicy()

    return RetentionPolicy(final_retention_days=final_days)
