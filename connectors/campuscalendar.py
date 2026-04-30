"""
TUM Campus calendar connector.

Fetches the personal ICS feed (TUM_CALENDAR env var) and prints all
events in the next N days in a compact, LLM-friendly format.
"""

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

TZ = ZoneInfo("Europe/Berlin")
# ICS types we want to strip from the course name
_TYPE_RE = re.compile(
    r'\s+(VO|UE|SE|PR|EX|TU|KO|VI|PS|RE|VU|AU|AG|BS|KV|WS)\b.*$',
    re.IGNORECASE,
)
# Building code suffix in location, e.g. "(5608.EG.053)"
_BLDG_RE = re.compile(r'\s*\(\d{4}\.\w+\.\w+\)\s*$')
# Noisy constraints appended to room names, e.g. "nur Mo-Di 7-19 Uhr"
_CONSTRAINT_RE = re.compile(r'\s+nur\s+.+$', re.IGNORECASE)


def _clean_summary(raw: str) -> str:
    """Strip the type/group suffix from a TUM course summary string."""
    # ICS escaping uses backslash-comma; icalendar already unescapes, but guard anyway
    text = raw.replace('\\,', ',').split(',')[0].strip()
    text = _TYPE_RE.sub('', text).strip()
    return text


def _clean_location(raw: str) -> str:
    if not raw:
        return ""
    text = raw.replace('\\,', ',')
    # Format is often "room_code, Room Name (building)" — take the part after the comma
    if ',' in text:
        text = text.split(',', 1)[1].strip()
    text = _BLDG_RE.sub('', text)
    text = _CONSTRAINT_RE.sub('', text)
    return text.strip()


def _to_local(dt) -> datetime:
    """Coerce an icalendar date or datetime to a timezone-aware local datetime."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    # date-only (all-day events) — treat as midnight local
    return datetime(dt.year, dt.month, dt.day, tzinfo=TZ)


def fetch_events(days: int = 10) -> list[dict]:
    url = os.environ.get("TUM_CALENDAR")
    if not url:
        sys.exit("TUM_CALENDAR environment variable not set.")

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    now = datetime.now(TZ)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_end   = window_start + timedelta(days=days)

    cal = Calendar.from_ical(resp.content)
    events = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        dtstart = component.get("DTSTART")
        dtend   = component.get("DTEND")
        if not dtstart:
            continue

        start = _to_local(dtstart.dt)
        end   = _to_local(dtend.dt) if dtend else start + timedelta(hours=1)

        if start >= window_end or end <= window_start:
            continue

        summary  = str(component.get("SUMMARY", ""))
        location = str(component.get("LOCATION", ""))
        events.append({
            "start":    start,
            "end":      end,
            "title":    _clean_summary(summary),
            "location": _clean_location(location),
        })

    events.sort(key=lambda e: e["start"])
    return events


def debug_print(events: list[dict], days: int = 10) -> str:
    if not events:
        return f"No events in the next {days} days."

    lines = []
    current_day = None
    for ev in events:
        day = ev["start"].date()
        if day != current_day:
            current_day = day
            dow = ev["start"].strftime("%a")
            lines.append(f"\n{day.isoformat()} {dow}")
        start_s = ev["start"].strftime("%H:%M")
        end_s   = ev["end"].strftime("%H:%M")
        loc     = f"  @ {ev['location']}" if ev["location"] else ""
        lines.append(f"  {start_s}-{end_s}  {ev['title']}{loc}")

    return "\n".join(lines).strip()


def main(days: int = 10) -> None:
    events = fetch_events(days)
    print(debug_print(events, days))


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    main(days)
