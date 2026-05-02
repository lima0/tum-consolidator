import json
import os
import re
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
The WHY is important. It's how the student should orient themselves.
Be specific about time estimates where available. Flag anything critical or overdue. \
Don't list everything — reason about what matters most given the schedule and workload.
Output Format: Plain text only. No markdown. No asterisks, no hashes.
Bullet points and numbered lists are okay. 
Mention a general summary, Actionable task list, What should be done. Anything a student may need to keep up.
"""


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
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    # strip any markdown the model produces despite instructions
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)   # **bold** / *italic*
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # ## headers
    return text


# ── Output ───────────────────────────────────────────────────────────────────

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    padding: 40px 20px;
}
.card {
    background: #fff;
    border-radius: 12px;
    padding: 28px 32px;
    max-width: 680px;
    margin: 0 auto 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.card h2 {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: #6e6e73;
    margin-bottom: 14px;
}
.briefing-text { font-size: 15px; line-height: 1.7; white-space: pre-wrap; }
ul { padding-left: 18px; }
li { font-size: 14px; line-height: 1.6; margin: 5px 0; }
b  { font-weight: 600; }
i  { color: #6e6e73; font-style: normal; }
.ts { font-size: 12px; color: #aeaeb2; text-align: center; margin-top: 8px; }
"""


def push_to_browser(html: str, path: str = "/tmp/tumsol_briefing.html") -> None:
    from datetime import datetime
    ts = datetime.now().strftime("%a %d %b %Y, %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head>"
            f"<body>{html}<p class='ts'>Updated {ts}</p></body></html>"
        )
    subprocess.run(["open", path])


if __name__ == "__main__":
    from storage.db import Database
    db = Database()

    briefing  = build_briefing(db)

    print("\n--- BRIEFING ---")
    print(briefing)
    # push_to_notes(full_note)
    # print("Note pushed to Apple Notes.")
