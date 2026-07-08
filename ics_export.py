"""
Export Artemis assignment deadlines as an iCalendar feed.

    python -m ics_export            # writes state/deadlines.ics

The web UI also serves the feed at /deadlines.ics — subscribe to
http://localhost:5050/deadlines.ics from Apple/Google Calendar to see
assignment deadlines alongside your regular schedule.

Calendar-source events are deliberately excluded: they already come from
the student's TUMOnline calendar, so exporting them back would duplicate.
"""

import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DEFAULT_PATH = "state/deadlines.ics"


def _escape(text: str) -> str:
    """Escape text per RFC 5545 §3.3.11."""
    return (
        text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Fold a content line at 74 octets per RFC 5545 §3.1."""
    out = []
    raw = line.encode("utf-8")
    while len(raw) > 74:
        cut = 74
        # don't split inside a multi-byte UTF-8 sequence
        while cut > 1 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return out


def _ics_datetime(due: str) -> str | None:
    """DB 'YYYY-MM-DD HH:MM' (localtime) → floating ICS 'YYYYMMDDTHHMMSS'."""
    try:
        return datetime.fromisoformat(due).strftime("%Y%m%dT%H%M%S")
    except ValueError:
        return None


def _event_lines(r, dtstamp: str) -> list[str] | None:
    dtstart = _ics_datetime(r["due"])
    if not dtstart:
        return None
    summary = f"[{r['course']}] {r['title']}" if r["course"] else r["title"]
    desc_parts = []
    if r["status"]:
        desc_parts.append(f"Status: {r['status']}")
    if r["score"] is not None and r["max_points"]:
        earned = round(r["score"] / 100 * r["max_points"], 2)
        desc_parts.append(f"Score: {earned}/{r['max_points']}pts")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{r['source']}-{r['source_id']}@tumsolidator",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{dtstart}",
        f"SUMMARY:{_escape(summary)}",
    ]
    if desc_parts:
        lines.append(f"DESCRIPTION:{_escape(' | '.join(desc_parts))}")
    if r["url"]:
        lines.append(f"URL:{r['url']}")
    lines += [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape(summary)}",
        "TRIGGER:-PT24H",
        "END:VALARM",
        "END:VEVENT",
    ]
    return lines


def build_ics(db) -> str:
    """Build an iCalendar document from all Artemis events with a due date."""
    rows = db.conn.execute("""
        SELECT source, source_id, course, title, due, status, score, max_points, url
        FROM events
        WHERE source = 'artemis' AND due IS NOT NULL
        ORDER BY due
    """).fetchall()
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//TUMsolidator//Deadlines//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:TUM Deadlines",
        "X-WR-CALDESC:Artemis assignment deadlines synced by TUMsolidator",
    ]
    count = 0
    for r in rows:
        ev = _event_lines(r, dtstamp)
        if ev:
            lines += ev
            count += 1
    lines.append("END:VCALENDAR")
    log.info("Built ICS feed with %d event(s)", count)
    folded = [f for line in lines for f in _fold(line)]
    return "\r\n".join(folded) + "\r\n"


def write_ics(db, path: str = DEFAULT_PATH) -> str:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(build_ics(db))
    return path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    from storage.db import Database
    path = write_ics(Database())
    print(f"Wrote {path} — import it into your calendar app, or subscribe to")
    print("http://localhost:5050/deadlines.ics while the web UI is running.")
