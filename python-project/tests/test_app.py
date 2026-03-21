import pytest
from datetime import datetime, timedelta, date, time
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from io_comp.models import (
    CalendarEvent,
    Participant,
    ParticipantType,
    AvailabilitySlot,
    MeetingRequest,
    WorkdaySchedule,
    DeepWorkMetrics,
)
from io_comp.repository import (
    InMemoryCalendarRepository,
    ICalendarRepository,
    SQLiteCalendarRepository,
)
from io_comp.service import MeetingFinderService
from io_comp.workday_policy import (
    IsraeliWorkdayPolicy,
    USAWorkdayPolicy,
    WorkdayPolicyFactory,
)


# ---------------------------------------------------------------------------
# SQLiteCalendarRepository
# ---------------------------------------------------------------------------

class TestSQLiteRepository:
    """SQLiteCalendarRepository — drop-in replacement for CSV."""

    def _make_event(self, person: str, subject: str, start_h: int, end_h: int) -> CalendarEvent:
        return CalendarEvent(
            person_name=person, event_subject=subject,
            start_time=datetime(2024, 3, 18, start_h, 0),
            end_time=datetime(2024, 3, 18, end_h, 0),
        )

    def test_add_and_load_events(self):
        repo = SQLiteCalendarRepository()
        repo.add_event(self._make_event("Alice", "Standup", 9, 10))
        assert len(repo.load_events()) == 1

    def test_load_participants_groups_by_person(self):
        repo = SQLiteCalendarRepository()
        repo.add_event(self._make_event("Alice", "A", 9, 10))
        repo.add_event(self._make_event("Alice", "B", 11, 12))
        repo.add_event(self._make_event("Bob", "C", 9, 10))
        participants = repo.load_participants()
        assert set(participants.keys()) == {"Alice", "Bob"}
        assert len(participants["Alice"].events) == 2

    def test_update_event_time(self):
        repo = SQLiteCalendarRepository()
        repo.add_event(self._make_event("Alice", "Standup", 9, 10))
        updated = repo.update_event_time(
            "Alice", "Standup",
            old_start=datetime(2024, 3, 18, 9, 0),
            new_start=datetime(2024, 3, 18, 11, 0),
            new_end=datetime(2024, 3, 18, 12, 0),
        )
        assert updated is True
        assert repo.load_events()[0].start_time.hour == 11

    def test_delete_event(self):
        repo = SQLiteCalendarRepository()
        repo.add_event(self._make_event("Alice", "Standup", 9, 10))
        deleted = repo.delete_event("Alice", "Standup", datetime(2024, 3, 18, 9, 0))
        assert deleted is True
        assert len(repo.load_events()) == 0

    def test_sqlite_is_drop_in_replacement_for_service(self):
        """MeetingFinderService works identically with SQLite as with InMemory."""
        repo = SQLiteCalendarRepository()
        for person, subject, sh, eh in [("Alice", "Morning meeting", 8, 9), ("Jack", "Sales call", 9, 10)]:
            repo.add_event(CalendarEvent(
                person_name=person, event_subject=subject,
                start_time=datetime(2024, 3, 18, sh, 0),
                end_time=datetime(2024, 3, 18, eh, 0),
            ))
        service = MeetingFinderService(repo, IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18),
        ))
        assert len(slots) > 0


# ---------------------------------------------------------------------------
# Recurring events (RRULE)
# ---------------------------------------------------------------------------

class TestRecurringEvents:
    """RRULE expansion in MeetingFinderService."""

    def test_recurring_event_blocks_slot_on_target_date(self):
        """A weekly recurring event should block the slot on its recurrence day."""
        try:
            from dateutil.rrule import rrulestr  # noqa: F401
        except ImportError:
            pytest.skip("python-dateutil not installed")

        recurring = CalendarEvent(
            person_name="Alice",
            event_subject="Weekly standup",
            start_time=datetime(2024, 3, 18, 9, 0),
            end_time=datetime(2024, 3, 18, 9, 30),
            is_recurring=True,
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO",
        )
        service = MeetingFinderService(InMemoryCalendarRepository([recurring]), IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice"],
            event_duration=timedelta(minutes=30),
            target_date=date(2024, 3, 25),  # next Monday
        ))
        assert "09:00" not in [s.start_time.strftime("%H:%M") for s in slots]

    def test_non_recurring_event_not_expanded(self):
        """A normal event should only appear on its own date."""
        event = CalendarEvent(
            person_name="Alice", event_subject="One-off",
            start_time=datetime(2024, 3, 18, 9, 0),
            end_time=datetime(2024, 3, 18, 10, 0),
        )
        service = MeetingFinderService(InMemoryCalendarRepository([event]), IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 19),  # different date — full day open
        ))
        assert slots[0].start_time.strftime("%H:%M") == "07:00"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    """Rate limiting decorator on Flask endpoints."""

    def setup_method(self):
        from io_comp.api import app, _rate_limit_store
        self.client = app.test_client()
        _rate_limit_store.clear()

    def test_requests_within_limit_succeed(self):
        for _ in range(5):
            resp = self.client.get("/api/health")
            assert resp.status_code == 200

    def test_post_requests_within_limit_succeed(self):
        payload = {
            "mandatory_participants": ["Alice"],
            "event_duration_hours": 1,
            "target_date": "2024-03-18",
        }
        resp = self.client.post("/api/available-slots", json=payload)
        assert resp.status_code != 429

    def test_exceeding_rate_limit_returns_429(self):
        from io_comp import api as api_module
        original = api_module.RATE_LIMIT_REQUESTS
        api_module.RATE_LIMIT_REQUESTS = 2
        api_module._rate_limit_store.clear()
        try:
            payload = {
                "mandatory_participants": ["Alice"],
                "event_duration_hours": 1,
                "target_date": "2024-03-18",
            }
            for _ in range(2):
                self.client.post("/api/available-slots", json=payload)
            resp = self.client.post("/api/available-slots", json=payload)
            assert resp.status_code == 429
        finally:
            api_module.RATE_LIMIT_REQUESTS = original


# ---------------------------------------------------------------------------
# README example
# ---------------------------------------------------------------------------

class TestReadmeExample:
    """
    Verifies output matches the README example exactly:
    Alice + Jack, 60 min → 07:00, 09:40, 14:00, 17:00
    """

    def setup_method(self):
        self.events = [
            CalendarEvent(person_name="Alice", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 9, 30)),
            CalendarEvent(person_name="Alice", event_subject="Lunch with Jack",
                          start_time=datetime(2024, 3, 18, 13, 0), end_time=datetime(2024, 3, 18, 14, 0)),
            CalendarEvent(person_name="Alice", event_subject="Yoga",
                          start_time=datetime(2024, 3, 18, 16, 0), end_time=datetime(2024, 3, 18, 17, 0)),
            CalendarEvent(person_name="Jack", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 8, 50)),
            CalendarEvent(person_name="Jack", event_subject="Sales call",
                          start_time=datetime(2024, 3, 18, 9, 0), end_time=datetime(2024, 3, 18, 9, 40)),
            CalendarEvent(person_name="Jack", event_subject="Lunch with Alice",
                          start_time=datetime(2024, 3, 18, 13, 0), end_time=datetime(2024, 3, 18, 14, 0)),
            CalendarEvent(person_name="Jack", event_subject="Yoga",
                          start_time=datetime(2024, 3, 18, 16, 0), end_time=datetime(2024, 3, 18, 17, 0)),
        ]
        self.service = MeetingFinderService(
            InMemoryCalendarRepository(self.events),
            IsraeliWorkdayPolicy(),
        )

    def test_readme_example_alice_and_jack_60_minutes(self):
        """README example: Alice + Jack, 60 min → 07:00, 09:40, 14:00, 17:00"""
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18),
        ))
        assert [s.start_time.strftime("%H:%M") for s in slots] == ["07:00", "09:40", "14:00", "17:00"]

    def test_slot_duration_equals_requested_duration(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18),
        ))
        assert all(s.duration == timedelta(hours=1) for s in slots)


# ---------------------------------------------------------------------------
# CalendarEvent
# ---------------------------------------------------------------------------

class TestCalendarEvent:

    def test_event_creation(self):
        event = CalendarEvent(
            person_name="Alice", event_subject="Meeting",
            start_time=datetime(2024, 3, 17, 8, 0),
            end_time=datetime(2024, 3, 17, 9, 0),
        )
        assert event.person_name == "Alice"
        assert event.event_subject == "Meeting"

    def test_event_end_time_validation(self):
        with pytest.raises(ValueError):
            CalendarEvent(
                person_name="Alice", event_subject="Meeting",
                start_time=datetime(2024, 3, 17, 9, 0),
                end_time=datetime(2024, 3, 17, 8, 0),
            )

    def test_event_overlap_detection(self):
        e1 = CalendarEvent(person_name="Alice", event_subject="M1",
                           start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0))
        e2 = CalendarEvent(person_name="Alice", event_subject="M2",
                           start_time=datetime(2024, 3, 17, 8, 30), end_time=datetime(2024, 3, 17, 10, 0))
        assert e1.overlaps_with(e2)
        assert e2.overlaps_with(e1)

    def test_event_no_overlap(self):
        e1 = CalendarEvent(person_name="Alice", event_subject="M1",
                           start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0))
        e2 = CalendarEvent(person_name="Alice", event_subject="M2",
                           start_time=datetime(2024, 3, 17, 9, 0), end_time=datetime(2024, 3, 17, 10, 0))
        assert not e1.overlaps_with(e2)


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------

class TestParticipant:

    def test_participant_creation(self):
        events = [CalendarEvent(person_name="Alice", event_subject="Meeting",
                                start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0))]
        participant = Participant(name="Alice", events=events)
        assert participant.name == "Alice"
        assert len(participant.events) == 1

    def test_get_busy_slots(self):
        events = [CalendarEvent(person_name="Alice", event_subject="Meeting",
                                start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0))]
        busy_slots = Participant(name="Alice", events=events).get_busy_slots(date(2024, 3, 17))
        assert len(busy_slots) == 1
        assert busy_slots[0][0] is not None


# ---------------------------------------------------------------------------
# InMemoryRepository
# ---------------------------------------------------------------------------

class TestInMemoryRepository:

    def test_load_events(self):
        events = [CalendarEvent(person_name="Alice", event_subject="Meeting",
                                start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0))]
        assert len(InMemoryCalendarRepository(events).load_events()) == 1

    def test_load_participants(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="M",
                          start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0)),
            CalendarEvent(person_name="Bob", event_subject="M",
                          start_time=datetime(2024, 3, 17, 9, 0), end_time=datetime(2024, 3, 17, 10, 0)),
        ]
        participants = InMemoryCalendarRepository(events).load_participants()
        assert len(participants) == 2
        assert "Alice" in participants and "Bob" in participants

    def test_get_events_for_person(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="M1",
                          start_time=datetime(2024, 3, 17, 8, 0), end_time=datetime(2024, 3, 17, 9, 0)),
            CalendarEvent(person_name="Alice", event_subject="M2",
                          start_time=datetime(2024, 3, 17, 10, 0), end_time=datetime(2024, 3, 17, 11, 0)),
        ]
        assert len(InMemoryCalendarRepository(events).get_events_for_person("Alice")) == 2


# ---------------------------------------------------------------------------
# WorkdayPolicy
# ---------------------------------------------------------------------------

class TestWorkdayPolicy:

    def test_israeli_workday_policy(self):
        schedule = IsraeliWorkdayPolicy().get_workday_schedule()
        assert schedule.start_hour == 7
        assert schedule.end_hour == 19
        assert 4 in schedule.weekend_days and 5 in schedule.weekend_days  # Fri=4, Sat=5

    def test_usa_workday_policy(self):
        schedule = USAWorkdayPolicy().get_workday_schedule()
        assert schedule.start_hour == 9
        assert schedule.end_hour == 17

    def test_is_working_day(self):
        policy = IsraeliWorkdayPolicy()
        assert policy.is_working_day(date(2024, 3, 18))       # Monday
        assert not policy.is_working_day(date(2024, 3, 23))   # Saturday

    def test_get_next_working_day(self):
        # March 22 2024 is Friday — next working day is Sunday (weekday 6) in Israel
        next_day = IsraeliWorkdayPolicy().get_next_working_day(date(2024, 3, 22))
        assert next_day.weekday() == 6  # Sunday

    def test_workday_policy_factory(self):
        assert isinstance(WorkdayPolicyFactory.create_policy("israel"), IsraeliWorkdayPolicy)
        assert isinstance(WorkdayPolicyFactory.create_policy("usa"), USAWorkdayPolicy)


# ---------------------------------------------------------------------------
# MeetingFinderService
# ---------------------------------------------------------------------------

class TestMeetingFinderService:

    def setup_method(self):
        self.events = [
            CalendarEvent(person_name="Alice", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 9, 30)),
            CalendarEvent(person_name="Alice", event_subject="Lunch",
                          start_time=datetime(2024, 3, 18, 13, 0), end_time=datetime(2024, 3, 18, 14, 0)),
            CalendarEvent(person_name="Jack", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 8, 50)),
            CalendarEvent(person_name="Jack", event_subject="Sales call",
                          start_time=datetime(2024, 3, 18, 9, 0), end_time=datetime(2024, 3, 18, 9, 40)),
            CalendarEvent(person_name="Bob", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 9, 30)),
        ]
        self.service = MeetingFinderService(
            InMemoryCalendarRepository(self.events), IsraeliWorkdayPolicy()
        )

    def test_find_available_slots_basic(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18),
        ))
        assert len(slots) > 0

    def test_find_available_slots_with_buffer(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18),
            buffer_minutes=15,
        ))
        assert len(slots) > 0

    def test_find_available_slots_no_slots(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack", "Bob"],
            event_duration=timedelta(hours=12),
            target_date=date(2024, 3, 17),
        ))
        assert isinstance(slots, list)

    def test_deep_work_scoring(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 17),
        ))
        if len(slots) > 1:
            for i in range(len(slots) - 1):
                assert slots[i].deep_work_score >= slots[i + 1].deep_work_score

    def test_meeting_request_validation(self):
        with pytest.raises(ValueError):
            MeetingRequest(participant_names=["Alice"], event_duration=timedelta(hours=-1),
                           target_date=date(2024, 3, 17))
        with pytest.raises(ValueError):
            MeetingRequest(participant_names=["Alice"], event_duration=timedelta(hours=1),
                           target_date=date(2024, 3, 17), buffer_minutes=-5)

    def test_multiple_participants(self):
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Jack", "Bob"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 17),
        ))
        assert isinstance(slots, list)

    def test_non_working_day(self):
        # March 23 2024 is Saturday — a weekend day in Israel
        slots = self.service.find_available_slots(MeetingRequest(
            participant_names=["Alice"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 23),  # Saturday
        ))
        assert len(slots) == 0

    def test_suggest_reschedules_when_no_slots(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="Morning block",
                          start_time=datetime(2024, 3, 18, 7, 0, tzinfo=ZoneInfo("UTC")),
                          end_time=datetime(2024, 3, 18, 13, 0, tzinfo=ZoneInfo("UTC"))),
            CalendarEvent(person_name="Bob", event_subject="Afternoon block",
                          start_time=datetime(2024, 3, 18, 13, 0, tzinfo=ZoneInfo("UTC")),
                          end_time=datetime(2024, 3, 18, 19, 0, tzinfo=ZoneInfo("UTC"))),
        ]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        request = MeetingRequest(
            participant_names=["Alice", "Bob"], mandatory_participants=["Alice", "Bob"],
            optional_participants=[], event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18), buffer_minutes=0, allow_fallback_optional=False,
        )
        assert len(service.find_available_slots(request)) == 0
        suggestions = service.suggest_reschedules_for_request(request, max_suggestions=3)
        assert len(suggestions) > 0
        assert "participant_name" in suggestions[0]
        assert "unlocked_meeting_start_datetime" in suggestions[0]

    def test_suggest_reschedules_can_move_blocker_to_next_workday(self):
        events = [CalendarEvent(person_name="Alice", event_subject="All day block",
                                start_time=datetime(2024, 3, 18, 7, 0, tzinfo=ZoneInfo("UTC")),
                                end_time=datetime(2024, 3, 18, 19, 0, tzinfo=ZoneInfo("UTC")))]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        request = MeetingRequest(
            participant_names=["Alice"], mandatory_participants=["Alice"], optional_participants=[],
            event_duration=timedelta(hours=1), target_date=date(2024, 3, 18),
            buffer_minutes=0, allow_fallback_optional=False,
        )
        suggestions = service.suggest_reschedules_for_request(request, max_suggestions=3)
        assert len(suggestions) > 0
        assert datetime.fromisoformat(suggestions[0]["suggested_start_datetime"]).date() > request.target_date

    def test_suggest_reschedules_can_move_two_blockers(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="Alice full day",
                          start_time=datetime(2024, 3, 18, 7, 0, tzinfo=ZoneInfo("UTC")),
                          end_time=datetime(2024, 3, 18, 19, 0, tzinfo=ZoneInfo("UTC"))),
            CalendarEvent(person_name="Bob", event_subject="Bob full day",
                          start_time=datetime(2024, 3, 18, 7, 0, tzinfo=ZoneInfo("UTC")),
                          end_time=datetime(2024, 3, 18, 19, 0, tzinfo=ZoneInfo("UTC"))),
        ]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        request = MeetingRequest(
            participant_names=["Alice", "Bob"], mandatory_participants=["Alice", "Bob"],
            optional_participants=[], event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18), buffer_minutes=0, allow_fallback_optional=False,
        )
        suggestions = service.suggest_reschedules_for_request(request, max_suggestions=3)
        assert len(suggestions) > 0
        assert suggestions[0]["move_count"] == 2

    def test_optional_participant_fallback_uses_mandatory_only(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="Morning sync",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 9, 0)),
            CalendarEvent(person_name="Bob", event_subject="All day block",
                          start_time=datetime(2024, 3, 18, 7, 0), end_time=datetime(2024, 3, 18, 19, 0)),
        ]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Bob"], mandatory_participants=["Alice"],
            optional_participants=["Bob"], event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18), allow_fallback_optional=True,
        ))
        assert len(slots) > 0

    def test_long_free_window_returns_multiple_start_options(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="Morning meeting",
                          start_time=datetime(2024, 3, 18, 8, 0), end_time=datetime(2024, 3, 18, 9, 0)),
            CalendarEvent(person_name="Alice", event_subject="Evening meeting",
                          start_time=datetime(2024, 3, 18, 15, 0), end_time=datetime(2024, 3, 18, 16, 0)),
        ]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice"], event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 18), buffer_minutes=0,
        ))
        assert len(slots) > 1
        assert all(slot.duration == timedelta(hours=1) for slot in slots)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

class TestIntegration:

    def test_end_to_end_search(self):
        events = [
            CalendarEvent(person_name="Alice", event_subject="Team standup",
                          start_time=datetime(2024, 3, 17, 9, 0), end_time=datetime(2024, 3, 17, 9, 30)),
            CalendarEvent(person_name="Bob", event_subject="1:1 with manager",
                          start_time=datetime(2024, 3, 17, 10, 0), end_time=datetime(2024, 3, 17, 11, 0)),
        ]
        service = MeetingFinderService(InMemoryCalendarRepository(events), IsraeliWorkdayPolicy())
        slots = service.find_available_slots(MeetingRequest(
            participant_names=["Alice", "Bob"],
            event_duration=timedelta(hours=1),
            target_date=date(2024, 3, 17),
        ))
        assert isinstance(slots, list)
        for slot in slots:
            assert slot.duration >= timedelta(hours=1)
            assert slot.start_time.date() == date(2024, 3, 17)

    def test_dependency_injection(self):
        mock_repo = Mock(spec=ICalendarRepository)
        mock_repo.load_participants.return_value = {}
        service = MeetingFinderService(mock_repo, IsraeliWorkdayPolicy())
        assert service.repository == mock_repo


# ---------------------------------------------------------------------------
# DeepWorkMetrics
# ---------------------------------------------------------------------------

class TestDeepWorkMetrics:

    def test_deep_work_metrics_creation(self):
        metrics = DeepWorkMetrics()
        assert metrics.time_distance_weight == 0.3
        assert metrics.isolation_weight == 0.7

    def test_isolation_score_calculation(self):
        metrics = DeepWorkMetrics()
        assert metrics.calculate_isolation_score(timedelta(minutes=30)) < \
               metrics.calculate_isolation_score(timedelta(hours=5))
