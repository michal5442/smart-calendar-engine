"""Public API and CLI entry point for the Smart Calendar Engine."""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import List

from .models import MeetingRequest, AvailabilitySlot
from .repository import CSVCalendarRepository
from .service import MeetingFinderService
from .workday_policy import WorkdayPolicyFactory


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Default CSV path relative to this file's parent directory
_DEFAULT_CSV = Path(__file__).parent.parent / "resources" / "calendar.csv"


def find_available_slots(
    person_list: List[str],
    event_duration: timedelta,
    target_date: date = None,
    calendar_csv_path: str = None,
    buffer_minutes: int = 0,
    workday_policy_type: str = "israel",
    timezone: str = "UTC",
) -> List:
    """Find available meeting slots and return a list of start times.

    This is the main public interface of the engine, matching the signature
    required by the Comp.io evaluation.

    Args:
        person_list: Names of all required attendees.
        event_duration: Desired meeting length.
        target_date: Day to search (defaults to today).
        calendar_csv_path: Path to the CSV data file (defaults to resources/calendar.csv).
        buffer_minutes: Minimum gap to leave before and after each event.
        workday_policy_type: One of "israel", "usa", "eu", "custom".
        timezone: Timezone for output times.

    Returns:
        List of datetime.time values representing the start of each free slot.

    Example:
        >>> slots = find_available_slots(["Alice", "Jack"], timedelta(hours=1))
        >>> # → [time(7, 0), time(9, 40), time(14, 0), time(17, 0)]
    """
    if target_date is None:
        target_date = date.today()
    if calendar_csv_path is None:
        calendar_csv_path = str(_DEFAULT_CSV)

    logger.info("Participants: %s | Duration: %s | Date: %s", person_list, event_duration, target_date)

    # Assemble the object graph via Dependency Injection
    repository = CSVCalendarRepository(str(calendar_csv_path), timezone=timezone)
    workday_policy = WorkdayPolicyFactory.create_policy(workday_policy_type)
    service = MeetingFinderService(repository, workday_policy)

    slots = service.find_available_slots(MeetingRequest(
        participant_names=person_list,
        event_duration=event_duration,
        target_date=target_date,
        buffer_minutes=buffer_minutes,
        timezone=timezone,
        allow_fallback_optional=True,
    ))

    result_times = [s.start_time.time() for s in slots]
    logger.info("Available slots: %s", result_times)
    return result_times


def find_available_slots_details(
    person_list: List[str],
    event_duration: timedelta,
    target_date: date = None,
    calendar_csv_path: str = None,
    buffer_minutes: int = 0,
    workday_policy_type: str = "israel",
    timezone: str = "UTC",
) -> List[AvailabilitySlot]:
    """Same as find_available_slots but returns full AvailabilitySlot objects.

    Each slot includes start_time, end_time, duration, and deep_work_score.
    """
    if target_date is None:
        target_date = date.today()
    if calendar_csv_path is None:
        calendar_csv_path = str(_DEFAULT_CSV)

    repository = CSVCalendarRepository(str(calendar_csv_path), timezone=timezone)
    workday_policy = WorkdayPolicyFactory.create_policy(workday_policy_type)
    service = MeetingFinderService(repository, workday_policy)

    return service.find_available_slots(MeetingRequest(
        participant_names=person_list,
        event_duration=event_duration,
        target_date=target_date,
        buffer_minutes=buffer_minutes,
        timezone=timezone,
    ))


def main() -> None:
    """CLI demo: print available slots for Alice, Jack, Bob with a 15-minute buffer."""
    csv_path = _DEFAULT_CSV
    if not csv_path.exists():
        logger.error("Calendar file not found at %s", csv_path)
        return

    slots = find_available_slots(
        person_list=["Alice", "Jack", "Bob"],
        event_duration=timedelta(hours=1),
        calendar_csv_path=str(csv_path),
        buffer_minutes=15,
    )

    print("\n" + "=" * 50)
    print("AVAILABLE TIME SLOTS")
    print("=" * 50)
    if slots:
        for i, t in enumerate(slots, 1):
            print(f"{i}. {t}")
    else:
        print("No available time slots found.")
    print("=" * 50 + "\n")

    # Also show detailed output with deep-work scores
    detailed = find_available_slots_details(
        person_list=["Alice", "Jack", "Bob"],
        event_duration=timedelta(hours=1),
        calendar_csv_path=str(csv_path),
        buffer_minutes=15,
    )
    print("=" * 50)
    print("DETAILED AVAILABLE SLOTS")
    print("=" * 50)
    for i, slot in enumerate(detailed, 1):
        print(f"{i}. {slot.start_time.time()} – {slot.end_time.time()} "
              f"(deep work score: {slot.deep_work_score:.2f})")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
