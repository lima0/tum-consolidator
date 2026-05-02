import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import Anthropic

NOTE_NAME    = "TUMsolidator"
CONTEXT_FILE = "state/context.md"

BRIEFING_PROMPT = """You are a study advisor for a TUM Informatics student.

Student context:
{context}

Upcoming deadlines (next 7 days):
{deadlines}

Today and tomorrow schedule:
{schedule}

Recently added course materials (last 48h):
{materials}

Write a concise daily briefing (5-8 sentences). Prioritize what to work on TODAY and WHY. \
Be specific about time estimates where available. Flag anything critical or overdue. \
Don't list everything — reason about what matters most given the schedule and workload.
Summarize your briefing again in the end."""


# ── Plain-text helpers for LLM context ──────────────────────────────────────

def _deadlines_text(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points, status
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now') AND datetime('now', '+7 days')
        ORDER BY due
    """).fetchall()
    if not rows:
        return "None."
    lines = []
    for r in rows:
        due       = datetime.fromisoformat(r["due"]) if r["due"] else None
        due_str   = due.strftime("%a %d %b %H:%M") if due else "?"
        score_str = f" (score: {r['score']}/{r['max_points']})" if r["score"] is not None else ""
        status    = f" [{r['status']}]" if r["status"] else ""
        lines.append(f"- {r['course']}: {r['title']}{score_str}{status} — due {due_str}")
    return "\n".join(lines)


def _calendar_text(db) -> str:
    tomorrow_end = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 00:00")
    rows = db.conn.execute("""
        SELECT title, release, extra, event_type
        FROM events
        WHERE source = 'calendar'
          AND release BETWEEN datetime('now', 'start of day') AND ?
        ORDER BY release
    """, (tomorrow_end,)).fetchall()
    if not rows:
        return "None."
    lines = []
    for r in rows:
        start    = datetime.fromisoformat(r["release"]) if r["release"] else None
        time_str = start.strftime("%a %H:%M") if start else "?"
        extra    = json.loads(r["extra"]) if r["extra"] else {}
        loc      = f" @ {extra['location']}" if extra.get("location") else ""
        lines.append(f"- {time_str}: {r['title']} ({r['event_type']}){loc}")
    return "\n".join(lines)


def _materials_text(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, summary_json
        FROM documents
        WHERE first_seen > datetime('now', '-48 hours')
          AND processed_at IS NOT NULL
        ORDER BY first_seen DESC
        LIMIT 10
    """).fetchall()
    if not rows:
        return "None."
    lines = []
    for r in rows:
        s          = json.loads(r["summary_json"]) if r["summary_json"] else {}
        mins       = s.get("estimated_minutes", "?")
        difficulty = s.get("difficulty", "?")
        topics     = ", ".join(s.get("topics", [])[:4])
        lines.append(f"- {r['course']}: {r['title']} (~{mins}min, {difficulty}) — {topics}")
    return "\n".join(lines)


# ── LLM briefing ─────────────────────────────────────────────────────────────

def build_briefing(db) -> str:
    context = (
        Path(CONTEXT_FILE).read_text()
        if Path(CONTEXT_FILE).exists()
        else "No student context provided."
    )
    prompt = BRIEFING_PROMPT.format(
        context=context,
        deadlines=_deadlines_text(db),
        schedule=_calendar_text(db),
        materials=_materials_text(db),
    )
    client   = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Output ───────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from storage.db import Database
    db = Database()

    briefing  = build_briefing(db)

    print("\n--- BRIEFING ---")
    print(briefing)
    # push_to_notes(full_note)
    # print("Note pushed to Apple Notes.")
