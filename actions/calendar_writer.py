"""
Apple Calendar writer — calls the Swift EventKit helper via subprocess.
All writes are idempotent via external_id.
"""

import json
import logging
import subprocess
from pathlib import Path

import db

log = logging.getLogger(__name__)

_SWIFT_BIN = Path(__file__).resolve().parent.parent / "swift_helper" / "add_event"


def _call_swift(payload: dict) -> dict:
    if not _SWIFT_BIN.exists():
        log.error("Swift helper not found at %s — run: swiftc swift_helper/add_event.swift -o swift_helper/add_event -framework EventKit", _SWIFT_BIN)
        return {"error": "Swift helper binary missing"}

    try:
        result = subprocess.run(
            [str(_SWIFT_BIN)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=15,
        )
        stdout = result.stdout.decode().strip()
        if not stdout:
            err = result.stderr.decode().strip()
            log.warning("Swift helper produced no output. stderr: %s", err)
            return {"error": err or "no output"}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {"error": "Swift helper timed out"}
    except json.JSONDecodeError as e:
        log.warning("Swift helper returned non-JSON: %s", result.stdout.decode()[:200])
        return {"error": f"JSON parse error: {e}"}
    except Exception as e:
        log.warning("Swift helper call failed: %s", e)
        return {"error": str(e)}


def create_event(
    title: str,
    start: str,
    end: str,
    notes: str = "",
    external_id: str | None = None,
    calendar: str = "TUM SoSe26",
) -> str:
    """
    Create a Calendar event. Idempotent — if external_id already logged in
    actions_taken, skips the Swift call and returns early.

    Returns a human-readable result string.
    """
    if external_id:
        # Check actions_taken first to avoid even calling Swift
        row = db.get_db().execute(
            "SELECT id, external_id FROM actions_taken WHERE external_id = ?",
            (f"calendar:{external_id}",),
        ).fetchone()
        if row:
            return f"Calendar event already exists (external_id={external_id})"

    payload = {
        "title":       title,
        "start":       start,
        "end":         end,
        "notes":       notes,
        "calendar":    calendar,
        "external_id": external_id,
    }

    response = _call_swift(payload)

    if "error" in response:
        log.warning("Calendar event creation failed: %s", response["error"])
        return f"Calendar event failed: {response['error']}"

    event_id = response.get("event_id", "")
    created  = response.get("created", False)

    if external_id:
        db.log_action(
            action_type="calendar_event",
            external_id=f"calendar:{external_id}",
            payload={
                "title":    title,
                "start":    start,
                "end":      end,
                "event_id": event_id,
            },
        )

    status = "created" if created else "already existed"
    log.info("Calendar event %s: %s (event_id=%s)", status, title, event_id)
    return f"Calendar event {status}: {title}"
