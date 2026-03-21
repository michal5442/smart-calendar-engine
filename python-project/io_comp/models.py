"""Core data models for the Smart Calendar Engine."""

from datetime import datetime, timedelta, time, date
from typing import List, Optional, Dict, Set
from pydantic import BaseModel, Field, field_validator, ConfigDict
from enum import Enum
from datetime import timezone as _tz


class ParticipantType(str, Enum):
    """Whether a participant must attend or is optional."""
    MANDATORY = "mandatory"
    OPTIONAL = "optional"


class CalendarEvent(BaseModel):
    """A single calendar event belonging to one person."""

    model_config = ConfigDict(use_enum_values=True)

    person_name: str = Field(..., description="Owner of the event")
    event_subject: str = Field(..., description="Title of the event")
    start_time: datetime = Field(..., description="Event start (naive = UTC)")
    end_time: datetime = Field(..., description="Event end (naive = UTC)")
    timezone: str = Field(default="UTC", description="Timezone the event was created in")
    is_recurring: bool = Field(default=False, description="Whether this event repeats")
    recurrence_rule: Optional[str] = Field(default=None, description="RRULE string, e.g. FREQ=WEEKLY;BYDAY=MO")
    has_explicit_date: bool = Field(default=True, description="False when the CSV row had no date columns — event applies to every working day")

    @field_validator("end_time")
    @classmethod
    def end_time_after_start(cls, v: datetime, info) -> datetime:
        if "start_time" in info.data and v <= info.data["start_time"]:
            raise ValueError("end_time must be after start_time")
        return v

    def normalize_to_utc(self) -> "CalendarEvent":
        """Return a copy of this event with both times converted to UTC-aware datetimes.

        Naive datetimes are treated as UTC directly (no local-time shift),
        which keeps them consistent with the UTC-aware slot times produced by the service.
        """
        utc = _tz.utc
        start_utc = self.start_time.replace(tzinfo=utc) if self.start_time.tzinfo is None \
            else self.start_time.astimezone(utc)
        end_utc = self.end_time.replace(tzinfo=utc) if self.end_time.tzinfo is None \
            else self.end_time.astimezone(utc)
        return CalendarEvent(
            person_name=self.person_name,
            event_subject=self.event_subject,
            start_time=start_utc,
            end_time=end_utc,
            timezone="UTC",
            is_recurring=self.is_recurring,
            recurrence_rule=self.recurrence_rule,
            has_explicit_date=self.has_explicit_date,
        )

    def remap_to_date(self, target_date: "date") -> "CalendarEvent":
        """Return a copy of this event remapped to *target_date* (same times, different day)."""
        from datetime import datetime as _datetime
        new_start = _datetime.combine(target_date, self.start_time.time(), tzinfo=self.start_time.tzinfo)
        new_end = _datetime.combine(target_date, self.end_time.time(), tzinfo=self.end_time.tzinfo)
        return CalendarEvent(
            person_name=self.person_name,
            event_subject=self.event_subject,
            start_time=new_start,
            end_time=new_end,
            timezone=self.timezone,
            is_recurring=self.is_recurring,
            recurrence_rule=self.recurrence_rule,
            has_explicit_date=self.has_explicit_date,
        )

    def overlaps_with(self, other: "CalendarEvent") -> bool:
        """Return True if this event and *other* share any time."""
        return not (self.end_time <= other.start_time or self.start_time >= other.end_time)


class Participant(BaseModel):
    """A person with a list of calendar events."""

    name: str = Field(..., description="Person's name")
    participant_type: ParticipantType = Field(
        default=ParticipantType.MANDATORY,
        description="Whether attendance is required or optional",
    )
    events: List[CalendarEvent] = Field(default_factory=list)

    def get_busy_slots(self, date_target: date, timezone: str = "UTC") -> List[tuple]:
        """Return sorted (start_time, end_time) tuples for all events on *date_target*."""
        busy = []
        for event in self.events:
            normalized = event.normalize_to_utc()
            if normalized.start_time.date() == date_target:
                busy.append((normalized.start_time.time(), normalized.end_time.time()))
        return sorted(busy)


class AvailabilitySlot(BaseModel):
    """A free time window that is long enough to host the requested meeting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    start_time: datetime = Field(..., description="Start of the free window")
    end_time: datetime = Field(..., description="End of the meeting (start + event_duration)")
    duration: timedelta = Field(..., description="Requested meeting duration")
    deep_work_score: float = Field(
        default=0.0,
        description="Isolation score 0–1: higher = further from other events",
    )
    available_participants: Set[str] = Field(default_factory=set)

    def fits_event(self, duration: timedelta) -> bool:
        """Return True if this slot is long enough for *duration*."""
        return self.duration >= duration


class MeetingRequest(BaseModel):
    """Input to MeetingFinderService.find_available_slots."""

    # participant_names is derived from mandatory + optional if not supplied directly
    participant_names: List[str] = Field(default_factory=list)
    mandatory_participants: List[str] = Field(default_factory=list)
    optional_participants: List[str] = Field(default_factory=list)
    event_duration: timedelta = Field(..., description="Desired meeting length")
    target_date: date = Field(..., description="Day to search")
    buffer_minutes: int = Field(default=0, description="Gap to leave before and after each event")
    timezone: str = Field(default="UTC")
    mandatory_only: bool = Field(default=False, description="Ignore optional participants entirely")
    allow_fallback_optional: bool = Field(
        default=True,
        description="If no slots found for all, retry with mandatory participants only",
    )

    @field_validator("participant_names", mode="before")
    @classmethod
    def participant_names_from_split_lists(cls, v, info) -> List[str]:
        """Merge mandatory + optional into participant_names for backward compatibility."""
        if v:
            return v
        mandatory = info.data.get("mandatory_participants", [])
        optional = info.data.get("optional_participants", [])
        seen: set = set()
        unique = []
        for name in mandatory + optional:
            if name not in seen:
                seen.add(name)
                unique.append(name)
        return unique

    @field_validator("event_duration")
    @classmethod
    def event_duration_positive(cls, v: timedelta) -> timedelta:
        if v <= timedelta(0):
            raise ValueError("event_duration must be positive")
        return v

    @field_validator("buffer_minutes")
    @classmethod
    def buffer_minutes_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("buffer_minutes must be non-negative")
        return v


class WorkdaySchedule(BaseModel):
    """Defines the working hours and weekend days for a region."""

    start_hour: int = Field(default=7)
    end_hour: int = Field(default=19)
    # weekday() values: 0=Monday … 6=Sunday
    weekend_days: Set[int] = Field(default_factory=lambda: {5, 6})

    @field_validator("start_hour", "end_hour")
    @classmethod
    def valid_hours(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("Hour must be between 0 and 23")
        return v

    @field_validator("end_hour", mode="after")
    @classmethod
    def end_after_start(cls, v: int, info) -> int:
        if "start_hour" in info.data and v <= info.data["start_hour"]:
            raise ValueError("end_hour must be after start_hour")
        return v

    def get_workday_start_time(self, target_date: date) -> datetime:
        return datetime.combine(target_date, time(hour=self.start_hour))

    def get_workday_end_time(self, target_date: date) -> datetime:
        return datetime.combine(target_date, time(hour=self.end_hour))

    def is_weekend(self, target_date: date) -> bool:
        return target_date.weekday() in self.weekend_days


class DeepWorkMetrics(BaseModel):
    """Weights used to score how well a slot preserves uninterrupted focus time.

    A slot that sits far from any existing event scores close to 1.0 (good for
    deep work). A slot immediately adjacent to another meeting scores near 0.0.
    """

    time_distance_weight: float = Field(default=0.3)
    isolation_weight: float = Field(default=0.7)

    def calculate_isolation_score(self, nearest_event_distance: timedelta) -> float:
        """Map *nearest_event_distance* to a 0–1 score.

        Uses a linear scale capped at 480 minutes (8 hours = full isolation).
        """
        distance_minutes = nearest_event_distance.total_seconds() / 60
        max_distance = 480  # minutes
        return min(1.0, distance_minutes / max_distance)
