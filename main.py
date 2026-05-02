"""
Full pipeline: sync all sources → summarize new PDFs → push daily briefing.
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.BASIC_FORMAT,
    format="%(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


def main() -> None:
    from storage.db import Database
    db = Database()

    # 1. Artemis — exercises + lecture attachments
    log.info("=== Artemis ===")
    from connectors import artemis
    session = artemis._make_session()
    if artemis._ensure_authenticated(session):
        data = artemis.get_full_dashboard_data(session)
        if data:
            for course in data:
                for exercise in course["exercises"]:
                    from storage.normalizer import normalize_artemis_assignment
                    db.upsert_event(normalize_artemis_assignment(course, exercise))
                log.info("[%s] %d exercise(s) synced", course["shortName"], len(course["exercises"]))

                lectures = artemis.get_course_lectures(session, course["id"])
                for lecture in lectures:
                    folder = os.path.join("resources", course["shortName"], str(lecture["id"]))
                    artemis.sync_lecture_resources(session, lecture["id"], folder, course=course, db=db)
    else:
        log.error("Artemis auth failed, skipping")

    # 2. Moodle — course files
    log.info("=== Moodle ===")
    try:
        from connectors import moodle
        username = os.environ["TUM_USERNAME"]
        password = os.environ["TUM_PASSWORD"]
        msession = moodle.get_session(username, password)
        sesskey, userid = moodle.get_sesskey_and_userid(msession)
        courses = moodle.get_enrolled_courses(msession, sesskey, userid)
        moodle.download_course_files(msession, courses, output_dir="resources", db=db)
    except Exception as exc:
        log.error("Moodle sync failed: %s", exc)

    # 3. Calendar — upcoming schedule
    log.info("=== Calendar ===")
    try:
        from connectors import campuscalendar
        from storage.normalizer import normalize_calendar_event
        events = campuscalendar.fetch_events(days=10)
        for ev in events:
            db.upsert_event(normalize_calendar_event(ev))
        log.info("%d calendar event(s) synced", len(events))
    except Exception as exc:
        log.error("Calendar sync failed: %s", exc)

    # 4. Summarize newly downloaded PDFs
    log.info("=== Summarize ===")
    try:
        from intelligence.summarize import process_unprocessed
        process_unprocessed(db)
    except Exception as exc:
        log.error("Summarization failed: %s", exc)

    # 5. Daily briefing → Apple Notes
    log.info("=== Briefing ===")
    try:
        from intelligence.planner import build_briefing, build_note, push_to_notes
        briefing  = build_briefing(db)
        dashboard = build_note(db)
        full_note = f"<h2>Today's Briefing</h2><p>{briefing}</p>\n{dashboard}"
        push_to_notes(full_note)
        log.info("Briefing pushed to Apple Notes")
        print("\n" + briefing)
    except Exception as exc:
        log.error("Briefing failed: %s", exc)


if __name__ == "__main__":
    main()
