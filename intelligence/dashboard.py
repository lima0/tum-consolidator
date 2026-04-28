"""
Markdown dashboard writer.
Regenerates ~/Documents/tum-dashboard.md nightly.
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path

import db
import connectors.campuscalendar as ical

log = logging.getLogger(__name__)

DASHBOARD_PATH = Path.home() / "Documents" / "tum-dashboard.md"


def _open_deadlines_md() -> str:
    rows = db.get_db().execute(
        """SELECT course_code, title, occurs_at, raw_json
           FROM events WHERE source='artemis' AND event_type='deadline'
           ORDER BY occurs_at"""
    ).fetchall()
    if not rows:
        return "_No open deadlines._\n"

    lines = ["| Course | Assignment | Due | Status |", "|--------|-----------|-----|--------|"]
    for row in rows:
        try:
            raw    = json.loads(row["raw_json"] or "{}")
            status = raw.get("status", "?")
            pts    = raw.get("max_points", "?")
        except (json.JSONDecodeError, TypeError):
            status = "?"
            pts    = "?"
        due = (row["occurs_at"] or "")[:16].replace("T", " ")
        lines.append(f"| {row['course_code']} | {row['title']} | {due} | {status} |")
    return "\n".join(lines) + "\n"


def _recent_completions_md() -> str:
    rows = db.get_db().execute(
        """SELECT action_type, payload, responded_at
           FROM actions_taken
           WHERE user_response='completed'
           ORDER BY responded_at DESC LIMIT 10"""
    ).fetchall()
    if not rows:
        return "_No completions recorded yet._\n"
    lines = []
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            title   = payload.get("title", row["action_type"])
        except (json.JSONDecodeError, TypeError):
            title = row["action_type"]
        when = (row["responded_at"] or "")[:10]
        lines.append(f"- {title} ✓ {when}")
    return "\n".join(lines) + "\n"


def _upcoming_calendar_md() -> str:
    try:
        events = ical.fetch_events(days=7)
        return ical.format_for_llm(events, 7) + "\n"
    except Exception as e:
        return f"_Calendar unavailable: {e}_\n"


def _agent_memory_md() -> str:
    mem_file = Path("agent_memory.md")
    try:
        text    = mem_file.read_text().strip()
        entries = [e for e in text.split("\n## ") if e.strip()]
        if not entries:
            return "_No digest entries yet._\n"
        last = entries[-1].strip()
        return f"```\n{last[:600]}\n```\n"
    except FileNotFoundError:
        return "_No digest entries yet._\n"


def write_dashboard() -> None:
    today = date.today().isoformat()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M")

    content = f"""# TUM SoSe26 Dashboard

_Last updated: {now}_

---

## Open Deadlines

{_open_deadlines_md()}

## Upcoming Schedule (7 days)

{_upcoming_calendar_md()}

## Recent Completions

{_recent_completions_md()}

## Last Agent Digest

{_agent_memory_md()}
"""

    DASHBOARD_PATH.write_text(content)
    log.info("Dashboard written to %s", DASHBOARD_PATH)
    print(f"Dashboard: {DASHBOARD_PATH}")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    write_dashboard()
