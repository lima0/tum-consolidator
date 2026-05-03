"""
TUMsolidator pipeline.

  python main.py           # full sync + summarize + brief
  python main.py --brief   # briefing only (fast — uses existing DB data)
  python main.py --sync    # sync connectors only, no LLM
"""

import argparse
import logging
import os

from dotenv import load_dotenv

load_dotenv(override=True)

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


def sync(db) -> None:
    """Pull latest data from all sources into the DB."""

    log.info("=== Artemis ===")
    from connectors import artemis
    from storage.normalizer import normalize_artemis_assignment
    session = artemis._make_session()
    if artemis._ensure_authenticated(session):
        data = artemis.get_full_dashboard_data(session)
        if data:
            for course in data:
                for exercise in course["exercises"]:
                    db.upsert_event(normalize_artemis_assignment(course, exercise))
                log.info("[%s] %d exercise(s)", course["shortName"], len(course["exercises"]))
                for lecture in artemis.get_course_lectures(session, course["id"]):
                    folder = os.path.join("resources", course["shortName"], str(lecture["id"]))
                    artemis.sync_lecture_resources(session, lecture["id"], folder, course=course, db=db)
    else:
        log.error("Artemis auth failed, skipping")

    log.info("=== Moodle ===")
    try:
        from connectors import moodle
        msession = moodle.get_session(os.environ["TUM_USERNAME"], os.environ["TUM_PASSWORD"])
        sesskey, userid = moodle.get_sesskey_and_userid(msession)
        courses = moodle.get_enrolled_courses(msession, sesskey, userid)
        moodle.download_course_files(msession, courses, output_dir="resources", db=db)
    except Exception as exc:
        log.error("Moodle sync failed: %s", exc)

    log.info("=== Calendar ===")
    try:
        from connectors import campuscalendar
        from storage.normalizer import normalize_calendar_event
        events = campuscalendar.fetch_events(days=10)
        for ev in events:
            db.upsert_event(normalize_calendar_event(ev))
        log.info("%d calendar event(s)", len(events))
    except Exception as exc:
        log.error("Calendar sync failed: %s", exc)


def summarize(db, limit: int = 10) -> None:
    """Summarize up to `limit` unprocessed PDFs (avoids rate-limit cascade)."""
    try:
        from intelligence.summarize import process_unprocessed
        process_unprocessed(db, limit=limit)
    except Exception as exc:
        log.error("Summarization failed: %s", exc)


def brief(db) -> None:
    """Generate briefing, open in browser, print to terminal."""
    try:
        import html as _html
        from intelligence.planner import build_briefing, push_to_browser, _deadlines_html, _materials_html, _strip_markdown
        briefing = build_briefing(db)
        card = (
            "<div class='card'>"
            "<h2>Today's Briefing</h2>"
            f"<div class='briefing-text briefing-md' data-md='{_html.escape(briefing, quote=True)}'></div>"
            "</div>"
        )
        push_to_browser(_deadlines_html(db) + _materials_html(db) + card)
        print("\n" + _strip_markdown(briefing))
    except Exception as exc:
        log.error("Briefing failed: %s", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="TUMsolidator")
    parser.add_argument("--brief",    action="store_true", help="briefing only, skip sync")
    parser.add_argument("--sync",     action="store_true", help="sync connectors only, no LLM")
    parser.add_argument("--ask",      type=str, metavar="QUESTION",
                        help="ask the AI a question about your courses and deadlines")
    parser.add_argument("--limit",    type=int, default=10, metavar="N",
                        help="max PDFs to summarize per run (default: 10)")
    args = parser.parse_args()

    from storage.db import Database
    db = Database()

    if args.ask:
        from intelligence.agent import ask
        answer = ask(db, args.ask)
        print("\n" + answer)
    elif args.brief:
        brief(db)
    elif args.sync:
        sync(db)
    else:
        sync(db)
        summarize(db, limit=args.limit)
        brief(db)


if __name__ == "__main__":
    main()
