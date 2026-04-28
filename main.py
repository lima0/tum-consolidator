"""
TUMsolidator — consolidates Artemis, Moodle, and Campus Calendar into an
LLM-powered study assistant.
"""

import json
import logging
import os
import re
import sys
import time
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.INFO,
    format="%(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

import connectors.artemis as artemis
import connectors.campuscalendar as calendar_conn
import connectors.moodle as moodle
import db

RESOURCES_DIR     = Path("resources")
FILE_ID_CACHE     = Path(".file_id_cache.json")
AGENT_MEMORY_FILE = Path("agent_memory.md")
FILES_BETA        = "files-api-2025-04-14"

AGENT_MEMORY_KEEP = 7
MAX_HISTORY = 20

client = anthropic.Anthropic()


# ---------------------------------------------------------------------------
# State helpers — now SQLite-backed
# ---------------------------------------------------------------------------

def _artemis_snapshot(data: list) -> dict:
    snapshot = {}
    for course in data:
        for ex in course["exercises"]:
            key = f"{course['shortName']}/{ex['id']}"
            snapshot[key] = {
                "title": ex["title"],
                "status": ex["status"],
                "score": ex["score"],
                "due": ex["due"],
            }
    return snapshot


def _load_artemis_snapshot() -> dict:
    raw = db.meta_get("artemis_snapshot")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {}


def _save_artemis_snapshot(snapshot: dict) -> None:
    db.meta_set("artemis_snapshot", json.dumps(snapshot))


def _load_known_files() -> set[str]:
    raw = db.meta_get("known_files")
    if raw:
        try:
            return set(json.loads(raw))
        except json.JSONDecodeError:
            pass
    return set()


def _save_known_files(files: set[str]) -> None:
    db.meta_set("known_files", json.dumps(sorted(files)))


def _load_deadline_reminders() -> dict:
    raw = db.meta_get("deadline_reminders")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {}


def _save_deadline_reminders(reminded: dict) -> None:
    db.meta_set("deadline_reminders", json.dumps(reminded))


def _compute_diff(old: dict, new: dict) -> list[str]:
    lines = []
    for key, nv in new.items():
        ov = old.get(key)
        title = nv["title"]
        if ov is None:
            lines.append(f"NEW exercise: {title} (due {nv['due']})")
            continue
        if ov["score"] != nv["score"] and nv["score"] is not None:
            lines.append(f"Score update: {title} — {ov['score']} → {nv['score']}%")
        if ov["status"] != nv["status"]:
            lines.append(f"Status change: {title} — {ov['status']} → {nv['status']}")
    return lines


# ---------------------------------------------------------------------------
# Write Artemis events to DB
# ---------------------------------------------------------------------------

def _sync_artemis_to_db(data: list) -> None:
    """Upsert Artemis exercise deadlines into the events table."""
    for course in data:
        course_code = course.get("shortName")
        for ex in course["exercises"]:
            eid = f"artemis:{ex['id']}"
            db.upsert_event(
                id=eid,
                source="artemis",
                course_code=course_code,
                event_type="deadline",
                title=ex["title"],
                occurs_at=ex.get("due"),
                raw_json=json.dumps(ex),
            )


# ---------------------------------------------------------------------------
# Files API — upload PDFs once, cache IDs locally by path + mtime
# ---------------------------------------------------------------------------

def _load_file_id_cache() -> dict:
    try:
        return json.loads(FILE_ID_CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_file_id_cache(cache: dict) -> None:
    FILE_ID_CACHE.write_text(json.dumps(cache, indent=2))


def _scan_local_pdfs() -> list[str]:
    if not RESOURCES_DIR.exists():
        return []
    return sorted(str(p.relative_to(RESOURCES_DIR)) for p in RESOURCES_DIR.rglob("*.pdf"))


def _build_file_index() -> dict:
    cache = _load_file_id_cache()
    if not cache:
        return {}

    try:
        live_ids = {f.id for f in client.beta.files.list(betas=[FILES_BETA]).data}
    except Exception as exc:
        log.warning("Could not validate Files API cache: %s — trusting cache", exc)
        return {rel: e["file_id"] for rel, e in cache.items() if e.get("file_id")}

    index = {}
    evicted = []
    for rel, entry in cache.items():
        fid = entry.get("file_id")
        if fid and fid in live_ids:
            index[rel] = fid
        else:
            evicted.append(rel)

    if evicted:
        log.info("Evicted %d expired file(s) from cache", len(evicted))
        for rel in evicted:
            del cache[rel]
        _save_file_id_cache(cache)

    return index


def _upload_file(rel: str, file_index: dict) -> str | None:
    pdf = RESOURCES_DIR / rel
    if not pdf.exists():
        return None

    cache = _load_file_id_cache()
    mtime = int(pdf.stat().st_mtime)

    print(f"  Uploading {rel}…", flush=True)
    for attempt in range(5):
        try:
            safe_name = re.sub(r'[^\w\-. ()]', '_', pdf.name)
            with open(pdf, "rb") as f:
                meta = client.beta.files.upload(
                    file=(safe_name, f, "application/pdf"),
                    betas=[FILES_BETA],
                )
            file_index[rel] = meta.id
            cache[rel] = {"file_id": meta.id, "mtime": mtime}
            _save_file_id_cache(cache)
            return meta.id
        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            print(f"  Rate limited — waiting {wait}s…", flush=True)
            time.sleep(wait)
        except Exception as exc:
            log.warning("Failed to upload %s: %s", rel, exc)
            return None

    return None


# ---------------------------------------------------------------------------
# Background Moodle sync
# ---------------------------------------------------------------------------

_moodle_result: dict = {"status": "pending", "summary": None, "error": None}
_moodle_event = threading.Event()


def _moodle_sync_worker() -> None:
    try:
        username = os.environ["TUM_USERNAME"]
        password = os.environ["TUM_PASSWORD"]
        sess = moodle.get_session(username, password)
        sesskey, userid = moodle.get_sesskey_and_userid(sess)
        courses = moodle.get_enrolled_courses(sess, sesskey, userid)
        moodle.download_course_files(sess, courses, output_dir=str(RESOURCES_DIR))
        names = [c.get("fullname") or c.get("shortname", "?") for c in courses]
        _moodle_result["summary"] = f"Synced {len(courses)} courses: {', '.join(names)}"
        _moodle_result["status"] = "done"
        log.info("Moodle sync complete.")
    except Exception as e:
        _moodle_result["error"] = str(e)
        _moodle_result["status"] = "error"
        log.warning("Moodle sync failed: %s", e)
    finally:
        _moodle_event.set()


def _await_moodle() -> str:
    _moodle_event.wait()
    if _moodle_result["status"] == "error":
        return f"Moodle sync failed: {_moodle_result['error']}"
    return _moodle_result["summary"] or "Moodle sync returned no data."


# ---------------------------------------------------------------------------
# On-demand refresh helpers
# ---------------------------------------------------------------------------

def _refresh_artemis() -> str:
    try:
        sess = artemis._make_session()
        if not artemis._ensure_authenticated(sess):
            return "[Artemis: authentication failed]"
        data = artemis.get_full_dashboard_data(sess)
        if not data:
            return "[Artemis: no data returned]"
        return artemis.format_for_llm(data)
    except Exception as e:
        return f"[Artemis refresh failed: {e}]"


def _refresh_calendar() -> str:
    try:
        events = calendar_conn.fetch_events(days=14)
        return calendar_conn.format_for_llm(events, 14)
    except Exception as e:
        return f"[Calendar refresh failed: {e}]"


# ---------------------------------------------------------------------------
# Tools (chat mode)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "sync_moodle",
        "description": "Wait for background Moodle sync. SLOW — only call when user asks about Moodle/sync status.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_artemis",
        "description": "Fetch live assignment/score data. Call when user wants current scores or recent submissions.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "refresh_calendar",
        "description": "Fetch live schedule. Call when user asks about upcoming events.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_course_materials",
        "description": "List available PDF files. Call before read_course_material to find the correct path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "course": {
                    "type": "string",
                    "description": "Optional course short name filter, e.g. 'EIST26'",
                }
            },
        },
    },
    {
        "name": "read_course_material",
        "description": "Read a course material PDF. Use list_course_materials first to find the exact path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from list_course_materials, e.g. 'EIST26/1815/L01 Lecture Slides.pdf'",
                }
            },
            "required": ["path"],
        },
    },
]


def _handle_tool(name: str, tool_input: dict, file_index: dict) -> list:
    if name == "sync_moodle":
        return [{"type": "text", "text": _await_moodle()}]
    if name == "refresh_artemis":
        return [{"type": "text", "text": _refresh_artemis()}]
    if name == "refresh_calendar":
        return [{"type": "text", "text": _refresh_calendar()}]
    if name == "list_course_materials":
        course_filter = tool_input.get("course", "").lower()
        pdfs = _scan_local_pdfs()
        if course_filter:
            pdfs = [p for p in pdfs if course_filter in p.lower()]
        return [{"type": "text", "text": "\n".join(pdfs) if pdfs else "(none)"}]
    if name == "read_course_material":
        path = tool_input.get("path", "")
        file_id = file_index.get(path) or _upload_file(path, file_index)
        if not file_id:
            return [{"type": "text", "text": f"File not found: {path!r}. Call list_course_materials to find the correct path."}]
        return [{"type": "document", "source": {"type": "file", "file_id": file_id}, "title": path}]
    return [{"type": "text", "text": f"[Unknown tool: {name}]"}]


# ---------------------------------------------------------------------------
# Context + system prompt
# ---------------------------------------------------------------------------

BRIEFING_PROMPT = """\
Give me a concise daily briefing. Cover:
1. Deadlines in the next 7 days — assignment name, course, due date, current status
2. Any score updates or status changes since last run (if none, skip this section)
3. Today's schedule highlights (if any)
4. priority recommendation for today

Be direct. No filler. Plain text.\
"""

SYSTEM_TEMPLATE = """\
TUM study assistant. Today: {today}.
{changes_block}
{context}
Use list_course_materials then read_course_material to access PDFs.
Be direct. No markdown. Prioritise actionable information.\
"""


def build_context(artemis_data: list) -> str:
    sections = []

    if artemis_data:
        sections.append("=== ASSIGNMENTS ===\n" + artemis.format_for_llm(artemis_data))

    try:
        events = calendar_conn.fetch_events(days=7)
        sections.append("=== SCHEDULE (7d) ===\n" + calendar_conn.format_for_llm(events, 7))
    except Exception as e:
        sections.append(f"=== SCHEDULE ===\n[Error: {e}]")

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# History management
# ---------------------------------------------------------------------------

def _strip_document_blocks(history: list) -> list:
    for msg in history:
        if msg["role"] != "user" or not isinstance(msg["content"], list):
            continue
        for block in msg["content"]:
            if block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if not isinstance(content, list):
                continue
            block["content"] = [
                {"type": "text", "text": f"[PDF '{item.get('title', '')}' read earlier — call read_course_material again if needed]"}
                if item.get("type") == "document" else item
                for item in content
            ]
    return history


def _trim_history(history: list) -> list:
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history.pop(0)
    return _strip_document_blocks(history)


# ---------------------------------------------------------------------------
# Agent mode helpers
# ---------------------------------------------------------------------------

def _detect_new_files() -> list[str]:
    current  = set(_scan_local_pdfs())
    known    = _load_known_files()
    new      = sorted(current - known)
    _save_known_files(current)
    return new


def _load_agent_memory() -> str:
    try:
        text    = AGENT_MEMORY_FILE.read_text().strip()
        entries = [e for e in text.split("\n## ") if e.strip()]
        recent  = entries[-AGENT_MEMORY_KEEP:]
        return ("## " + "\n## ".join(recent)) if recent else ""
    except FileNotFoundError:
        return ""


def _append_agent_memory(text: str) -> None:
    with open(AGENT_MEMORY_FILE, "a") as f:
        f.write(f"\n## {date.today()}\n{text.strip()}\n")


def _summarize_pdf(path: str, file_index: dict) -> str:
    file_id = file_index.get(path) or _upload_file(path, file_index)
    if not file_id:
        return f"Could not read {path!r}."
    response = client.beta.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system="Summarize this course material in 3 bullet points: key concepts, what to know, exam relevance. Be concise.",
        messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "file", "file_id": file_id}},
        ]}],
        betas=[FILES_BETA],
    )
    return next((b.text for b in response.content if hasattr(b, "text")), "")


AGENT_TOOLS = [
    {
        "name": "notify",
        "description": "Push urgent notification. Use for: due <24h unsubmitted, new grade, build failed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message":  {"type": "string"},
                "subtitle": {"type": "string"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "create_reminder",
        "description": "Create macOS Reminder. Use for assignments due 24–72h away, not yet submitted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "due":   {"type": "string", "description": "YYYY-MM-DD HH:MM"},
            },
            "required": ["title", "due"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a TUM SoSe26 calendar event. Use for deadlines and study blocks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":       {"type": "string"},
                "start":       {"type": "string", "description": "ISO 8601, e.g. 2026-04-29T21:59:00+02:00"},
                "end":         {"type": "string", "description": "ISO 8601"},
                "notes":       {"type": "string", "description": "Topics + PDF path"},
                "external_id": {"type": "string", "description": "Idempotency key, e.g. artemis:12345"},
            },
            "required": ["title", "start", "end", "external_id"],
        },
    },
    {
        "name": "summarize_material",
        "description": "Read a new PDF and return a 3-bullet summary. Call for each new file detected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path under resources/"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_digest",
        "description": "Record today's study priority. Call exactly once, last.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]

AGENT_SYSTEM = """\
You are an autonomous TUM study agent. Today: {today}.

Review the student's assignments, schedule, and any new materials. Take targeted actions.

Rules:
- notify: due <24h unsubmitted, new grade released, build failed
- create_reminder: due 24–72h, not submitted, not already reminded
- create_calendar_event: create deadline events for all open assignments; external_id = "artemis:<exercise_id>"
- summarize_material: call for every new PDF listed below
- write_digest: always call last — one paragraph, today's top priority and why

{new_files_block}{memory_block}{context}\
"""


def _handle_agent_tool(name: str, tool_input: dict, file_index: dict, digest: list) -> str:
    from actions import notify as notify_mod
    import actions.calendar_writer as cal

    if name == "notify":
        notify_mod.notify("TUMsolidator", tool_input["message"], tool_input.get("subtitle", ""))
        return "Notification sent."

    if name == "create_reminder":
        title = tool_input["title"]
        due   = tool_input["due"]
        ok    = notify_mod.add_reminder(title, due)
        if ok:
            # Log to actions_taken so --sync can pick up completion
            db.log_action(
                action_type="reminder",
                external_id=f"reminder:{title}",
                payload={"title": title, "due": due},
            )
        return "Reminder created." if ok else "Reminder creation failed."

    if name == "create_calendar_event":
        result = cal.create_event(
            title=tool_input["title"],
            start=tool_input["start"],
            end=tool_input["end"],
            notes=tool_input.get("notes", ""),
            external_id=tool_input["external_id"],
        )
        return result

    if name == "summarize_material":
        from intelligence.topics import topics_for_pdf
        rel     = tool_input["path"]
        course  = Path(rel).parts[0] if rel else ""
        summary = _summarize_pdf(rel, file_index)
        topics  = topics_for_pdf(course, summary)

        notify_mod.notify("New material", summary.split("\n")[0][:100], subtitle=course)

        db.upsert_document(
            id=rel,
            source="moodle",
            course_code=course,
            doc_type="lecture",
            title=Path(rel).stem,
            local_path=str(RESOURCES_DIR / rel),
        )
        result = summary
        if topics:
            result += f"\nTopics: {topics}"
        return result

    if name == "write_digest":
        digest.append(tool_input["text"])
        return "Digest recorded."

    return f"Unknown tool: {name}"


def run_agent() -> None:
    print("Agent: fetching context…", flush=True)

    artemis_data = None
    try:
        sess = artemis._make_session()
        if artemis._ensure_authenticated(sess):
            artemis_data = artemis.get_full_dashboard_data(sess)
    except Exception as e:
        log.warning("Artemis error: %s", e)

    new_files = _detect_new_files()
    if artemis_data:
        snap = _artemis_snapshot(artemis_data)
        _save_artemis_snapshot(snap)
        _sync_artemis_to_db(artemis_data)

    file_index = _build_file_index()
    context    = build_context(artemis_data or [])
    memory     = _load_agent_memory()

    new_files_block = (
        "New files since last run:\n" + "\n".join(f"  {f}" for f in new_files) + "\n\n"
    ) if new_files else ""
    memory_block = f"Recent memory:\n{memory}\n\n" if memory else ""

    system = AGENT_SYSTEM.format(
        today=date.today(),
        new_files_block=new_files_block,
        memory_block=memory_block,
        context=context,
    )

    messages = [{"role": "user", "content": "Run your study check and take appropriate actions."}]
    digest: list[str] = []

    print("Agent: reasoning…", flush=True)
    while True:
        response = client.beta.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=messages,
            tools=AGENT_TOOLS,
            betas=[FILES_BETA],
        )

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            print(f"  [{block.name}] {next(iter(block.input.values()), '')!r:.60}", flush=True)
            result = _handle_agent_tool(block.name, block.input, file_index, digest)
            results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     [{"type": "text", "text": result}],
            })
        messages.append({"role": "user", "content": results})

    if digest:
        print(f"\n=== Agent Digest ===\n{digest[-1]}\n")
        _append_agent_memory(digest[-1])
    else:
        print("Agent: no digest produced.")

    try:
        from intelligence.dashboard import write_dashboard
        write_dashboard()
    except Exception as e:
        log.warning("Dashboard write failed: %s", e)


# ---------------------------------------------------------------------------
# Check mode — stateless diff + notifications + calendar events
# ---------------------------------------------------------------------------

def run_check() -> None:
    from actions import notify
    import actions.calendar_writer as cal

    print("Checking Artemis…", flush=True)
    artemis_data = None
    try:
        sess = artemis._make_session()
        if artemis._ensure_authenticated(sess):
            artemis_data = artemis.get_full_dashboard_data(sess)
    except Exception as e:
        log.warning("Artemis error during check: %s", e)

    if not artemis_data:
        print("No Artemis data.")
        return

    new_snap = _artemis_snapshot(artemis_data)
    old_snap = _load_artemis_snapshot()
    changes  = _compute_diff(old_snap, new_snap)
    _save_artemis_snapshot(new_snap)
    _sync_artemis_to_db(artemis_data)

    if changes:
        print(f"{len(changes)} change(s):")
        for change in changes:
            print(f"  • {change}")
            notify.notify("TUMsolidator", change)
    else:
        print("No changes.")

    # Deadline proximity → Reminder + Calendar event
    reminded  = _load_deadline_reminders()
    now       = datetime.now()
    threshold = now + timedelta(hours=48)
    new_reminders = 0

    for course in artemis_data:
        for ex in course["exercises"]:
            if not ex["due"]:
                continue
            key = f"{course['shortName']}/{ex['id']}"

            if ex["status"] in ("SUBMITTED", "FINISHED"):
                continue
            try:
                due_dt = datetime.strptime(ex["due"], "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            # Calendar event for ALL open deadlines (idempotent)
            cal.create_event(
                title=f"[{course['shortName']}] {ex['title']}",
                start=due_dt.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
                end=(due_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+02:00"),
                notes=f"Course: {course['title']}\nStatus: {ex['status']}\nPoints: {ex['max_points']}",
                external_id=f"artemis:{ex['id']}",
            )

            # Reminder only within 48h window
            if key in reminded:
                continue
            if now < due_dt <= threshold:
                label = f"[{course['shortName']}] {ex['title']} — due {ex['due']}"
                print(f"  ⏰ Reminder: {label}")
                if notify.add_reminder(label, ex["due"]):
                    db.log_action(
                        action_type="reminder",
                        external_id=f"reminder:{key}",
                        payload={"title": label, "due": ex["due"], "course": course["shortName"]},
                    )
                    reminded[key] = True
                    new_reminders += 1
                    notify.notify("TUMsolidator", f"Reminder set: {ex['title']}", subtitle=course["shortName"])

    if new_reminders == 0 and not changes:
        print("No upcoming deadlines within 48h.")

    _save_deadline_reminders(reminded)


# ---------------------------------------------------------------------------
# Sync mode — read Reminders completion back into actions_taken
# ---------------------------------------------------------------------------

def run_sync() -> None:
    from actions import notify as notify_mod

    print("Syncing completion state from Reminders…", flush=True)
    completed = notify_mod.read_completed_reminders("tasks")
    print(f"  Found {len(completed)} completed reminder(s).")

    matched = 0
    for title in completed:
        # Match by title substring against actions_taken payload
        rows = db.get_db().execute(
            "SELECT id, external_id, payload FROM actions_taken WHERE action_type='reminder' AND user_response IS NULL"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            stored_title = payload.get("title", "")
            if title.strip() in stored_title or stored_title in title.strip():
                db.mark_completed(row["external_id"])
                print(f"  Marked completed: {stored_title[:60]}")
                matched += 1
                break

    print(f"  {matched} action(s) marked completed.")


# ---------------------------------------------------------------------------
# Briefing mode
# ---------------------------------------------------------------------------

def run_briefing(system_block: list) -> None:
    print("\n=== Daily Briefing ===\n", flush=True)
    response = client.beta.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_block,
        messages=[{"role": "user", "content": BRIEFING_PROMPT}],
        betas=[FILES_BETA],
    )
    reply = next((b.text for b in response.content if hasattr(b, "text")), "")
    print(reply)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "--check" in sys.argv:
        run_check()
        return

    if "--agent" in sys.argv:
        run_agent()
        return

    if "--sync" in sys.argv:
        run_sync()
        return

    if "--plan" in sys.argv:
        from intelligence.planner import run as run_plan
        run_plan()
        return

    if "--dashboard" in sys.argv:
        from intelligence.dashboard import write_dashboard
        write_dashboard()
        return

    # Start Moodle sync immediately in background
    threading.Thread(target=_moodle_sync_worker, daemon=True, name="moodle-sync").start()
    log.info("Moodle sync running in background.")

    print("Fetching Artemis + calendar…", flush=True)
    artemis_data = None
    try:
        sess = artemis._make_session()
        if artemis._ensure_authenticated(sess):
            artemis_data = artemis.get_full_dashboard_data(sess)
    except Exception as e:
        log.warning("Artemis error: %s", e)

    # State diff
    changes: list[str] = []
    if artemis_data:
        new_snap = _artemis_snapshot(artemis_data)
        old_snap = _load_artemis_snapshot()
        changes = _compute_diff(old_snap, new_snap)
        _save_artemis_snapshot(new_snap)
        _sync_artemis_to_db(artemis_data)

    changes_block = ""
    if changes:
        changes_block = (
            "\n=== CHANGES SINCE LAST RUN ===\n"
            + "\n".join(f"• {c}" for c in changes)
            + "\n"
        )

    print("Validating file cache…", flush=True)
    file_index = _build_file_index()

    context = build_context(artemis_data or [])
    system_prompt = SYSTEM_TEMPLATE.format(
        today=date.today(),
        changes_block=changes_block,
        context=context,
    )

    system_block = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    if "--brief" in sys.argv:
        run_briefing(system_block)
        return

    history: list = []

    print("\n=== TUMsolidator ===")
    if changes:
        print(f"  {len(changes)} change(s) since last run:")
        for c in changes:
            print(f"  • {c}")
    print("Type your question (empty line or Ctrl+C to quit).\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not user_input:
            break

        history.append({"role": "user", "content": user_input})

        while True:
            response = client.beta.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system_block,
                messages=history,
                tools=TOOLS,
                betas=[FILES_BETA],
            )

            if response.stop_reason == "tool_use":
                history.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        args_str = ", ".join(f"{k}={v!r}" for k, v in block.input.items())
                        print(f"  [→ {block.name}({args_str})]", flush=True)
                        result = _handle_tool(block.name, block.input, file_index)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                history.append({"role": "user", "content": tool_results})
                continue

            reply = next((b.text for b in response.content if hasattr(b, "text")), "")
            history.append({"role": "assistant", "content": reply})
            print(f"\nAssistant: {reply}\n")
            break

        history = _trim_history(history)


if __name__ == "__main__":
    main()
