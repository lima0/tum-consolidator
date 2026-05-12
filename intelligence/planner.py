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

Knowledge gaps (prerequisites required but not yet covered):
{gaps}

Write a concise daily briefing. Use markdown: **bold** for course names and deadlines, bullet lists for tasks, `code` for time blocks. Be specific and direct.

Structure:
**Situation** — one sentence on today's priority and why.
**Schedule** — time-blocked study plan using estimated_minutes. Be arithmetic: if 4h free, allocate ≤240min total.
**Deadlines** — flag anything overdue or due within 24h in bold.
**Knowledge gaps** — if any gaps exist, call out specifically which topics to review before tackling upcoming materials. Skip this section if no gaps.
**Action list** — 3-5 concrete next steps.
"""


# ── Plain-text helpers for LLM context ──────────────────────────────────────

def _deadlines_text(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points, status
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now', 'localtime') AND datetime('now', 'localtime', '+14 days')
        ORDER BY due
    """).fetchall()
    if not rows:
        return "None."
    lines = []
    for r in rows:
        due       = datetime.fromisoformat(r["due"]) if r["due"] else None
        due_str   = due.strftime("%a %d %b %H:%M") if due else "?"
        if r["score"] is not None and r["max_points"]:
            earned = round(r["score"] / 100 * r["max_points"], 2)
            score_str = f" ({earned}/{r['max_points']}pts)"
        else:
            score_str = ""
        status    = f" [{r['status']}]" if r["status"] else ""
        lines.append(f"- {r['course']}: {r['title']}{score_str}{status} — due {due_str}")
    return "\n".join(lines)


def _calendar_text(db) -> str:
    tomorrow_end = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 00:00")
    rows = db.conn.execute("""
        SELECT title, release, extra, event_type
        FROM events
        WHERE source = 'calendar'
          AND release BETWEEN datetime('now', 'localtime', 'start of day') AND ?
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


# ── Gap detection ────────────────────────────────────────────────────────────

def _gaps_text(db) -> str:
    rows = db.conn.execute("""
        SELECT summary_json FROM documents
        WHERE processed_at IS NOT NULL AND summary_json IS NOT NULL
        ORDER BY first_seen DESC LIMIT 30
    """).fetchall()
    prereqs: list[str] = []
    covered: list[str] = []
    for r in rows:
        try:
            s = json.loads(r["summary_json"]) if r["summary_json"] else {}
        except (json.JSONDecodeError, TypeError):
            continue
        prereqs.extend(s.get("prerequisites", []))
        covered.extend(s.get("topics", []))
    if not prereqs:
        return "None detected."
    prereqs_unique = sorted(set(prereqs))
    covered_unique = sorted(set(covered))
    return (
        "Prerequisites required by course materials:\n"
        + "\n".join(f"- {p}" for p in prereqs_unique)
        + "\n\nTopics covered in materials you have:\n"
        + "\n".join(f"- {t}" for t in covered_unique)
    )


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
        gaps=_gaps_text(db),
    )
    client   = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


def _strip_markdown(text: str) -> str:
    text = re.sub(r'\*+([^*]+)\*+', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    return text


# ── HTML data cards ──────────────────────────────────────────────────────────

def _deadlines_html(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points, status, url
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now', 'localtime') AND datetime('now', 'localtime', '+14 days')
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
        if r["score"] is not None and r["max_points"]:
            earned = round(r["score"] / 100 * r["max_points"], 2)
            score_str = f'<span class="muted">{earned}/{r["max_points"]}pts</span> '
        else:
            score_str = ""
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
        lp = r["local_path"]
        if lp and Path(lp).exists():
            from urllib.parse import quote
            path_attr = f'href="/file?path={quote(lp)}" target="_blank"'
        else:
            path_attr = ""
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

def push_to_browser(html: str, path: str = "/tmp/tumsol_briefing.html") -> None:
    css = (Path(__file__).parent.parent / "static" / "style.css").read_text()
    ts  = datetime.now().strftime("%a %d %b %Y, %H:%M")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"<html><head><meta charset='utf-8'>"
            f"<script src='https://cdn.jsdelivr.net/npm/marked/marked.min.js'></script>"
            f"<style>{css}</style></head>"
            f"<body>{html}<p class='ts'>Updated {ts}</p>"
            f"<script>document.querySelectorAll('.briefing-md').forEach(el=>{{el.innerHTML=marked.parse(el.dataset.md);}});</script>"
            f"</body></html>"
        )
    subprocess.run(["open", path])


if __name__ == "__main__":
    from storage.db import Database
    db = Database()

    briefing  = build_briefing(db)

    print("\n--- BRIEFING ---")
    print(briefing)