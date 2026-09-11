import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings


@dataclass(frozen=True)
class PreviewPreset:
    name: str
    resolution: tuple[int, int]
    video_bitrate: int
    audio_bitrate: int = 64_000
    crf: int | None = None


PREVIEW_PRESETS: dict[str, PreviewPreset] = {
    "small_video": PreviewPreset(
        name="small_video",
        resolution=(320, 180),
        video_bitrate=150_000,
        audio_bitrate=32_000,
    ),
    "big_video": PreviewPreset(
        name="big_video",
        resolution=(640, 360),
        video_bitrate=500_000,
        audio_bitrate=64_000,
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

    environment: str = "development"
    session_secret: str | None = None
    jwt_algorithm: str = "HS256"
    session_token_expire_hours: int = 168
    access_token_expire_seconds: int = 3600

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
        "session_token_expire_hours", "access_token_expire_seconds", mode="after"
    )
    @classmethod
    def validate_token_expirations(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("token expiration values must be positive")
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

    class Config:
        env_file = ".env"


settings = Settings()
