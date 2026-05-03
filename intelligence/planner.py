import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import Anthropic

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

Write a concise daily briefing (5-8 sentences). Prioritize what to work on and WHY. \
The WHY is important. It's how the student should orient themselves.
Be specific about time estimates where available. Flag anything critical or overdue. \
Don't list everything — reason about what matters most given the schedule and workload.
Output Format: Plain text only. No markdown. No asterisks, no hashes.
Bullet points and numbered lists are okay. 

1. One sentence summary of today's situation.
2. Concrete study schedule for TODAY: assign specific materials to time blocks based on estimated_minutes, difficulty, and proximity to deadlines. Be arithmetic — if student has 4h free, allocate up to 240min total. 
3. Flag anything overdue or due tomorrow.

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


def _effective_date(r) -> datetime | None:
    s = json.loads(r["summary_json"]) if r["summary_json"] else {}
    release = s.get("release_date")
    field = release if release else r["first_seen"]
    if field:
        try:
            return datetime.fromisoformat(str(field)[:10])
        except ValueError:
            pass
    return None


def _recent_materials(db):
    rows = db.conn.execute("""
        SELECT course, title, summary_json, local_path, updated_at, first_seen
        FROM documents
        WHERE processed_at IS NOT NULL
        ORDER BY first_seen DESC
        LIMIT 60
    """).fetchall()
    return sorted(
        rows,
        key=lambda r: _effective_date(r) or datetime.min,
        reverse=True,
    )[:10]


def _materials_text(db) -> str:
    rows = _recent_materials(db)
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


# ── HTML data cards ──────────────────────────────────────────────────────────

def _deadlines_html(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points, status, url
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now', '-1 day') AND datetime('now', '+7 days')
        ORDER BY due
    """).fetchall()
    if not rows:
        return ""
    now = datetime.now()
    items = []
    for r in rows:
        due = datetime.fromisoformat(r["due"]) if r["due"] else None
        delta = (due - now).total_seconds() if due else None
        if delta is not None and delta < 0:
            urgency = "overdue"
        elif delta is not None and delta < 86400:
            urgency = "soon"
        else:
            urgency = ""
        due_str = due.strftime("%a %d %b %H:%M") if due else "?"
        score_str = f'<span class="muted">{r["score"]}/{r["max_points"]}</span> ' if r["score"] is not None else ""
        status_badge = f'<span class="badge badge-{(r["status"] or "").lower()}">{r["status"]}</span> ' if r["status"] else ""
        title_html = (
            f'<a class="row-title-link" href="{r["url"]}" target="_blank">{r["title"]}</a>'
            if r["url"] else f'<span class="row-title">{r["title"]}</span>'
        )
        items.append(
            f'<div class="row">'
            f'<span class="pill">{r["course"]}</span>'
            f'{title_html}'
            f'<span class="row-meta">{score_str}{status_badge}'
            f'<span class="due {urgency}">{due_str}</span></span>'
            f'</div>'
        )
    return (
        "<div class='card'>"
        "<h2>Upcoming Deadlines</h2>"
        + "".join(items) +
        "</div>"
    )


def _materials_html(db) -> str:
    rows = [r for r in _recent_materials(db) if r["summary_json"]]
    if not rows:
        return ""
    items = []
    for r in rows:
        s = json.loads(r["summary_json"])
        diff = s.get("difficulty", "")
        mins = s.get("estimated_minutes", "")
        topics = ", ".join(s.get("topics", [])[:4])
        mins_str = f'<span class="muted">~{mins}min</span>' if mins else ""
        abs_path = Path(r["local_path"]).resolve() if r["local_path"] else None
        path_attr = f'href="file://{abs_path}" target="_blank"' if abs_path and abs_path.exists() else ""
        title_html = f'<a class="row-title-link" {path_attr}>{r["title"]}</a>' if path_attr else f'<span class="row-title">{r["title"]}</span>'
        items.append(
            f'<div class="row">'
            f'<span class="pill">{r["course"]}</span>'
            f'{title_html}'
            f'<span class="row-meta">{mins_str} <span class="badge badge-{diff}">{diff}</span></span>'
            f'<div class="topics">{topics}</div>'
            f'</div>'
        )
    return (
        "<div class='card'>"
        "<h2>Recent Materials (±7 days)</h2>"
        + "".join(items) +
        "</div>"
    )


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
    max-width: 740px;
    margin: 0 auto 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,.08);
}
.card h2 {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .08em;
    color: #aeaeb2;
    margin-bottom: 16px;
}
.briefing-text { font-size: 15px; line-height: 1.75; white-space: pre-wrap; }
.row {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 6px;
    padding: 9px 0;
    border-bottom: 1px solid #f2f2f7;
}
.row:last-child { border-bottom: none; }
.pill {
    font-size: 10px;
    font-weight: 600;
    background: #f2f2f7;
    color: #6e6e73;
    padding: 2px 7px;
    border-radius: 20px;
    white-space: nowrap;
    flex-shrink: 0;
}
.row-title { font-size: 14px; font-weight: 500; flex: 1; min-width: 0; }
.row-title-link { font-size: 14px; font-weight: 500; flex: 1; min-width: 0; color: #0071e3; text-decoration: none; }
.row-title-link:hover { text-decoration: underline; }
.row-meta { font-size: 13px; color: #6e6e73; white-space: nowrap; display: flex; gap: 5px; align-items: center; }
.topics { font-size: 12px; color: #aeaeb2; width: 100%; padding-left: 2px; }
.muted { color: #aeaeb2; font-size: 12px; }
.due { font-size: 13px; }
.due.overdue { color: #ff3b30; font-weight: 600; }
.due.soon    { color: #ff9500; font-weight: 600; }
.badge {
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: .04em;
}
.badge-easy        { background: #d1f5d3; color: #1c7a30; }
.badge-medium      { background: #fff3d1; color: #8a5e00; }
.badge-hard        { background: #fde8e8; color: #c0392b; }
.badge-submitted   { background: #d1f5d3; color: #1c7a30; }
.badge-finished    { background: #d1f5d3; color: #1c7a30; }
.badge-started     { background: #fff3d1; color: #8a5e00; }
.badge-not_started { background: #f2f2f7; color: #6e6e73; }
.badge-overdue     { background: #fde8e8; color: #c0392b; }
.ts { font-size: 11px; color: #c7c7cc; text-align: center; margin-top: 4px; }
"""


def push_to_browser(html: str, path: str = "/tmp/tumsol_briefing.html") -> None:
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