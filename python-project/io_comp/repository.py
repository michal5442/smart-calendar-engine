"""Repository pattern for calendar data access.

ICalendarRepository defines the interface; concrete implementations
(CSV, SQLite, InMemory) can be swapped without touching the service layer.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from pathlib import Path
import csv
import sqlite3
from datetime import datetime, date

from .models import CalendarEvent, Participant, ParticipantType


class ICalendarRepository(ABC):
    """Abstract base class for all calendar data sources."""

    @abstractmethod
    def load_events(self) -> List[CalendarEvent]:
        """Return all events from the data source."""
        pass

    @abstractmethod
    def load_participants(self) -> Dict[str, Participant]:
        """Return a name → Participant mapping built from all events."""
        pass

    @abstractmethod
    def get_events_for_person(self, person_name: str) -> List[CalendarEvent]:
        """Return all events belonging to *person_name*."""
        pass


class CSVCalendarRepository(ICalendarRepository):
    """Reads calendar data from a CSV file.

    Expected format (no header row):
        Person name, Event subject, HH:MM start, HH:MM end[, YYYY-MM-DD start date, YYYY-MM-DD end date]

    When date columns are absent, today's date is used as the reference day
    so that time-only entries still produce valid datetime objects.
    Results are cached after the first read.
    """

    def __init__(self, csv_path: str, timezone: str = "UTC"):
        self.csv_path = Path(csv_path)
        self.timezone = timezone
        self._events_cache: Optional[List[CalendarEvent]] = None
        self._participants_cache: Optional[Dict[str, Participant]] = None

        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV file not found: {self.csv_path}")

    def _parse_time_string(self, time_str: str, reference_date: date) -> datetime:
        """Parse an HH:MM string into a datetime on *reference_date*."""
        time_obj = datetime.strptime(time_str.strip(), "%H:%M").time()
        return datetime.combine(reference_date, time_obj)

    def load_events(self, reference_date: Optional[date] = None) -> List[CalendarEvent]:
        if self._events_cache is not None:
            return self._events_cache

        if reference_date is None:
            reference_date = date.today()

        events = []
        try:
            with open(self.csv_path, "r", encoding="utf-8") as csvfile:
                for row in csv.reader(csvfile):
                    row = [col.strip() for col in row]
                    if len(row) < 4:
                        continue

                    person_name, event_subject = row[0], row[1]

                    # Use explicit date columns when present; fall back to reference_date
                    if len(row) >= 6 and row[4] and row[5]:
                        start_date = datetime.strptime(row[4], "%Y-%m-%d").date()
                        end_date = datetime.strptime(row[5], "%Y-%m-%d").date()
                    else:
                        start_date = end_date = reference_date

                    has_explicit_date = bool(len(row) >= 6 and row[4] and row[5])
                    events.append(CalendarEvent(
                        person_name=person_name,
                        event_subject=event_subject,
                        start_time=self._parse_time_string(row[2], start_date),
                        end_time=self._parse_time_string(row[3], end_date),
                        timezone=self.timezone,
                        has_explicit_date=has_explicit_date,
                    ))

            self._events_cache = events
            return events

        except (FileNotFoundError, IndexError, ValueError) as e:
            raise ValueError(f"Error reading CSV file: {e}")

    def load_participants(self) -> Dict[str, Participant]:
        if self._participants_cache is not None:
            return self._participants_cache

        participants: Dict[str, Participant] = {}
        for event in self.load_events():
            if event.person_name not in participants:
                participants[event.person_name] = Participant(
                    name=event.person_name,
                    participant_type=ParticipantType.MANDATORY,
                    events=[],
                )
            participants[event.person_name].events.append(event)

        self._participants_cache = participants
        return participants

    def get_events_for_person(self, person_name: str) -> List[CalendarEvent]:
        participants = self.load_participants()
        return participants[person_name].events if person_name in participants else []


class InMemoryCalendarRepository(ICalendarRepository):
    """In-memory repository used in unit tests.

    Accepts a pre-built list of CalendarEvent objects so tests never touch
    the filesystem.
    """

    def __init__(self, events: Optional[List[CalendarEvent]] = None):
        self.events = events or []
        self._participants_cache: Optional[Dict[str, Participant]] = None

    def load_events(self) -> List[CalendarEvent]:
        return self.events

    def load_participants(self) -> Dict[str, Participant]:
        if self._participants_cache is not None:
            return self._participants_cache

        participants: Dict[str, Participant] = {}
        for event in self.events:
            if event.person_name not in participants:
                participants[event.person_name] = Participant(
                    name=event.person_name,
                    participant_type=ParticipantType.MANDATORY,
                    events=[],
                )
            participants[event.person_name].events.append(event)

        self._participants_cache = participants
        return participants

    def get_events_for_person(self, person_name: str) -> List[CalendarEvent]:
        return [e for e in self.events if e.person_name == person_name]

    def add_event(self, event: CalendarEvent) -> None:
        self.events.append(event)
        self._participants_cache = None  # invalidate so next load_participants rebuilds

    def invalidate_cache(self) -> None:
        """Force a cache rebuild on the next call to load_participants.

        Call this after mutating participant.events directly (e.g. in the
        reschedule simulation inside MeetingFinderService).
        """
        self._participants_cache = None


class SQLiteCalendarRepository(ICalendarRepository):
    """SQLite-backed implementation of ICalendarRepository.

    Drop-in replacement for CSVCalendarRepository — identical interface,
    persistent storage. Demonstrates that the Repository Pattern lets you
    swap the data source without touching the service layer.

    Schema:
        events(id, person_name, event_subject, start_time, end_time,
               timezone, is_recurring, recurrence_rule)
    """

    def __init__(self, db_path: str = ":memory:", seed_from_csv: Optional[str] = None):
        """
        Args:
            db_path: Path to the SQLite file, or ":memory:" for an in-process DB.
            seed_from_csv: Optional CSV path to import on first run (skipped if
                           the table already contains rows).
        """
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()
        if seed_from_csv:
            self._seed_from_csv(seed_from_csv)

    # ------------------------------------------------------------------
    # Schema & seeding
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                person_name      TEXT    NOT NULL,
                event_subject    TEXT    NOT NULL,
                start_time       TEXT    NOT NULL,
                end_time         TEXT    NOT NULL,
                timezone         TEXT    NOT NULL DEFAULT 'UTC',
                is_recurring     INTEGER NOT NULL DEFAULT 0,
                recurrence_rule  TEXT
            )
        """)
        self._conn.commit()

    def _seed_from_csv(self, csv_path: str) -> None:
        """Import events from a CSV file, skipping if the table already has rows."""
        if self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0:
            return
        for event in CSVCalendarRepository(csv_path).load_events():
            self._insert_event(event)

    def _insert_event(self, event: CalendarEvent) -> None:
        self._conn.execute(
            """
            INSERT INTO events
                (person_name, event_subject, start_time, end_time,
                 timezone, is_recurring, recurrence_rule)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.person_name,
                event.event_subject,
                event.start_time.isoformat(),
                event.end_time.isoformat(),
                event.timezone,
                int(event.is_recurring),
                event.recurrence_rule,
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # ICalendarRepository interface
    # ------------------------------------------------------------------

    def load_events(self) -> List[CalendarEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY person_name, start_time"
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def load_participants(self) -> Dict[str, Participant]:
        participants: Dict[str, Participant] = {}
        for event in self.load_events():
            if event.person_name not in participants:
                participants[event.person_name] = Participant(
                    name=event.person_name,
                    participant_type=ParticipantType.MANDATORY,
                    events=[],
                )
            participants[event.person_name].events.append(event)
        return participants

    def get_events_for_person(self, person_name: str) -> List[CalendarEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE person_name = ? ORDER BY start_time",
            (person_name,),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # Write operations (extension beyond the base interface)
    # ------------------------------------------------------------------

    def add_event(self, event: CalendarEvent) -> None:
        """Persist a new event to the database."""
        self._insert_event(event)

    def delete_event(self, person_name: str, event_subject: str, start_time: datetime) -> bool:
        """Delete a specific event. Returns True if a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM events WHERE person_name=? AND event_subject=? AND start_time=?",
            (person_name, event_subject, start_time.isoformat()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def update_event_time(
        self,
        person_name: str,
        event_subject: str,
        old_start: datetime,
        new_start: datetime,
        new_end: datetime,
    ) -> bool:
        """Reschedule an event to a new time. Returns True if a row was updated."""
        cursor = self._conn.execute(
            """
            UPDATE events
               SET start_time = ?, end_time = ?
             WHERE person_name = ? AND event_subject = ? AND start_time = ?
            """,
            (
                new_start.isoformat(),
                new_end.isoformat(),
                person_name,
                event_subject,
                old_start.isoformat(),
            ),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> CalendarEvent:
        return CalendarEvent(
            person_name=row["person_name"],
            event_subject=row["event_subject"],
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]),
            timezone=row["timezone"],
            is_recurring=bool(row["is_recurring"]),
            recurrence_rule=row["recurrence_rule"],
        )
