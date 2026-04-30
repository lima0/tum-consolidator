import json
from pathlib import Path

from models import Document, Event

ARTEMIS_BASE = "https://artemis.tum.de"


def normalize_artemis_assignment(course: dict, exercise: dict) -> Event:
    extra = {}
    ex_type = exercise.get("type")

    if ex_type == "programming":
        extra = {
            "language":    exercise.get("language"),
            "passed":      exercise.get("passed_tests"),
            "total":       exercise.get("total_tests"),
            "build_failed": exercise.get("build_failed"),
        }
    elif ex_type == "quiz":
        extra = {
            "duration_min":    exercise.get("duration_min"),
            "attempts_allowed": exercise.get("attempts_allowed"),
            "started":         exercise.get("quiz_started"),
            "ended":           exercise.get("quiz_ended"),
        }

    return Event(
        source="artemis",
        source_id=str(exercise["id"]),
        course=course["shortName"],
        title=exercise["title"],
        event_type=ex_type or "unknown",
        due=exercise.get("due"),
        release=exercise.get("release"),
        status=exercise.get("status"),
        score=exercise.get("score"),
        max_points=exercise.get("max_points"),
        url=f"{ARTEMIS_BASE}/courses/{course['id']}/exercises/{exercise['id']}",
        extra=json.dumps(extra) if extra else None,
    )


def normalize_artemis_attachment(course: dict, unit: dict, local_path: str | None = None) -> Document:
    attachment = unit.get("attachment") or {}
    link = attachment.get("link") or unit.get("link") or ""
    filename = Path(link.split("?")[0]).name or unit.get("name", "")

    return Document(
        source="artemis",
        source_id=f"unit_{unit['id']}",
        course=course["shortName"],
        title=unit.get("name", ""),
        filename=filename,
        local_path=local_path,
        url=link,
        updated_at=attachment.get("uploadDate"),
    )


def normalize_moodle_document(
    course: dict,
    mod: dict,
    entry: dict,
    local_path: str | None = None,
) -> Document:
    filename = entry.get("filename") or Path(entry.get("fileurl", "")).name or mod["name"]
    tm = entry.get("timemodified")

    return Document(
        source="moodle",
        source_id=f"mod_{mod['id']}_{filename}",
        course=course.get("shortname") or course.get("fullname", ""),
        title=mod["name"],
        filename=filename,
        local_path=local_path,
        url=entry.get("fileurl", ""),
        updated_at=str(tm) if tm else None,
    )


def normalize_calendar_event(event: dict) -> Event:
    start = event["start"]
    end   = event["end"]
    loc   = event.get("location")

    return Event(
        source="calendar",
        source_id=event["uid"],
        course=None,
        title=event["title"],
        event_type=event.get("event_type", "class"),
        due=end.strftime("%Y-%m-%d %H:%M"),
        release=start.strftime("%Y-%m-%d %H:%M"),
        status=None,
        score=None,
        max_points=None,
        url=None,
        extra=json.dumps({"location": loc}) if loc else None,
    )
