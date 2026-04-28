"""
Weekly plan synthesizer — Layer 3 LLM task.

Runs Sunday 22:00 (via launchd). Reads open deadlines + completion
history, asks Claude for a structured 7-day study plan, writes each
day's block as a Calendar event and logs to actions_taken.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import db
import connectors.campuscalendar as ical
import actions.calendar_writer as cal_writer

load_dotenv()
log = logging.getLogger(__name__)


PLAN_SYSTEM = """\
You are a TUM study planner. Given open assignment deadlines, past completion
behaviour, and the student's calendar, produce a realistic 7-day study plan.

Rules:
- Prioritise by: (days until due) × (1 / points), lower = higher priority
- Skipped items from previous weeks get +20% priority weight
- Do not schedule on days already blocked >6h in the calendar
- Each block must have a "reason" field — one sentence why this is the priority
- Return ONLY valid JSON — an array of plan items, nothing else

Plan item schema:
{
  "day":          "YYYY-MM-DD",
  "course":       "course_code",
  "topic":        "topic tag or assignment name",
  "action":       "what to do, specific",
  "est_minutes":  integer,
  "reason":       "one sentence"
}
"""


def _open_deadlines(horizon_days: int = 14) -> list[dict]:
    cutoff = (datetime.now() + timedelta(days=horizon_days)).isoformat()
    rows = db.get_db().execute(
        """SELECT id, course_code, title, occurs_at, raw_json
           FROM events
           WHERE source='artemis' AND event_type='deadline'
             AND (occurs_at IS NULL OR occurs_at <= ?)
           ORDER BY occurs_at""",
        (cutoff,),
    ).fetchall()
    result = []
    for row in rows:
        item = {"id": row["id"], "course": row["course_code"], "title": row["title"], "due": row["occurs_at"]}
        try:
            raw = json.loads(row["raw_json"] or "{}")
            item["status"]     = raw.get("status", "")
            item["max_points"] = raw.get("max_points")
        except (json.JSONDecodeError, TypeError):
            pass
        result.append(item)
    return result


def _skipped_items() -> list[dict]:
    rows = db.get_db().execute(
        """SELECT payload FROM actions_taken
           WHERE action_type='plan_item' AND user_response='skipped'
           ORDER BY ts DESC LIMIT 20"""
    ).fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row["payload"]))
        except (json.JSONDecodeError, TypeError):
            pass
    return out


def _calendar_summary(days: int = 7) -> str:
    try:
        events = ical.fetch_events(days=days)
        return ical.format_for_llm(events, days)
    except Exception as e:
        return f"[Calendar unavailable: {e}]"


def synthesize_plan() -> list[dict]:
    """
    Ask the LLM for a 7-day plan. Returns list of plan items.
    Caches result for the week so re-runs are free.
    """
    today     = date.today()
    week_key  = f"plan_{today.isocalendar().year}_W{today.isocalendar().week}"

    cached = db.cache_get(week_key)
    if cached:
        try:
            log.info("planner: returning cached plan for %s", week_key)
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    deadlines = _open_deadlines(14)
    skipped   = _skipped_items()
    schedule  = _calendar_summary(7)

    if not deadlines:
        log.info("planner: no open deadlines — skipping plan synthesis")
        return []

    user_msg = (
        f"Today: {today.isoformat()}\n\n"
        f"Open deadlines (next 14 days):\n{json.dumps(deadlines, indent=2)}\n\n"
        f"Previously skipped items (boost priority):\n{json.dumps(skipped, indent=2)}\n\n"
        f"Current calendar (next 7 days):\n{schedule}\n\n"
        "Produce the 7-day study plan JSON array."
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=PLAN_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = next((b.text for b in response.content if hasattr(b, "text")), "[]")

        import re
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m:
            log.warning("planner: no JSON array in LLM response")
            return []

        plan = json.loads(m.group(0))
        db.cache_set(week_key, json.dumps(plan))
        return plan

    except Exception as e:
        log.error("planner: synthesis failed: %s", e)
        return []


def publish_plan(plan: list[dict]) -> None:
    """
    Write each plan item to Apple Calendar and log to actions_taken.
    Idempotent — items already logged are skipped.
    """
    for item in plan:
        day_str   = item.get("day", "")
        course    = item.get("course", "unknown")
        topic     = item.get("topic", "")
        action    = item.get("action", "")
        minutes   = item.get("est_minutes", 60)
        reason    = item.get("reason", "")

        try:
            start_dt  = datetime.fromisoformat(f"{day_str}T18:00:00+02:00")
        except ValueError:
            log.warning("planner: bad day format %r — skipping", day_str)
            continue

        end_dt   = start_dt + timedelta(minutes=minutes)
        ext_id   = f"plan:{day_str}:{course}:{topic[:20]}"
        notes    = f"{action}\n\nReason: {reason}"

        result = cal_writer.create_event(
            title=f"[{course}] {topic}",
            start=start_dt.isoformat(),
            end=end_dt.isoformat(),
            notes=notes,
            external_id=ext_id,
        )

        db.log_action(
            action_type="plan_item",
            external_id=ext_id,
            payload=item,
        )

        log.info("Plan item: %s on %s — %s", topic, day_str, result)


def run() -> None:
    """Entry point for launchd --plan job."""
    log.info("planner: synthesizing weekly plan…")
    plan = synthesize_plan()
    if not plan:
        print("planner: no plan items generated.")
        return
    print(f"planner: {len(plan)} item(s) generated. Publishing to Calendar…")
    publish_plan(plan)
    print("planner: done.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()
