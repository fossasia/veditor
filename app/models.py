from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    talks: Mapped[list[Talk]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    hashed_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    event_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list)


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
        if self.status in ("done", "failed", "broken", "rejected"):
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
            return 0.0 if self.status == "done" and self.progress_pct is not None and self.progress_pct >= 100.0 else None
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

    talk: Mapped[Talk] = relationship(back_populates="reviews")
