import re
from datetime import UTC, datetime, time
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)


class EventBase(BaseModel):
    name: str


class EventCreate(EventBase):
    pass


class EventRead(EventBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class ClientBase(BaseModel):
    event_ids: list[int] = []


class ClientCreate(ClientBase):
    hashed_key: str


class ClientRead(ClientBase):
    id: int
    hashed_key: str
    model_config = ConfigDict(from_attributes=True)


class TalkBase(BaseModel):
    title: str
    room: str | None = None
    start: datetime
    end: datetime
    status: str = "waiting_for_files"


class TalkCreate(TalkBase):
    event_id: int


class TalkRead(TalkBase):
    id: int
    event_id: int
    preview_urls: list[str] = []
    raw_duration_seconds: float | None = None
    cut_start: float | None = None
    cut_end: float | None = None
    include_intro: bool = False
    include_outro: bool = False
    intro_source: str | None = None
    outro_source: str | None = None
    custom_intro_path: str | None = None
    custom_outro_path: str | None = None
    model_config = ConfigDict(from_attributes=True)

    @field_validator("include_intro", "include_outro", mode="before")
    @classmethod
    def _coerce_bool(cls, v):
        return False if v is None else bool(v)


class JobBase(BaseModel):
    kind: str
    status: str
    log_path: str | None = None
    progress_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    started_at: datetime | None = None
    updated_at: datetime | None = None


class JobCreate(JobBase):
    talk_id: int


class JobRead(JobBase):
    id: int
    talk_id: int
    model_config = ConfigDict(from_attributes=True)

    @computed_field
    @property
    def elapsed_time(self) -> float | None:
        """Computed elapsed runtime in seconds (now - started_at)."""
        if self.started_at is None:
            return None
        now = datetime.now(UTC)
        started = (
            self.started_at
            if self.started_at.tzinfo is not None
            else self.started_at.replace(tzinfo=UTC)
        )
        return max(0.0, round((now - started).total_seconds(), 2))

    @computed_field
    @property
    def estimated_remaining(self) -> float | None:
        """
        Linearly extrapolated remaining seconds based on progress_pct and elapsed_time.
        Returns None if progress_pct is None or <= 0.
        Returns 0.0 if progress_pct >= 100.0.
        """
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


class ReviewBase(BaseModel):
    decision: str
    note: str | None = None


class ReviewCreate(ReviewBase):
    talk_id: int


class ReviewRead(ReviewBase):
    id: int
    talk_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class ReviewDecision(str, Enum):
    approve = "approve"
    needs_work = "needs_work"
    reject = "reject"


class ReviewRequest(BaseModel):
    decision: ReviewDecision
    note: str | None = None


class ReviewResponse(BaseModel):
    talk: TalkRead
    review: ReviewRead | None = None
    model_config = ConfigDict(from_attributes=True)


class TalkWithJobsRead(TalkRead):
    jobs: list[JobRead] = []


class RecordingIngestRequest(BaseModel):
    source_path: str | None = None
    relative_key: str | None = None

    @model_validator(mode="after")
    def exactly_one_path(self):
        if bool(self.source_path) == bool(self.relative_key):
            raise ValueError("exactly one of source_path or relative_key is required")
        return self


class ApproveRequest(BaseModel):
    decision: Literal["approve", "reject"] = "approve"


HHMMSS_PATTERN = re.compile(r"^\d{2}:\d{2}:\d{2}(?:\.\d+)?$")


def _parse_hhmmss(value: str) -> float:
    """Parse HH:MM:SS (with optional subseconds) into seconds (float)."""
    if not isinstance(value, str) or not HHMMSS_PATTERN.match(value):
        raise ValueError(f"Invalid time format '{value}', expected HH:MM:SS")
    try:
        t = time.fromisoformat(value)
        return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1_000_000
    except ValueError as exc:
        raise ValueError(f"Invalid time format '{value}', expected HH:MM:SS") from exc


class CutBoundsRequest(BaseModel):
    cut_start: str  # "HH:MM:SS"
    cut_end: str  # "HH:MM:SS"

    @model_validator(mode="after")
    def parse_and_validate(self):
        start_s = _parse_hhmmss(self.cut_start)
        end_s = _parse_hhmmss(self.cut_end)
        if end_s <= start_s:
            raise ValueError("cut_end must be greater than cut_start")
        return self

    def parsed_seconds(self) -> tuple[float, float]:
        return _parse_hhmmss(self.cut_start), _parse_hhmmss(self.cut_end)


class IntroOutroRequest(BaseModel):
    include_intro: bool = False
    include_outro: bool = False
    intro_source: Literal["generated", "custom"] = "generated"
    outro_source: Literal["generated", "custom"] = "generated"
    custom_intro_path: str | None = None
    custom_outro_path: str | None = None

    @model_validator(mode="after")
    def validate_custom_paths(self):
        if (
            self.include_intro
            and self.intro_source == "custom"
            and (not self.custom_intro_path or not self.custom_intro_path.strip())
        ):
            raise ValueError(
                "custom_intro_path is required when intro_source is 'custom'"
            )
        if (
            self.include_outro
            and self.outro_source == "custom"
            and (not self.custom_outro_path or not self.custom_outro_path.strip())
        ):
            raise ValueError(
                "custom_outro_path is required when outro_source is 'custom'"
            )
        return self
