import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import PositiveInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class PreviewPreset:
    name: str
    resolution: tuple[int, int]
    video_bitrate: int
    audio_bitrate: int = 64_000
    crf: int | None = None
    preset_speed: str = "veryfast"


PREVIEW_PRESETS: dict[str, PreviewPreset] = {
    "small_video": PreviewPreset(
        name="small_video",
        resolution=(320, 180),
        video_bitrate=150_000,
        audio_bitrate=32_000,
        preset_speed="veryfast",
    ),
    "big_video": PreviewPreset(
        name="big_video",
        resolution=(640, 360),
        video_bitrate=500_000,
        audio_bitrate=64_000,
        preset_speed="veryfast",
    ),
}


ALLOWED_JWT_ALGORITHMS: tuple[str, ...] = ("HS256", "HS384", "HS512")


class Settings(BaseSettings):
    postgres_user: str = "veditor"
    postgres_password: str = "password"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "veditor"

    redis_url: str = "redis://localhost:6379/0"

    data_dir: str = "data"
    storage_backend: Literal["local"] = "local"
    ingest_roots: list[Path] = []
    preview_presets: dict[str, PreviewPreset] = PREVIEW_PRESETS
    disk_guard_multiplier: float = 3.0
    retention_sweep_interval_seconds: int = 3600
    max_bumper_upload_size_bytes: PositiveInt = 100 * 1024 * 1024
    encoder_threads: PositiveInt | None = None

    environment: str = "development"
    session_secret: str | None = None
    jwt_algorithm: str = "HS256"
    session_token_expire_hours: int = 168
    access_token_expire_seconds: int = 3600
    sso_token_expire_seconds: int = 300

    @field_validator("jwt_algorithm", mode="after")
    @classmethod
    def validate_jwt_algorithm(cls, value: str) -> str:
        if value not in ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"jwt_algorithm must be one of {sorted(ALLOWED_JWT_ALGORITHMS)}"
            )
        return value

    @field_validator("session_secret", mode="after")
    @classmethod
    def validate_session_secret(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 bytes long")
        return value

    @field_validator(
        "session_token_expire_hours",
        "access_token_expire_seconds",
        "sso_token_expire_seconds",
        mode="after",
    )
    @classmethod
    def validate_token_expirations(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("token expiration values must be positive")
        return value

    @field_validator("retention_sweep_interval_seconds", mode="after")
    @classmethod
    def validate_retention_sweep_interval_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("retention_sweep_interval_seconds must be positive")
        return value

    @field_validator("disk_guard_multiplier", mode="after")
    @classmethod
    def validate_disk_guard_multiplier(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("disk_guard_multiplier must be a finite positive number")
        return value

    @field_validator("ingest_roots", mode="after")
    @classmethod
    def validate_ingest_roots(cls, roots: list[Path]) -> list[Path]:
        for r in roots:
            if not r.is_absolute():
                raise ValueError(f"ingest_roots entries must be absolute paths: {r}")
        return [r.resolve() for r in roots]

    @property
    def database_url(self) -> str:
        return f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    title: str
    description: str
    default_value: Any
    value_type: type
    options: tuple[tuple[str, str], ...] = ()
    input_type: str = "select"
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None


SYSTEM_SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    "detect_duration_tolerance_seconds": SettingDefinition(
        key="detect_duration_tolerance_seconds",
        title="Schedule Time Margin",
        description="Allowed difference between the scheduled talk time and the video recording length.",
        default_value=300.0,
        value_type=float,
        input_type="range",
        min_value=60.0,
        max_value=1800.0,
        step=30.0,
        options=(
            ("60.0", "Strict (1 minute)"),
            ("180.0", "Moderate (3 minutes)"),
            ("300.0", "Standard (5 minutes) - Default"),
            ("600.0", "Relaxed (10 minutes)"),
            ("900.0", "Wide (15 minutes)"),
            ("1800.0", "Maximum (30 minutes)"),
        ),
    ),
    "loudness_target_lufs": SettingDefinition(
        key="loudness_target_lufs",
        title="Speech Volume Level",
        description="Standardized loudness level for speech so all recorded talks have even, balanced audio.",
        default_value=-16.0,
        value_type=float,
        options=(
            ("-14.0", "Loud (-14 LUFS - for noisy venues or mobile)"),
            ("-16.0", "Standard Web (-16 LUFS - Recommended / Default)"),
            ("-18.0", "Cinematic (-18 LUFS)"),
            ("-23.0", "European Broadcast (-23 LUFS - EBU R128)"),
            ("-24.0", "US Broadcast (-24 LUFS - ATSC A/85)"),
        ),
    ),
    "default_preview_preset": SettingDefinition(
        key="default_preview_preset",
        title="Draft Video Preview Quality",
        description="Quality and resolution profile used for generating quick draft previews in the editor.",
        default_value="small_video",
        value_type=str,
        options=(
            ("small_video", "Standard Draft (320x180, Fastest) - Default"),
            ("big_video", "High Detail Draft (640x360, Sharper)"),
        ),
    ),
    "default_transcode_preset": SettingDefinition(
        key="default_transcode_preset",
        title="Final Video Export Quality",
        description="Resolution and quality profile used when rendering the finished published video.",
        default_value="1080p_default",
        value_type=str,
        options=(
            ("1080p_default", "Full HD (1080p, High Quality) - Default"),
            ("720p", "HD (720p, Standard)"),
            ("4k_master", "Ultra HD (4K Master)"),
        ),
    ),
}

EXCLUDED_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "postgres_user",
        "postgres_password",
        "postgres_host",
        "postgres_port",
        "postgres_db",
        "database_url",
        "redis_url",
        "session_secret",
        "jwt_algorithm",
        "data_dir",
        "ingest_roots",
        "storage_backend",
    }
)


def _cast_setting_value(key: str, raw_val: str, default: Any = None) -> Any:
    defn = SYSTEM_SETTING_DEFINITIONS.get(key)
    fallback = (
        default if default is not None else (defn.default_value if defn else None)
    )
    v_type = (
        defn.value_type if defn else (type(default) if default is not None else str)
    )

    try:
        if v_type is float:
            val = float(raw_val)
            if not math.isfinite(val):
                raise ValueError("non-finite float")
            return val
        if v_type is int:
            return int(raw_val)
        if v_type is bool:
            return raw_val.strip().lower() in ("true", "1", "yes", "on")
        if v_type is str:
            return str(raw_val)
        return json.loads(raw_val)
    except Exception:  # noqa: BLE001
        return fallback if fallback is not None else raw_val


def get_setting(key: str, default: Any = None, db: Any = None) -> Any:
    """Resolve a configuration setting, checking DB overrides before falling back to defaults."""
    normalized_key = key.strip().lower()
    if normalized_key in EXCLUDED_SETTING_KEYS:
        return getattr(settings, normalized_key, default)

    try:
        from app.models import SystemSetting

        if db is not None:
            row = db.get(SystemSetting, normalized_key)
            if row is not None:
                return _cast_setting_value(normalized_key, row.value, default)
        else:
            from app.db import SessionLocal

            with SessionLocal() as session:
                row = session.get(SystemSetting, normalized_key)
                if row is not None:
                    return _cast_setting_value(normalized_key, row.value, default)
    except Exception:  # noqa: BLE001, S110
        pass

    if default is not None:
        return default
    if hasattr(settings, normalized_key):
        return getattr(settings, normalized_key)
    defn = SYSTEM_SETTING_DEFINITIONS.get(normalized_key)
    return defn.default_value if defn else None


settings = Settings()
