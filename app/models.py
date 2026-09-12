from datetime import UTC, datetime
from typing import Any, Self

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db import Base
from app.retention import validate_retention_overrides


class RetentionOverrides(MutableDict):
    """Mutable dict tracking changes and enforcing validation on retention overrides."""

    def __setitem__(self, key: Any, value: Any) -> None:
        validate_retention_overrides({**self, key: value})
        super().__setitem__(key, value)

    def update(self, *args: Any, **kwargs: Any) -> None:
        candidate = dict(self)
        candidate.update(*args, **kwargs)
        validate_retention_overrides(candidate)
        super().update(*args, **kwargs)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            validate_retention_overrides({**self, key: default})
        return super().setdefault(key, default)

    def __ior__(self, other: Any) -> Self:
        self.update(other)
        return self


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("idx_users_email", "email", unique=True),
        CheckConstraint(
            "role IN ('user', 'organizer', 'admin')",
            name="ck_users_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(32),
        default="user",
        server_default=text("'user'"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list[Event]] = relationship(back_populates="created_by_user")
    reviews: Mapped[list[Review]] = relationship(back_populates="user")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    retention_overrides: Mapped[dict[str, Any] | None] = mapped_column(
        RetentionOverrides.as_mutable(JSONB), nullable=True, default=None
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_events_created_by_user_id_users",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    talks: Mapped[list[Talk]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    created_by_user: Mapped[User | None] = relationship(back_populates="events")

    @validates("retention_overrides")
    def _validate_retention_overrides(self, key: str, value: Any) -> Any:
        return validate_retention_overrides(value)


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    hashed_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    webhook_secret: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )


class Talk(Base):
    __tablename__ = "talks"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "title", "start", name="uq_talks_event_id_title_start"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    room: Mapped[str | None] = mapped_column(String(255))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="waiting_for_files"
    )
    raw_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    cut_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    cut_end: Mapped[float | None] = mapped_column(Float, nullable=True)
    include_intro: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    include_outro: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    intro_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    outro_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    custom_intro_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_outro_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    event: Mapped[Event] = relationship(back_populates="talks")
    jobs: Mapped[list[Job]] = relationship(
        back_populates="talk", cascade="all, delete-orphan"
    )
    reviews: Mapped[list[Review]] = relationship(
        back_populates="talk", cascade="all, delete-orphan"
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    talk_id: Mapped[int] = mapped_column(ForeignKey("talks.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    log_path: Mapped[str | None] = mapped_column(Text)
    progress_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=True,
    )

    talk: Mapped[Talk] = relationship(back_populates="jobs")

    @property
    def elapsed_time(self) -> float | None:
        """Calculate elapsed runtime in seconds.

        Uses `updated_at` as the end time for terminal statuses (`done`, `failed`, `broken`, `rejected`)
        when present, returning None if `updated_at` is missing for terminal jobs to prevent time drift.
        """
        if self.started_at is None:
            return None
        started = (
            self.started_at
            if self.started_at.tzinfo is not None
            else self.started_at.replace(tzinfo=UTC)
        )
        if self.status in ("done", "failed", "broken", "rejected", "cancelled"):
            if self.updated_at is None:
                return None
            end = (
                self.updated_at
                if self.updated_at.tzinfo is not None
                else self.updated_at.replace(tzinfo=UTC)
            )
        else:
            end = datetime.now(UTC)
        return max(0.0, round((end - started).total_seconds(), 2))

    @property
    def estimated_remaining(self) -> float | None:
        """Estimate remaining runtime in seconds based on elapsed time and progress percentage."""
        if self.status != "running":
            return (
                0.0
                if self.status == "done"
                and self.progress_pct is not None
                and self.progress_pct >= 100.0
                else None
            )
        if (
            self.elapsed_time is None
            or self.progress_pct is None
            or self.progress_pct <= 0.0
        ):
            return None
        if self.progress_pct >= 100.0:
            return 0.0
        pct_fraction = self.progress_pct / 100.0
        remaining = (self.elapsed_time / pct_fraction) * (1.0 - pct_fraction)
        return max(0.0, round(remaining, 2))


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    talk_id: Mapped[int] = mapped_column(ForeignKey("talks.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(50), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "users.id",
            name="fk_reviews_user_id_users",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    talk: Mapped[Talk] = relationship(back_populates="reviews")
    user: Mapped[User | None] = relationship(back_populates="reviews")
