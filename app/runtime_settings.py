"""Runtime-tunable platform settings that admins can override from the UI.

Only settings listed in ``RUNTIME_SETTINGS`` can be overridden. Everything else,
including secrets and connection strings, stays in environment variables.

Resolution order for a setting is: a valid ``system_settings`` row, then the
environment/code default. Values are read on every call (no cache), so pipeline
jobs pick up a change on their next run without a restart.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db import SessionLocal
from app.models import SystemSetting
from app.pipeline.detect import DETECT_DURATION_TOLERANCE_SECONDS
from app.pipeline.loudness import DEFAULT_TARGET_LUFS
from app.pipeline.transcode import PRESET_1080P_DEFAULT, TRANSCODE_PRESETS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSetting:
    key: str
    label: str
    description: str
    kind: Literal["float", "choice"]
    default: Callable[[], Any]
    unit: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    choices: Callable[[], list[str]] | None = None

    def parse(self, raw: Any) -> Any:
        """Coerce and validate a raw (form or JSON) value. Raises ValueError."""
        if self.kind == "choice":
            value = str(raw).strip() if raw is not None else ""
            allowed = self.choices() if self.choices else []
            if value not in allowed:
                raise ValueError(f"must be one of: {', '.join(allowed)}")
            return value

        try:
            value = float(raw.strip() if isinstance(raw, str) else raw)
        except TypeError, ValueError:
            raise ValueError("must be a number") from None
        if not math.isfinite(value):
            raise ValueError("must be a finite number")
        if self.min_value is not None and value < self.min_value:
            raise ValueError(f"must be at least {self.min_value:g}")
        if self.max_value is not None and value > self.max_value:
            raise ValueError(f"must be at most {self.max_value:g}")
        return value


RUNTIME_SETTINGS: dict[str, RuntimeSetting] = {
    s.key: s
    for s in (
        RuntimeSetting(
            key="detect_duration_tolerance_seconds",
            label="Detection duration tolerance",
            description=(
                "Maximum allowed difference between a recording's duration and "
                "its scheduled slot before detection rejects it."
            ),
            kind="float",
            unit="seconds",
            min_value=0,
            max_value=86_400,
            step=1,
            default=lambda: DETECT_DURATION_TOLERANCE_SECONDS,
        ),
        RuntimeSetting(
            key="loudness_target_lufs",
            label="Loudness target",
            description=(
                "Integrated loudness that audio is normalized to. Raise it (e.g. "
                "-14) for unusually quiet recordings."
            ),
            kind="float",
            unit="LUFS",
            min_value=-70,
            max_value=0,
            step=0.5,
            default=lambda: DEFAULT_TARGET_LUFS,
        ),
        RuntimeSetting(
            key="preview_preset",
            label="Review preview preset",
            description="Preset used to render the low-resolution review preview.",
            kind="choice",
            choices=lambda: list(settings.preview_presets),
            default=lambda: "small_video",
        ),
        RuntimeSetting(
            key="transcode_preset",
            label="Final transcode preset",
            description="Encoder preset used to produce the published video.",
            kind="choice",
            choices=lambda: list(TRANSCODE_PRESETS),
            default=lambda: PRESET_1080P_DEFAULT.name,
        ),
        RuntimeSetting(
            key="disk_guard_multiplier",
            label="Disk guard multiplier",
            description=(
                "Ingest requires this many times the recording's size in free "
                "storage space before accepting it."
            ),
            kind="float",
            unit="x file size",
            min_value=1,
            max_value=100,
            step=0.1,
            default=lambda: settings.disk_guard_multiplier,
        ),
    )
}


def resolve_setting(
    spec: RuntimeSetting, row: SystemSetting | None
) -> tuple[Any, bool]:
    """Return (effective value, is_overridden). Invalid stored values fall back."""
    if row is not None:
        try:
            return spec.parse(row.value), True
        except ValueError as exc:
            logger.warning(
                "Ignoring invalid override for setting %s (%r): %s",
                spec.key,
                row.value,
                exc,
            )
    return spec.default(), False


def get_setting(key: str) -> Any:
    """Return the effective value of a runtime setting.

    Uses its own short-lived session so a failed lookup (e.g. migration not yet
    applied) can never poison the caller's transaction; on DB errors the
    environment/code default is returned.
    """
    spec = RUNTIME_SETTINGS[key]
    try:
        with SessionLocal() as db:
            row = db.get(SystemSetting, key)
    except SQLAlchemyError as exc:
        logger.warning("Could not read setting %s; using default: %s", key, exc)
        return spec.default()
    return resolve_setting(spec, row)[0]
