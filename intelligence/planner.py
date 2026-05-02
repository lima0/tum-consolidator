import json
import subprocess
from datetime import datetime, timedelta

NOTE_NAME = "TUMsolidator"


def build_note(db) -> str:
    now = datetime.now()
    sections = []
    for section in (_deadlines_section(db), _calendar_section(db), _materials_section(db)):
        if section:
            sections.append(section)
    [f"<p><i>Updated {now.strftime('%a %d %b %Y, %H:%M')}</i></p>"]
    return "\n".join(sections)


def _deadlines_section(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now') AND datetime('now', '+7 days')
        ORDER BY due
    """).fetchall()
    if not rows:
        return ""
    items = []
    for r in rows:
        due = datetime.fromisoformat(r["due"]) if r["due"] else None
        due_str = due.strftime("%a %d %b %H:%M") if due else "?"
        score_str = f" [{r['score']}/{r['max_points']}]" if r["score"] is not None else ""
        items.append(f"<li><b>{r['course']}</b>   {r['title']}{score_str} — {due_str}</li>")
    return f"<h2>Deadlines (next 7 days)</h2><ul>{''.join(items)}</ul>"


def _calendar_section(db) -> str:
    tomorrow_end = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d 00:00")
    rows = db.conn.execute("""
        SELECT title, release, extra
        FROM events
        WHERE source = 'calendar'
          AND release BETWEEN datetime('now', 'start of day') AND ?
        ORDER BY release
    """, (tomorrow_end,)).fetchall()
    if not rows:
        return ""
    items = []
    for r in rows:
        start = datetime.fromisoformat(r["release"]) if r["release"] else None
        day_str = start.strftime("%a %H:%M") if start else "?"
        extra = json.loads(r["extra"]) if r["extra"] else {}
        loc = f" @ {extra['location']}" if extra.get("location") else ""
        items.append(f"<li><b>{day_str}</b>   {r['title']}{loc}</li>")
    return f"<h2>Today &amp; Tomorrow</h2><ul>{''.join(items)}</ul>"


def _materials_section(db) -> str:
    rows = db.conn.execute("""
        SELECT course, title, summary_json
        FROM documents
        WHERE first_seen > datetime('now', '-48 hours')
          AND processed_at IS NOT NULL
        ORDER BY first_seen DESC
        LIMIT 15
    """).fetchall()
    if not rows:
        return ""
    items = []
    for r in rows:
        s = json.loads(r["summary_json"]) if r["summary_json"] else {}
        mins = s.get("estimated_minutes")
        topics = ", ".join(s.get("topics", [])[:4])
        time_str = f" ~{mins}min" if mins else ""
        topic_line = f"<br><i>{topics}</i>" if topics else ""
        items.append(f"<li><b>{r['course']}</b> - {r['title']}{time_str}{topic_line}</li>")
    return f"<h2>New Materials (48h)</h2><ul>{''.join(items)}</ul>"

# TODO fix applescript 
def push_to_notes(text: str, note_name: str = NOTE_NAME) -> None:
    tmp = "/tmp/tumsol_note.txt"
    with open(tmp, "w") as f:
        f.write(text)
    script = f"""
tell application "Notes"
    set noteBody to read POSIX file "{tmp}"
    set matchingNotes to every note of default account whose name is "{note_name}"
    if (count of matchingNotes) > 0 then
        set properites of item 1 of matchingNotes to {{name:"{note_name}", body:noteBody}}
    else
        make new note at default account with properties {{name:"{note_name}", body:noteBody}}
    end if
end tell
"""
    subprocess.run(["osascript", "-e", script], check=True)


if __name__ == "__main__":
    from storage.db import Database
    db = Database()
    note = build_note(db)
    print(note)
    push_to_notes(note)
