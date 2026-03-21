"""
Flask API for the Calendar Engine
ממשק API ל-Calendar Engine

מספק endpoints לניהול הפגישות דרך דפדפן
"""

from flask import Flask, render_template, request, jsonify
from datetime import datetime, timedelta, date
from typing import List, Dict
from functools import wraps
import json
import logging
import csv
import time
from collections import defaultdict
from pathlib import Path

from .models import MeetingRequest, CalendarEvent
from .repository import CSVCalendarRepository
from .service import MeetingFinderService
from .workday_policy import WorkdayPolicyFactory

# הגדר logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# יצור Flask app
app = Flask(__name__, template_folder='templates', static_folder='static')

# הגדרות גלובליות
CSV_PATH = "resources/calendar.csv"
DEFAULT_POLICY = "israel"

_service_instance: MeetingFinderService = None

# Rate limiting state: ip -> list of request timestamps
_rate_limit_store: Dict[str, list] = defaultdict(list)
RATE_LIMIT_REQUESTS = 30  # max requests
RATE_LIMIT_WINDOW = 60    # per N seconds


def rate_limited(f):
    """Decorator: reject requests exceeding RATE_LIMIT_REQUESTS per RATE_LIMIT_WINDOW seconds."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr
        now = time.time()
        window_start = now - RATE_LIMIT_WINDOW
        _rate_limit_store[ip] = [
            t for t in _rate_limit_store[ip] if t > window_start
        ]
        if len(_rate_limit_store[ip]) >= RATE_LIMIT_REQUESTS:
            return jsonify({
                "error": f"Rate limit exceeded: max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s",
                "status": "error",
            }), 429
        _rate_limit_store[ip].append(now)
        return f(*args, **kwargs)
    return wrapper


def get_service() -> MeetingFinderService:
    """Return a shared MeetingFinderService instance (singleton)."""
    global _service_instance
    if _service_instance is None:
        repository = CSVCalendarRepository(CSV_PATH)
        policy = WorkdayPolicyFactory.create_policy(DEFAULT_POLICY)
        _service_instance = MeetingFinderService(repository, policy)
    return _service_instance


def format_datetime_range(start_dt: datetime, end_dt: datetime) -> str:
    """Format a date/time range for UI display."""
    return f"{start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%H:%M')}"


@app.route('/')
def index():
    """דף ראשי"""
    return render_template('index.html')


@app.route('/api/participants', methods=['GET'])
def get_participants():
    """קבל רשימת משתתפים
    
    GET /api/participants
    
    Response:
        {
            "participants": ["Alice", "Jack", "Bob"],
            "status": "success"
        }
    """
    try:
        service = get_service()
        participants_dict = service.repository.load_participants()
        participants = list(participants_dict.keys())
        
        logger.info(f"הוחזרו {len(participants)} משתתפים")
        
        return jsonify({
            "participants": participants,
            "status": "success",
            "count": len(participants)
        })
    except Exception as e:
        logger.error(f"שגיאה בקבלת משתתפים: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 400


@app.route('/api/available-slots', methods=['POST'])
@rate_limited
def find_available_slots():
    """חיפוש זמנים זמינים
    
    POST /api/available-slots
    {
        "mandatory_participants": ["Alice", "Jack"],
        "optional_participants": ["Bob"],
        "event_duration_hours": 1,
        "target_date": "2024-03-18",
        "buffer_minutes": 15
    }
    
    Response:
        {
            "slots": [
                {
                    "start_time": "09:45",
                    "end_time": "10:45",
                    "duration": "1:00:00",
                    "deep_work_score": 0.03
                },
                ...
            ],
            "status": "success"
        }
    """
    try:
        data = request.get_json()
        
        # אימות נתונים
        mandatory = data.get('mandatory_participants', [])
        optional = data.get('optional_participants', [])
        event_hours = data.get('event_duration_hours', 1)
        target_date_str = data.get('target_date')
        buffer_minutes = data.get('buffer_minutes', 0)
        
        # בדוק שיש משתתפים חובה לפחות
        if not mandatory:
            return jsonify({
                "error": "יש לבחור לפחות משתתף אחד חובה",
                "status": "error"
            }), 400
        
        # פרוש התאריך
        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({
                "error": "פורמט תאריך לא תקין",
                "status": "error"
            }), 400
        
        # יצור רשימת משתתפים
        all_participants = mandatory + optional
        event_duration = timedelta(hours=event_hours)
        
        # חיפוש זמנים
        service = get_service()
        meeting_request = MeetingRequest(
            participant_names=all_participants,
            mandatory_participants=mandatory,
            optional_participants=optional,
            event_duration=event_duration,
            target_date=target_date,
            buffer_minutes=buffer_minutes,
            allow_fallback_optional=True,
        )
        
        slots = service.find_available_slots(meeting_request)
        
        # המר לפורמט JSON
        slots_data = []
        for slot in slots:
            slots_data.append({
                "start_time": slot.start_time.time().strftime("%H:%M"),
                "end_time": slot.end_time.time().strftime("%H:%M"),
                "duration": str(slot.duration),
                "deep_work_score": round(slot.deep_work_score, 2),
                "start_datetime": slot.start_time.isoformat(),
                "end_datetime": slot.end_time.isoformat(),
            })
        
        logger.info(f"נמצאו {len(slots_data)} זמנים זמינים")
        
        return jsonify({
            "slots": slots_data,
            "status": "success",
            "count": len(slots_data),
            "target_date": target_date_str,
        })
    
    except Exception as e:
        logger.error(f"שגיאה בחיפוש זמנים: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/suggest-reschedules', methods=['POST'])
@rate_limited
def suggest_reschedules():
    """הצע הזזת פגישות קיימות כדי לפנות מקום לפגישה חדשה"""
    try:
        data = request.get_json()

        mandatory = data.get('mandatory_participants', [])
        optional = data.get('optional_participants', [])
        event_hours = data.get('event_duration_hours', 1)
        target_date_str = data.get('target_date')
        buffer_minutes = data.get('buffer_minutes', 0)

        if not mandatory:
            return jsonify({
                "error": "יש לבחור לפחות משתתף אחד חובה",
                "status": "error"
            }), 400

        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return jsonify({
                "error": "פורמט תאריך לא תקין",
                "status": "error"
            }), 400

        all_participants = mandatory + optional
        event_duration = timedelta(hours=event_hours)

        service = get_service()
        meeting_request = MeetingRequest(
            participant_names=all_participants,
            mandatory_participants=mandatory,
            optional_participants=optional,
            event_duration=event_duration,
            target_date=target_date,
            buffer_minutes=buffer_minutes,
            allow_fallback_optional=True,
        )

        suggestions = service.suggest_reschedules_for_request(
            meeting_request,
            max_suggestions=5,
        )

        suggestions_data = []
        for suggestion in suggestions:
            original_start = datetime.fromisoformat(suggestion["original_start_datetime"])
            original_end = datetime.fromisoformat(suggestion["original_end_datetime"])
            suggested_start = datetime.fromisoformat(suggestion["suggested_start_datetime"])
            suggested_end = datetime.fromisoformat(suggestion["suggested_end_datetime"])
            unlocked_start = datetime.fromisoformat(suggestion["unlocked_meeting_start_datetime"])
            unlocked_end = datetime.fromisoformat(suggestion["unlocked_meeting_end_datetime"])
            moves = []
            for move in suggestion.get("moves", []):
                move_original_start = datetime.fromisoformat(move["original_start_datetime"])
                move_original_end = datetime.fromisoformat(move["original_end_datetime"])
                move_suggested_start = datetime.fromisoformat(move["suggested_start_datetime"])
                move_suggested_end = datetime.fromisoformat(move["suggested_end_datetime"])
                moves.append({
                    "participant_name": move["participant_name"],
                    "event_subject": move["event_subject"],
                    "original_start_datetime": move["original_start_datetime"],
                    "original_end_datetime": move["original_end_datetime"],
                    "suggested_start_datetime": move["suggested_start_datetime"],
                    "suggested_end_datetime": move["suggested_end_datetime"],
                    "original_time": format_datetime_range(move_original_start, move_original_end),
                    "suggested_time": format_datetime_range(move_suggested_start, move_suggested_end),
                })

            suggestions_data.append({
                "participant_name": suggestion["participant_name"],
                "event_subject": suggestion["event_subject"],
                "original_start_datetime": suggestion["original_start_datetime"],
                "original_end_datetime": suggestion["original_end_datetime"],
                "suggested_start_datetime": suggestion["suggested_start_datetime"],
                "suggested_end_datetime": suggestion["suggested_end_datetime"],
                "moves": moves,
                "move_count": suggestion.get("move_count", len(moves) or 1),
                "unlocked_meeting_start_datetime": suggestion["unlocked_meeting_start_datetime"],
                "unlocked_meeting_end_datetime": suggestion["unlocked_meeting_end_datetime"],
                "unlocked_meeting_deep_work_score": suggestion["unlocked_meeting_deep_work_score"],
                "original_time": format_datetime_range(original_start, original_end),
                "suggested_time": format_datetime_range(suggested_start, suggested_end),
                "unlocked_meeting_time": format_datetime_range(unlocked_start, unlocked_end),
            })

        return jsonify({
            "status": "success",
            "suggestions": suggestions_data,
            "count": len(suggestions_data),
        })
    except Exception as e:
        logger.error(f"שגיאה בהצעת הזזות פגישות: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/move-meeting', methods=['POST'])
@rate_limited
def move_meeting():
    """הזזת פגישה קיימת לזמן חדש בקובץ ה-CSV"""
    try:
        data = request.get_json()

        participant_name = data.get('participant_name')
        event_subject = data.get('event_subject')
        from_start_str = data.get('from_start_datetime')
        from_end_str = data.get('from_end_datetime')
        to_start_str = data.get('to_start_datetime')
        to_end_str = data.get('to_end_datetime')

        if not all([participant_name, event_subject, from_start_str, from_end_str, to_start_str, to_end_str]):
            return jsonify({
                "error": "חסרים נתונים להזזת הפגישה",
                "status": "error"
            }), 400

        try:
            from_start_dt = datetime.fromisoformat(from_start_str)
            from_end_dt = datetime.fromisoformat(from_end_str)
            to_start_dt = datetime.fromisoformat(to_start_str)
            to_end_dt = datetime.fromisoformat(to_end_str)
        except ValueError:
            return jsonify({
                "error": "פורמט תאריך/שעה לא תקין",
                "status": "error"
            }), 400

        csv_path = Path(CSV_PATH)
        if not csv_path.exists():
            return jsonify({
                "error": "קובץ היומן לא נמצא",
                "status": "error"
            }), 404

        rows = []
        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            rows = list(reader)

        updated = False
        for idx, row in enumerate(rows):
            clean_row = [col.strip() for col in row]
            if len(clean_row) < 4:
                continue

            row_participant = clean_row[0]
            row_subject = clean_row[1]

            try:
                row_start_time = datetime.strptime(clean_row[2], "%H:%M").time()
                row_end_time = datetime.strptime(clean_row[3], "%H:%M").time()
            except ValueError:
                continue

            if len(clean_row) >= 6 and clean_row[4] and clean_row[5]:
                try:
                    row_start_date = datetime.strptime(clean_row[4], "%Y-%m-%d").date()
                    row_end_date = datetime.strptime(clean_row[5], "%Y-%m-%d").date()
                except ValueError:
                    row_start_date = from_start_dt.date()
                    row_end_date = from_end_dt.date()
            else:
                row_start_date = from_start_dt.date()
                row_end_date = from_end_dt.date()

            row_start_dt = datetime.combine(row_start_date, row_start_time)
            row_end_dt = datetime.combine(row_end_date, row_end_time)

            is_target = (
                row_participant == participant_name
                and row_subject == event_subject
                and row_start_dt == from_start_dt
                and row_end_dt == from_end_dt
            )

            if not is_target:
                continue

            # עדכן את הפגישה לזמן החדש
            while len(rows[idx]) < 6:
                rows[idx].append("")

            rows[idx][2] = to_start_dt.strftime("%H:%M")
            rows[idx][3] = to_end_dt.strftime("%H:%M")
            rows[idx][4] = to_start_dt.date().isoformat()
            rows[idx][5] = to_end_dt.date().isoformat()

            updated = True
            break

        if not updated:
            return jsonify({
                "error": "לא נמצאה פגישה מתאימה להזזה",
                "status": "error"
            }), 404

        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        logger.info(
            "הפגישה הוזזה: %s / %s מ-%s ל-%s",
            participant_name,
            event_subject,
            from_start_dt.isoformat(),
            to_start_dt.isoformat(),
        )

        # אפס את ה-singleton כדי שהשינוי ב-CSV ייטען מחדש
        global _service_instance
        _service_instance = None

        return jsonify({
            "status": "success",
            "message": "הפגישה הוזזה בהצלחה",
            "participant_name": participant_name,
            "event_subject": event_subject,
            "new_start_time": to_start_dt.strftime("%H:%M"),
            "new_end_time": to_end_dt.strftime("%H:%M"),
        })
    except Exception as e:
        logger.error(f"שגיאה בהזזת פגישה: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/book-meeting', methods=['POST'])
@rate_limited
def book_meeting():
    """הוסף פגישה חדשה לקובץ CSV
    
    POST /api/book-meeting
    {
        "mandatory_participants": ["Alice", "Jack"],
        "optional_participants": ["Bob"],
        "event_subject": "פגישת תכנון",
        "start_datetime": "2024-03-18T09:45:00",
        "end_datetime": "2024-03-18T10:45:00"
    }
    
    Response:
        {
            "message": "הפגישה נוספה בהצלחה",
            "status": "success",
            "created_events": 2
        }
    """
    try:
        data = request.get_json()
        
        mandatory = data.get('mandatory_participants', [])
        optional = data.get('optional_participants', [])
        event_subject = data.get('event_subject', 'פגישה')
        start_str = data.get('start_datetime')
        end_str = data.get('end_datetime')
        
        # פרוש זמנים
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
        except ValueError:
            return jsonify({
                "error": "פורמט זמן לא תקין",
                "status": "error"
            }), 400
        
        # כל המשתתפים
        all_participants = mandatory + optional
        
        if not all_participants:
            return jsonify({
                "error": "יש לבחור לפחות משתתף אחד",
                "status": "error"
            }), 400
        
        # קרא את הקובץ הקיים
        csv_path = Path(CSV_PATH)
        events = []
        
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                events = list(reader)
        except FileNotFoundError:
            events = []
        
        # הוסף אירועים חדשים לכל משתתף
        for participant in all_participants:
            events.append([
                participant,
                event_subject,
                start_dt.strftime("%H:%M"),
                end_dt.strftime("%H:%M"),
                start_dt.date().isoformat(),
                end_dt.date().isoformat(),
            ])
        
        # כתוב חזרה לקובץ
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(events)
        
        logger.info(f"הפגישה '{event_subject}' נוספה ל-CSV עם {len(all_participants)} משתתפים")

        # אפס את ה-singleton כדי שהשינוי ב-CSV ייטען מחדש
        global _service_instance
        _service_instance = None

        return jsonify({
            "message": "הפגישה נוספה בהצלחה!",
            "status": "success",
            "created_events": len(all_participants),
            "participants": all_participants,
            "event_subject": event_subject,
            "start_time": start_dt.time().strftime("%H:%M"),
            "end_time": end_dt.time().strftime("%H:%M"),
        })
    
    except Exception as e:
        logger.error(f"שגיאה בהוספת פגישה: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/validate-meeting-time', methods=['POST'])
@rate_limited
def validate_meeting_time():
    """בדוק אם שעת התחלה ידנית פנויה לפגישה"""
    try:
        data = request.get_json()

        mandatory = data.get('mandatory_participants', [])
        optional = data.get('optional_participants', [])
        event_hours = data.get('event_duration_hours', 1)
        target_date_str = data.get('target_date')
        start_time_str = data.get('start_time')
        buffer_minutes = data.get('buffer_minutes', 0)

        if not mandatory:
            return jsonify({
                "error": "יש לבחור לפחות משתתף אחד חובה",
                "status": "error"
            }), 400

        if not target_date_str or not start_time_str:
            return jsonify({
                "error": "חסרים תאריך או שעת התחלה",
                "status": "error"
            }), 400

        try:
            target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
            start_time = datetime.strptime(start_time_str, "%H:%M").time()
        except ValueError:
            return jsonify({
                "error": "פורמט תאריך/שעה לא תקין",
                "status": "error"
            }), 400

        event_duration = timedelta(hours=event_hours)
        selected_start = datetime.combine(target_date, start_time)
        selected_end = selected_start + event_duration
        buffer_delta = timedelta(minutes=buffer_minutes)

        service = get_service()

        # בדוק עמידה בשעות העבודה
        workday_schedule = service.workday_policy.get_workday_schedule()
        workday_start = datetime.combine(target_date, datetime.strptime(f"{workday_schedule.start_hour:02d}:00", "%H:%M").time())
        workday_end = datetime.combine(target_date, datetime.strptime(f"{workday_schedule.end_hour:02d}:00", "%H:%M").time())

        if selected_start < workday_start or selected_end > workday_end:
            return jsonify({
                "status": "success",
                "is_free": False,
                "reason": "השעה שנבחרה מחוץ לשעות העבודה",
            })

        all_participants = mandatory + optional
        participants_dict = service.repository.load_participants()

        missing = [name for name in all_participants if name not in participants_dict]
        if missing:
            return jsonify({
                "status": "success",
                "is_free": False,
                "reason": f"משתתפים לא נמצאו: {', '.join(missing)}",
            })

        # בדוק התנגשויות עבור כל המשתתפים
        for participant_name in all_participants:
            participant = participants_dict[participant_name]
            for event in participant.events:
                if event.start_time.date() != target_date:
                    continue

                busy_start = event.start_time - buffer_delta
                busy_end = event.end_time + buffer_delta

                has_overlap = selected_start < busy_end and selected_end > busy_start
                if has_overlap:
                    return jsonify({
                        "status": "success",
                        "is_free": False,
                        "reason": f"התנגשות עם אירוע של {participant_name}",
                    })

        # נסה למצוא ניקוד עמוק-עבודה (אם קיים בדיוק על אותה שעת התחלה)
        deep_work_score = 0.0
        try:
            meeting_request = MeetingRequest(
                participant_names=all_participants,
                mandatory_participants=mandatory,
                optional_participants=optional,
                event_duration=event_duration,
                target_date=target_date,
                buffer_minutes=buffer_minutes,
                allow_fallback_optional=True,
            )
            available_slots = service.find_available_slots(meeting_request)
            for slot in available_slots:
                if slot.start_time.strftime("%H:%M") == start_time_str:
                    deep_work_score = round(slot.deep_work_score, 2)
                    break
        except Exception:
            deep_work_score = 0.0

        return jsonify({
            "status": "success",
            "is_free": True,
            "start_time": selected_start.strftime("%H:%M"),
            "end_time": selected_end.strftime("%H:%M"),
            "start_datetime": selected_start.isoformat(),
            "end_datetime": selected_end.isoformat(),
            "duration": str(event_duration),
            "deep_work_score": deep_work_score,
        })
    except Exception as e:
        logger.error(f"שגיאה בבדיקת זמן ידני: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/upcoming-meetings', methods=['GET'])
def get_upcoming_meetings():
    """קבל את כל הפגישות מהיום ואילך"""
    try:
        csv_path = Path(CSV_PATH)

        if not csv_path.exists():
            return jsonify({
                "status": "success",
                "meetings": [],
                "count": 0,
            })

        today = date.today()
        grouped: Dict[tuple, set] = {}

        with open(csv_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                row = [col.strip() for col in row]
                if len(row) < 4:
                    continue

                person_name = row[0]
                event_subject = row[1]
                start_time_str = row[2]
                end_time_str = row[3]

                # פורמט חדש: כולל תאריך התחלה/סיום בעמודות 5-6
                if len(row) >= 6 and row[4] and row[5]:
                    start_date = datetime.strptime(row[4], "%Y-%m-%d").date()
                    end_date = datetime.strptime(row[5], "%Y-%m-%d").date()
                else:
                    # תאימות לאחור: אם אין תאריך, נייחס להיום
                    start_date = today
                    end_date = today

                if start_date < today:
                    continue

                start_dt = datetime.combine(start_date, datetime.strptime(start_time_str, "%H:%M").time())
                end_dt = datetime.combine(end_date, datetime.strptime(end_time_str, "%H:%M").time())

                key = (event_subject, start_dt, end_dt)
                if key not in grouped:
                    grouped[key] = set()
                grouped[key].add(person_name)

        meetings = []
        for (event_subject, start_dt, end_dt), participants in grouped.items():
            meetings.append({
                "event_subject": event_subject,
                "start_date": start_dt.date().isoformat(),
                "start_time": start_dt.strftime("%H:%M"),
                "end_time": end_dt.strftime("%H:%M"),
                "participants": sorted(list(participants)),
                "participants_count": len(participants),
            })

        meetings = sorted(meetings, key=lambda x: (x["start_date"], x["start_time"]))

        return jsonify({
            "status": "success",
            "meetings": meetings,
            "count": len(meetings),
        })
    except Exception as e:
        logger.error(f"שגיאה בקבלת פגישות עתידיות: {e}")
        return jsonify({
            "error": str(e),
            "status": "error"
        }), 500


@app.route('/api/health', methods=['GET'])
def health():
    """בדיקת בריאות ה-API"""
    try:
        service = get_service()
        participants = service.repository.load_participants()
        
        return jsonify({
            "status": "healthy",
            "participants_count": len(participants),
            "csv_path": CSV_PATH,
            "policy": DEFAULT_POLICY,
        })
    except Exception as e:
        return jsonify({
            "status": "unhealthy",
            "error": str(e),
        }), 500


def run_server(host='127.0.0.1', port=5000, debug=True):
    """הרץ את ה-Flask server
    
    Args:
        host: כתובת ה-host
        port: פורט
        debug: הפעל debug mode
    """
    logger.info(f"התחל Flask server ב-http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    run_server()
