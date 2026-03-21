# Smart Calendar Engine

A calendar scheduling engine that finds available meeting slots across multiple participants.

## Setup

```bash
pip install -r requirements.txt
pip install -e .
```

## Run

```bash
python -m io_comp.app        # CLI demo
python -m io_comp.api        # Flask UI at http://localhost:5000
pytest tests/ -v             # 42 tests
```

## Core Usage

```python
from datetime import timedelta, date
from io_comp import find_available_slots

slots = find_available_slots(
    person_list=["Alice", "Jack"],
    event_duration=timedelta(hours=1),
    target_date=date(2024, 3, 18),
)
# → [07:00, 09:40, 14:00, 17:00]
```

## Project Structure

```
io_comp/
├── models.py          # Pydantic v2 data models
├── repository.py      # CSV, SQLite & InMemory repositories
├── service.py         # MeetingFinderService — core algorithm
├── workday_policy.py  # Pluggable workday rules per region
├── app.py             # Public API & CLI
└── api.py             # Flask REST API with rate limiting
tests/
└── test_app.py        # 42 tests
resources/
└── calendar.csv
```

## Architecture

Three design patterns work together:

**Repository Pattern** — `ICalendarRepository` ABC with three implementations.
Swapping CSV for SQLite requires zero changes to the service layer:

```python
# CSV (default)
service = MeetingFinderService(CSVCalendarRepository("resources/calendar.csv"), policy)

# SQLite — drop-in replacement
service = MeetingFinderService(SQLiteCalendarRepository("calendar.db", seed_from_csv="resources/calendar.csv"), policy)

# InMemory — used in tests
service = MeetingFinderService(InMemoryCalendarRepository(events), policy)
```

**Strategy Pattern** — `IWorkdayPolicy` with pluggable implementations:

```python
WorkdayPolicyFactory.create_policy("israel")  # 07:00–19:00, Fri–Sat off
WorkdayPolicyFactory.create_policy("usa")     # 09:00–17:00, Sat–Sun off
WorkdayPolicyFactory.create_policy("eu")      # 08:00–18:00, Sat–Sun off
WorkdayPolicyFactory.create_policy("custom", start_hour=8, end_hour=20)
```

**Dependency Injection** — service receives both dependencies at construction:

```python
service = MeetingFinderService(repository, workday_policy)
```

## Features

| Feature | Details |
|---|---|
| Available slot search | Merges busy slots across participants, returns free windows chronologically |
| Recurring events | RRULE expansion via `python-dateutil`, graceful fallback if not installed |
| Buffer time | `buffer_minutes` gap between meetings |
| Mandatory / optional participants | Retries with mandatory-only if optional blocks all slots |
| Deep work scoring | Isolation score 0–1 per slot (distance from nearest event) |
| Reschedule suggestions | Proposes moving blocking events to free a slot |
| Multi-day search | `find_available_slots_multi_day(start_date, end_date)` |
| SQLite persistence | Full CRUD: add, update, delete, seed from CSV |
| Rate limiting | 30 req/60s per IP on all POST endpoints — HTTP 429 on exceed |
| Flask UI | Step-by-step booking: participants → duration → slot → confirm |

## Algorithm (`find_available_slots`)

1. Load participants from repository
2. Expand recurring events (RRULE) for the target date
3. Normalize all times to UTC
4. Merge busy slots across all participants
5. Find free windows within workday boundaries
6. Score each window for deep work isolation
7. Return sorted chronologically

## Tests

```
TestSQLiteRepository      5   add, load, update, delete, drop-in replacement
TestRecurringEvents       2   RRULE expansion, non-recurring isolation
TestRateLimiting          3   within limit, POST, 429 on exceed
TestReadmeExample         2   exact output [07:00, 09:40, 14:00, 17:00], duration
TestCalendarEvent         4   creation, validation, overlap
TestParticipant           2   creation, busy slots
TestInMemoryRepository    3   load events, participants, per-person
TestWorkdayPolicy         5   Israel, USA, working day, next day, factory
TestMeetingFinderService  11  core algorithm + reschedule suggestions
TestIntegration           2   end-to-end, dependency injection
TestDeepWorkMetrics       2   creation, isolation score
─────────────────────────────────────────────────────────────────────────────
Total                    42   ✅ all passed, 0 warnings
```
