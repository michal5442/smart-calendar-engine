"""Business logic layer — MeetingFinderService.

Responsible for finding free time slots across multiple participants.
Depends on ICalendarRepository (data) and IWorkdayPolicy (rules) via
constructor injection, making it fully testable without touching the filesystem.
"""

from typing import List, Dict, Set, Tuple, Optional, Any
from datetime import datetime, timedelta, date, time
from itertools import product
from datetime import timezone as _tz
import logging

try:
    from dateutil.rrule import rrulestr
    _RRULE_AVAILABLE = True
except ImportError:
    _RRULE_AVAILABLE = False  # graceful degradation: recurring events treated as single-occurrence

from .models import (
    CalendarEvent,
    AvailabilitySlot,
    MeetingRequest,
    Participant,
    ParticipantType,
    DeepWorkMetrics,
)
from .repository import ICalendarRepository
from .workday_policy import IWorkdayPolicy


logger = logging.getLogger(__name__)


class MeetingFinderService:
    """Core scheduling engine.

    Algorithm (find_available_slots):
        1. Load participants from the repository.
        2. Expand recurring events (RRULE) for the target date.
        3. Normalize all event times to UTC.
        4. Merge busy intervals across all participants into a single timeline.
        5. Walk the gaps between busy intervals within workday boundaries.
        6. Score each free window for deep-work isolation.
        7. Return slots sorted chronologically.
    """

    def __init__(
        self,
        calendar_repository: ICalendarRepository,
        workday_policy: IWorkdayPolicy,
        deep_work_metrics: Optional[DeepWorkMetrics] = None,
    ):
        self.repository = calendar_repository
        self.workday_policy = workday_policy
        self.deep_work_metrics = deep_work_metrics or DeepWorkMetrics()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_available_slots(self, request: MeetingRequest) -> List[AvailabilitySlot]:
        """Return free slots for *request*, sorted chronologically.

        If no slots are found and optional participants are present,
        retries with mandatory participants only (fallback behaviour).
        """
        mandatory_names = request.mandatory_participants or request.participant_names
        optional_names = request.optional_participants

        # When the caller did not split mandatory/optional, treat everyone as mandatory
        if not request.mandatory_participants and not request.optional_participants:
            optional_names = []

        requested_names = mandatory_names if request.mandatory_only else mandatory_names + optional_names

        logger.info("Searching slots for mandatory=%s optional=%s", mandatory_names, optional_names)

        if not requested_names:
            logger.warning("No participants requested")
            return []

        if not self.workday_policy.is_working_day(request.target_date):
            logger.warning("%s is not a working day", request.target_date)
            return []

        all_participants = self.repository.load_participants()
        participant_objects = self._get_participants(requested_names, all_participants)

        if not participant_objects:
            logger.warning("No valid participants found")
            return []

        busy_slots = self._get_all_busy_slots(participant_objects, request.target_date, request.timezone)
        available_slots = self._find_free_slots(busy_slots, request.event_duration, request.buffer_minutes, request.target_date, request.timezone)
        available_slots = self._score_slots_for_deep_work(available_slots, busy_slots)
        available_slots = sorted(available_slots, key=lambda s: s.start_time)

        logger.info("Found %d available slots", len(available_slots))

        # Fallback: retry without optional participants
        if not available_slots and request.allow_fallback_optional and optional_names and not request.mandatory_only:
            logger.info("No slots found — retrying with mandatory participants only")
            mandatory_objects = self._get_participants(mandatory_names, all_participants)
            if not mandatory_objects:
                return []
            mandatory_busy = self._get_all_busy_slots(mandatory_objects, request.target_date, request.timezone)
            fallback = self._find_free_slots(mandatory_busy, request.event_duration, request.buffer_minutes, request.target_date, request.timezone)
            fallback = self._score_slots_for_deep_work(fallback, mandatory_busy)
            fallback = sorted(fallback, key=lambda s: s.start_time)
            logger.info("Found %d slots with mandatory-only fallback", len(fallback))
            return fallback

        return available_slots

    def find_available_slots_multi_day(
        self,
        person_list: List[str],
        event_duration: timedelta,
        start_date: date,
        end_date: date,
        buffer_minutes: int = 0,
        timezone: str = "UTC",
    ) -> List[AvailabilitySlot]:
        """Run find_available_slots for every working day in [start_date, end_date]."""
        all_slots: List[AvailabilitySlot] = []
        current_date = start_date
        while current_date <= end_date:
            if self.workday_policy.is_working_day(current_date):
                slots = self.find_available_slots(MeetingRequest(
                    participant_names=person_list,
                    event_duration=event_duration,
                    target_date=current_date,
                    buffer_minutes=buffer_minutes,
                    timezone=timezone,
                ))
                all_slots.extend(slots)
            current_date += timedelta(days=1)
        return all_slots

    def suggest_reschedules_for_request(
        self,
        request: MeetingRequest,
        max_suggestions: int = 5,
    ) -> List[Dict[str, Any]]:
        """Propose moving existing events to free up a slot for the new meeting.

        For each event on the target day, simulates removing it and checks
        whether that creates a valid slot. If so, finds an alternative time
        for the removed event and returns the suggestion.

        Falls back to _suggest_multi_move_reschedules when a single move
        is not enough (e.g. two participants each block the only window).
        """
        mandatory_names = request.mandatory_participants or request.participant_names
        optional_names = [] if (not request.mandatory_participants and not request.optional_participants) \
            else request.optional_participants
        requested_names = mandatory_names if request.mandatory_only else mandatory_names + optional_names

        if not requested_names or not self.workday_policy.is_working_day(request.target_date):
            return []

        # No need to suggest moves if slots already exist
        if self.find_available_slots(request):
            return []

        participants_dict = self.repository.load_participants()
        selected_participants = self._get_participants(requested_names, participants_dict)
        if not selected_participants:
            return []

        suggestions: List[Dict[str, Any]] = []
        seen_keys: Set = set()
        candidate_move_dates = self._get_candidate_reschedule_dates(request.target_date)

        for participant in selected_participants:
            logger.info(f'Processing participant: {participant.name}, events: {len(participant.events)}')            # Include date-less events (has_explicit_date=False) — they apply to every working day,
            # so remap them to request.target_date for the purpose of finding moves.
            day_events = []
            for e in participant.events:
                if not e.has_explicit_date:
                    day_events.append(e.remap_to_date(request.target_date))
                elif e.start_time.date() == request.target_date:
                    day_events.append(e)

            for event in day_events:
                original_events = participant.events
                # Temporarily remove the event to test whether it unblocks a slot.
                # For date-less events, match by subject+time rather than object identity.
                if event.has_explicit_date:
                    events_without = [e for e in original_events if e is not event]
                else:
                    events_without = [
                        e for e in original_events
                        if not (not e.has_explicit_date and e.event_subject == event.event_subject
                                and e.start_time.time() == event.start_time.time()
                                and e.end_time.time() == event.end_time.time())
                    ]
                participant.events = events_without

                try:
                    found = False
                    for candidate_date in candidate_move_dates:
                        move_options = self._find_free_slots(
                            self._get_all_busy_slots([participant], candidate_date, request.timezone),
                            event.end_time - event.start_time,
                            request.buffer_minutes,
                            candidate_date,
                            request.timezone,
                        )
                        move_options = sorted(move_options, key=lambda s: s.start_time)

                        # Skip the original slot when searching the same day
                        if candidate_date == event.start_time.date():
                            move_options = [s for s in move_options
                                            if not (s.start_time == event.start_time and s.end_time == event.end_time)]

                        for move_slot in move_options[:10]:
                            # Simulate the move and check whether the new meeting fits
                            participant.events = events_without + [
                                CalendarEvent(
                                    person_name=event.person_name,
                                    event_subject=event.event_subject,
                                    start_time=move_slot.start_time,
                                    end_time=move_slot.end_time,
                                    timezone=event.timezone,
                                    is_recurring=event.is_recurring,
                                    recurrence_rule=event.recurrence_rule,
                                    has_explicit_date=True,
                                )
                            ]
                            unlocked = self._find_free_slots(
                                self._get_all_busy_slots(selected_participants, request.target_date, request.timezone),
                                request.event_duration,
                                request.buffer_minutes,
                                request.target_date,
                                request.timezone,
                            )
                            unlocked = self._score_slots_for_deep_work(unlocked, self._get_all_busy_slots(
                                selected_participants, request.target_date, request.timezone))
                            unlocked = sorted(unlocked, key=lambda s: s.deep_work_score, reverse=True)

                            if not unlocked:
                                continue

                            key = (participant.name, event.event_subject,
                                   event.start_time.isoformat(), event.end_time.isoformat(),
                                   move_slot.start_time.isoformat(), move_slot.end_time.isoformat())
                            if key in seen_keys:
                                continue

                            seen_keys.add(key)
                            suggestions.append(self._build_reschedule_suggestion(
                                [{"participant_name": participant.name,
                                  "event_subject": event.event_subject,
                                  "original_start_datetime": event.start_time.isoformat(),
                                  "original_end_datetime": event.end_time.isoformat(),
                                  "suggested_start_datetime": move_slot.start_time.isoformat(),
                                  "suggested_end_datetime": move_slot.end_time.isoformat()}],
                                unlocked[0],
                            ))

                            if len(suggestions) >= max_suggestions:
                                return suggestions
                            found = True
                            break
                        if found:
                            break
                finally:
                    participant.events = original_events

        if suggestions:
            return suggestions

        # Single-move was not enough — try moving two events simultaneously
        return self._suggest_multi_move_reschedules(selected_participants, request, candidate_move_dates, max_suggestions, seen_keys)

    def get_mandatory_participants(self, person_list: List[str]) -> List[str]:
        """Filter *person_list* to those marked as MANDATORY in the repository."""
        participants = self.repository.load_participants()
        return [n for n in person_list
                if n in participants and participants[n].participant_type == ParticipantType.MANDATORY]

    def get_optional_participants(self, person_list: List[str]) -> List[str]:
        """Filter *person_list* to those marked as OPTIONAL in the repository."""
        participants = self.repository.load_participants()
        return [n for n in person_list
                if n in participants and participants[n].participant_type == ParticipantType.OPTIONAL]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_participants(
        self,
        participant_names: List[str],
        participants: Dict[str, Participant],
        mandatory_only: bool = False,
    ) -> List[Participant]:
        result = []
        for name in participant_names:
            if name not in participants:
                logger.warning("Participant not found: %s", name)
                continue
            p = participants[name]
            if mandatory_only and p.participant_type != ParticipantType.MANDATORY:
                continue
            result.append(p)
        return result

    def _expand_recurring_events(self, events: List[CalendarEvent], target_date: date) -> List[CalendarEvent]:
        """Expand RRULE recurring events into concrete occurrences on *target_date*.

        Non-recurring events pass through unchanged.
        If python-dateutil is not installed, all events pass through unchanged
        (graceful degradation — no crash, no silent data loss).
        """
        if not _RRULE_AVAILABLE:
            return events

        expanded: List[CalendarEvent] = []
        day_start = datetime.combine(target_date, time.min)
        day_end = datetime.combine(target_date, time.max)

        for event in events:
            if not event.is_recurring or not event.recurrence_rule:
                expanded.append(event)
                continue
            try:
                rule = rrulestr(event.recurrence_rule, dtstart=event.start_time)
                duration = event.end_time - event.start_time
                for occurrence_start in rule.between(day_start, day_end, inc=True):
                    expanded.append(CalendarEvent(
                        person_name=event.person_name,
                        event_subject=event.event_subject,
                        start_time=occurrence_start,
                        end_time=occurrence_start + duration,
                        timezone=event.timezone,
                        is_recurring=True,
                        recurrence_rule=event.recurrence_rule,
                    ))
            except Exception:
                logger.warning("Failed to expand RRULE for %s: %s", event.event_subject, event.recurrence_rule)
                expanded.append(event)  # fall back to the original event

        return expanded

    def _get_all_busy_slots(
        self,
        participants: List[Participant],
        target_date: date,
        timezone: str,
    ) -> List[Tuple[datetime, datetime]]:
        """Collect and merge every participant's busy intervals on *target_date*.

        Returns a sorted, non-overlapping list of (start, end) UTC datetimes
        representing the combined busy time across all participants.
        """
        all_busy: List[Tuple[datetime, datetime]] = []
        for participant in participants:
            events_on_day = self._expand_recurring_events(participant.events, target_date)
            for event in events_on_day:
                # Date-less CSV events (has_explicit_date=False) apply to every working day
                if not event.has_explicit_date:
                    event = event.remap_to_date(target_date)
                normalized = event.normalize_to_utc()
                if normalized.start_time.date() == target_date:
                    all_busy.append((normalized.start_time, normalized.end_time))

        return self._merge_intervals(sorted(all_busy, key=lambda x: x[0]))

    def _merge_intervals(self, intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
        """Merge overlapping (start, end) intervals into a minimal non-overlapping list."""
        if not intervals:
            return []

        merged = []
        current_start, current_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end:
                # Overlapping — extend the current interval
                current_end = max(current_end, end)
            else:
                merged.append((current_start, current_end))
                current_start, current_end = start, end
        merged.append((current_start, current_end))
        return merged

    def _find_free_slots(
        self,
        busy_slots: List[Tuple[datetime, datetime]],
        event_duration: timedelta,
        buffer_minutes: int,
        target_date: date,
        timezone: str,
    ) -> List[AvailabilitySlot]:
        """Return one AvailabilitySlot per free window that fits *event_duration*.

        Each slot's start_time is the earliest possible start within the window.
        The slot's end_time is start_time + event_duration (not the window end),
        so duration always equals the requested meeting length.
        """
        buffer = timedelta(minutes=buffer_minutes)
        schedule = self.workday_policy.get_workday_schedule()
        workday_start = datetime.combine(target_date, time(hour=schedule.start_hour), tzinfo=_tz.utc)
        workday_end = datetime.combine(target_date, time(hour=schedule.end_hour), tzinfo=_tz.utc)

        free_slots: List[AvailabilitySlot] = []

        def add_if_fits(window_start: datetime, window_end: datetime) -> None:
            """Append a slot for *window_start* if the window is long enough."""
            if window_end - window_start >= event_duration:
                free_slots.append(AvailabilitySlot(
                    start_time=window_start,
                    end_time=window_start + event_duration,
                    duration=event_duration,
                ))

        if not busy_slots:
            # Entire workday is free
            add_if_fits(workday_start, workday_end)
        else:
            # Gap before the first busy interval
            if workday_start + event_duration + buffer <= busy_slots[0][0]:
                add_if_fits(workday_start, busy_slots[0][0] - buffer)

            # Gaps between consecutive busy intervals
            for i in range(len(busy_slots) - 1):
                gap_start = busy_slots[i][1] + buffer
                gap_end = busy_slots[i + 1][0] - buffer
                add_if_fits(gap_start, gap_end)

            # Gap after the last busy interval
            last_end = busy_slots[-1][1]
            if last_end + buffer + event_duration <= workday_end:
                add_if_fits(last_end + buffer, workday_end)

        return free_slots

    def _score_slots_for_deep_work(
        self,
        slots: List[AvailabilitySlot],
        busy_slots: List[Tuple[datetime, datetime]],
    ) -> List[AvailabilitySlot]:
        """Assign a deep-work isolation score to each slot.

        The score reflects how far the slot sits from the nearest busy interval.
        Slots surrounded by meetings score near 0; isolated slots score near 1.
        """
        for slot in slots:
            min_distance = timedelta(hours=24)  # default: assume fully isolated
            for busy_start, busy_end in busy_slots:
                if busy_start > slot.end_time:
                    min_distance = min(min_distance, busy_start - slot.end_time)
                elif busy_end < slot.start_time:
                    min_distance = min(min_distance, slot.start_time - busy_end)
            slot.deep_work_score = self.deep_work_metrics.calculate_isolation_score(min_distance)
        return slots

    def _build_reschedule_suggestion(
        self,
        moves: List[Dict[str, str]],
        unlocked_slot: AvailabilitySlot,
    ) -> Dict[str, Any]:
        """Build a serialisable reschedule suggestion dict for the API layer."""
        primary = moves[0]
        return {
            "participant_name": primary["participant_name"],
            "event_subject": primary["event_subject"],
            "original_start_datetime": primary["original_start_datetime"],
            "original_end_datetime": primary["original_end_datetime"],
            "suggested_start_datetime": primary["suggested_start_datetime"],
            "suggested_end_datetime": primary["suggested_end_datetime"],
            "moves": moves,
            "move_count": len(moves),
            "unlocked_meeting_start_datetime": unlocked_slot.start_time.isoformat(),
            "unlocked_meeting_end_datetime": unlocked_slot.end_time.isoformat(),
            "unlocked_meeting_deep_work_score": round(unlocked_slot.deep_work_score, 2),
        }

    def _get_unlocked_slots_after_reschedule(self, participants: List[Participant], request: MeetingRequest) -> List[AvailabilitySlot]:
        """Recalculate available slots after a simulated set of event moves."""
        busy = self._get_all_busy_slots(participants, request.target_date, request.timezone)
        slots = self._find_free_slots(busy, request.event_duration, request.buffer_minutes, request.target_date, request.timezone)
        slots = self._score_slots_for_deep_work(slots, busy)
        return sorted(slots, key=lambda s: s.deep_work_score, reverse=True)

    def _get_move_options_for_event(
        self,
        participant: Participant,
        event: CalendarEvent,
        request: MeetingRequest,
        candidate_move_dates: List[date],
        max_options_per_day: int = 5,
        max_total_options: int = 10,
    ) -> List[AvailabilitySlot]:
        """Find alternative slots for *event* across the candidate working days."""
        original_events = participant.events
        participant.events = [e for e in original_events if e is not event]
        try:
            move_options: List[AvailabilitySlot] = []
            for candidate_date in candidate_move_dates:
                options = self._find_free_slots(
                    self._get_all_busy_slots([participant], candidate_date, request.timezone),
                    event.end_time - event.start_time,
                    request.buffer_minutes,
                    candidate_date,
                    request.timezone,
                )
                options = sorted(options, key=lambda s: s.start_time)
                if candidate_date == event.start_time.date():
                    options = [s for s in options
                                if not (s.start_time == event.start_time and s.end_time == event.end_time)]
                move_options.extend(options[:max_options_per_day])
                if len(move_options) >= max_total_options:
                    break
            return move_options[:max_total_options]
        finally:
            participant.events = original_events

    def _get_blocking_events_for_window(
        self,
        participants: List[Participant],
        window_start: datetime,
        window_end: datetime,
        target_date: date,
        buffer_minutes: int,
    ) -> List[Tuple[Participant, CalendarEvent]]:
        """Return all (participant, event) pairs that overlap the candidate window."""
        blockers: List[Tuple[Participant, CalendarEvent]] = []
        seen: Set = set()
        buffer = timedelta(minutes=buffer_minutes)
        padded_start = window_start - buffer
        padded_end = window_end + buffer

        for participant in participants:
            for event in participant.events:
                normalized = event.normalize_to_utc()
                if normalized.start_time.date() != target_date:
                    continue
                if normalized.end_time <= padded_start or normalized.start_time >= padded_end:
                    continue
                key = (participant.name, event.event_subject, event.start_time.isoformat(), event.end_time.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                blockers.append((participant, event))
        return blockers

    def _suggest_multi_move_reschedules(
        self,
        selected_participants: List[Participant],
        request: MeetingRequest,
        candidate_move_dates: List[date],
        max_suggestions: int,
        seen_keys: Set,
    ) -> List[Dict[str, Any]]:
        """Suggest plans that move exactly two blocking events simultaneously."""
        suggestions: List[Dict[str, Any]] = []
        # Enumerate all possible meeting windows across the full workday
        candidate_windows = self._find_free_slots([], request.event_duration, request.buffer_minutes, request.target_date, request.timezone)

        for window in candidate_windows[:12]:
            blockers = self._get_blocking_events_for_window(
                selected_participants, window.start_time, window.end_time, request.target_date, request.buffer_minutes)

            if len(blockers) != 2:
                continue

            blocker_options = []
            for participant, event in blockers:
                options = self._get_move_options_for_event(participant, event, request, candidate_move_dates)
                if not options:
                    blocker_options = []
                    break
                blocker_options.append((participant, event, options[:4]))

            if len(blocker_options) != 2:
                continue

            found_for_window = False
            for selected_moves in product(*(opts for _, _, opts in blocker_options)):
                original_lists: List[Tuple[Participant, List[CalendarEvent]]] = []
                move_entries: List[Dict[str, str]] = []
                try:
                    for (participant, event, _), move_slot in zip(blocker_options, selected_moves):
                        original_lists.append((participant, participant.events))
                        participant.events = [e for e in participant.events if e is not event] + [
                            CalendarEvent(
                                person_name=event.person_name,
                                event_subject=event.event_subject,
                                start_time=move_slot.start_time,
                                end_time=move_slot.end_time,
                                timezone=event.timezone,
                                is_recurring=event.is_recurring,
                                recurrence_rule=event.recurrence_rule,
                            )
                        ]
                        move_entries.append({
                            "participant_name": participant.name,
                            "event_subject": event.event_subject,
                            "original_start_datetime": event.start_time.isoformat(),
                            "original_end_datetime": event.end_time.isoformat(),
                            "suggested_start_datetime": move_slot.start_time.isoformat(),
                            "suggested_end_datetime": move_slot.end_time.isoformat(),
                        })

                    unlocked = self._get_unlocked_slots_after_reschedule(selected_participants, request)
                    if not unlocked:
                        continue

                    preferred = [s for s in unlocked if s.start_time == window.start_time and s.end_time == window.end_time]
                    best = preferred[0] if preferred else unlocked[0]

                    key = tuple(sorted(
                        (e["participant_name"], e["event_subject"],
                         e["original_start_datetime"], e["original_end_datetime"],
                         e["suggested_start_datetime"], e["suggested_end_datetime"])
                        for e in move_entries
                    ))
                    if key in seen_keys:
                        continue

                    seen_keys.add(key)
                    suggestions.append(self._build_reschedule_suggestion(move_entries, best))
                    found_for_window = True

                    if len(suggestions) >= max_suggestions:
                        return suggestions
                finally:
                    for participant, original_events in reversed(original_lists):
                        participant.events = original_events

                if found_for_window:
                    break

        return suggestions

    def _get_candidate_reschedule_dates(self, target_date: date, max_days: int = 5) -> List[date]:
        """Return *target_date* plus the next *max_days - 1* working days."""
        dates = [target_date]
        current = target_date
        while len(dates) < max_days:
            current = self.workday_policy.get_next_working_day(current)
            dates.append(current)
        return dates
