import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import requests

from connectors import http
from storage.db import Database
from storage.normalizer import normalize_artemis_assignment, normalize_artemis_attachment

log = logging.getLogger(__name__)

BASE_URL      = "https://artemis.tum.de"
AUTH_URL      = f"{BASE_URL}/api/core/public/authenticate"
DASHBOARD_URL = f"{BASE_URL}/api/core/courses/for-dashboard"
COOKIES_FILE  = "state/artemis_cookies.json"
RESOURCES_DIR = Path("resources")


# --- Auth ---

def _make_session() -> requests.Session:
    return http.make_session({"Accept": "application/json", "Content-Type": "application/json"})


def _is_session_valid(session: requests.Session) -> bool:
    try:
        return session.get(DASHBOARD_URL, timeout=10).status_code == 200
    except requests.RequestException:
        return False


def _login(session: requests.Session) -> bool:
    username = os.environ["TUM_USERNAME"]
    password = os.environ["TUM_PASSWORD"]
    res = session.post(AUTH_URL, json={"username": username, "password": password, "rememberMe": True})
    if res.status_code == 200:
        http.save_cookies(session, COOKIES_FILE)
        return True
    log.warning("Login failed: %s", res.status_code)
    return False


def _ensure_authenticated(session: requests.Session) -> bool:
    if http.load_cookies(session, COOKIES_FILE) and _is_session_valid(session):
        return True
    log.info("Session expired or missing, logging in...")
    return _login(session)


# --- Helpers ---

def _fmt_date(iso):
    if not iso:
        return None
    return iso[:16].replace("T", " ")


def _parse_categories(raw_list):
    names = []
    for entry in (raw_list or []):
        try:
            names.append(json.loads(entry).get("category", ""))
        except (json.JSONDecodeError, TypeError):
            pass
    return names


def _score_label(included):
    return {
        "INCLUDED_COMPLETELY": "COUNTED",
        "INCLUDED_AS_BONUS":   "BONUS",
        "NOT_INCLUDED":        "EXCLUDED",
    }.get(included, included)

# TIME FOR DUE DATES
def _time_until(due: str | None) -> str | None:
    if not due:
        return None
    try:
        delta = datetime.strptime(due, "%Y-%m-%d %H:%M") - datetime.now()
    except ValueError:
        return None
    total = int(delta.total_seconds())
    if total < 0:
        return "OVERDUE"
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m      = rem // 60
    if d > 0:
        return f"in {d}d {h}h"
    if h > 0:
        return f"in {h}h {m}m"
    return f"in {m}m"

# Assignments
def _parse_participation(parts: list, participation_scores: dict) -> dict:
    result = {
        #Default to NOT_STARTED, overwritten later
        "submission_status": "NOT_STARTED",
        "score":             None,
        "rated":             None,
        "passed_tests":      None,
        "total_tests":       None,
        "submitted_at":      None,
        "build_failed":      None,
    }
    if not parts:
        return result

    part       = parts[0]
    part_id    = part.get("id")
    init_state = part.get("initializationState", "")

    if init_state == "FINISHED":
        result["submission_status"] = "FINISHED"
    elif init_state == "INITIALIZED":
        result["submission_status"] = "STARTED"
    else:
        result["submission_status"] = init_state

    subs = part.get("submissions") or []
    if subs:
        last_sub                  = subs[-1]
        result["submitted_at"]    = _fmt_date(last_sub.get("submissionDate"))
        result["build_failed"]    = last_sub.get("buildFailed")
        result["submission_status"] = "SUBMITTED" if last_sub.get("submitted") else "IN_PROGRESS"
        results = last_sub.get("results") or []
        if results:
            last_result             = results[-1]
            result["score"]         = last_result.get("score")
            result["rated"]         = last_result.get("rated")
            result["passed_tests"]  = last_result.get("passedTestCaseCount")
            result["total_tests"]   = last_result.get("testCaseCount")

    if result["score"] is None and part_id in participation_scores:
        pr             = participation_scores[part_id]
        result["score"] = pr["score"]
        result["rated"] = pr["rated"]

    return result


def _extract_scores(score_obj):
    s = score_obj.get("studentScores", {})
    return {
        "max":      score_obj.get("maxPoints", 0),
        "reachable": score_obj.get("reachablePoints", 0),
        "absolute": s.get("absoluteScore", 0),
        "relative": s.get("relativeScore", 0),
    }


# --- Dashboard ---

def get_full_dashboard_data(session: requests.Session):
    response = session.get(DASHBOARD_URL)
    if response.status_code != 200:
        log.warning("Failed to fetch dashboard: %s", response.status_code)
        return None

    all_courses_data = []

# Parse all artemis Data - NOTE: use Artemis' schema available on their Github for later updates
    for item in response.json().get("courses", []):
        course = item.get("course", {})

        participation_scores = {
            p["participationId"]: {"score": p["score"], "rated": p["rated"]}
            for p in item.get("participationResults", [])
        }

        course_info = {
            "id":        course.get("id"),
            "title":     course.get("title"),
            "shortName": course.get("shortName"),
            "semester":  course.get("semester"),
            "start":     _fmt_date(course.get("startDate")),
            "end":       _fmt_date(course.get("endDate")),
            "scores": {
                "total":       _extract_scores(item.get("totalScores", {})),
                "programming": _extract_scores(item.get("programmingScores", {})),
                "quiz":        _extract_scores(item.get("quizScores", {})),
                "text":        _extract_scores(item.get("textScores", {})),
                "modeling":    _extract_scores(item.get("modelingScores", {})),
                "file_upload": _extract_scores(item.get("fileUploadScores", {})),
            },
            "exercises": [],
        }

        # Main dashboard truncates exercise list — supplement from per-course endpoint
        course_id = course.get("id")
        dash_ids = {ex.get("id") for ex in course.get("exercises", [])}
        extra_exercises = []

        # Try /for-dashboard first (more reliable somehow(?) - revise later)
        for endpoint in (
            f"{BASE_URL}/api/core/courses/{course_id}/for-dashboard",
            f"{BASE_URL}/api/core/courses/{course_id}/with-exercises",
        ):
            r = session.get(endpoint)
            if r.status_code != 200:
                continue
            body = r.json()
            # /with-exercises: {id, exercises:[...]}  /for-dashboard: {course:{exercises:[...]}}
            raw = body.get("exercises") or body.get("course", {}).get("exercises", [])
            extra_exercises = [ex for ex in raw if ex.get("id") not in dash_ids]
            if raw:
                break

        exercises_list = course.get("exercises", []) + extra_exercises

        for ex in exercises_list:
            p          = _parse_participation(ex.get("studentParticipations") or [], participation_scores)
            ex_type    = ex.get("type")
            categories = _parse_categories(ex.get("categories"))

            exercise = {
                "id":           ex.get("id"),
                "short":        ex.get("shortName"),
                "title":        ex.get("title"),
                "type":         ex_type,
                "difficulty":   ex.get("difficulty"),
                "mode":         ex.get("mode"),
                "categories":   categories,
                "max_points":   ex.get("maxPoints"),
                "bonus_points": ex.get("bonusPoints", 0),
                "included":     _score_label(ex.get("includedInOverallScore")),
                "release":      _fmt_date(ex.get("releaseDate")),
                "due":          _fmt_date(ex.get("dueDate")),
                "solution_date": _fmt_date(ex.get("exampleSolutionPublicationDate")),
                "status":       p["submission_status"],
                "submitted_at": p["submitted_at"],
                "score":        p["score"],
                "rated":        p["rated"],
                "passed_tests": p["passed_tests"],
                "total_tests":  p["total_tests"],
                "build_failed": p["build_failed"],
            }

            if ex_type == "programming":
                exercise["language"] = ex.get("programmingLanguage")
            elif ex_type == "quiz":
                exercise["quiz_started"]      = ex.get("quizStarted")
                exercise["quiz_ended"]        = ex.get("quizEnded")
                exercise["attempts_allowed"]  = ex.get("allowedNumberOfAttempts")
                duration_s = ex.get("duration")
                exercise["duration_min"] = duration_s // 60 if duration_s else None

            course_info["exercises"].append(exercise)

        all_courses_data.append(course_info)

    return all_courses_data


# --- Lecture resources ---

# In case Course uses artemis for File Uploads (Tutorials, Slides)
def get_course_lectures(session: requests.Session, course_id: int) -> list:
    url      = f"{BASE_URL}/api/core/courses/{course_id}/for-dashboard"
    response = session.get(url)
    if response.status_code != 200:
        log.warning("Failed to fetch lectures for course %s: %s", course_id, response.status_code)
        return []
    return response.json().get("course", {}).get("lectures", [])

# Convert an attachment link to the student-accessible download URL.
def _link_to_student_url(link: str) -> str:
    if link.startswith("http"):
        path = link.split(BASE_URL, 1)[-1].lstrip("/")
    elif link.startswith("/"):
        path = link.lstrip("/")
    else:
        path = link  # already relative, e.g. "attachments/attachment-unit/..."

    match = re.search(r"attachment-unit/(\d+)/(.+)", path)
    if match:
        unit_id, filename = match.group(1), Path(match.group(2)).name
        return f"{BASE_URL}/api/core/files/attachments/attachment-unit/{unit_id}/student/{filename}"
    # fallback — try direct path
    return f"{BASE_URL}/api/core/files/{path}"


def _filename_from_link(link: str, fallback: str) -> str:
    name = Path(link.split("?")[0]).name
    try:
        name = requests.utils.unquote(name)
    except Exception:
        pass
    if not name or not Path(name).suffix:
        return f"{fallback}.pdf"
    return name


def sync_lecture_resources(
    session: requests.Session,
    lecture_id: int,
    download_folder: str,
    course: dict | None = None,
    db: Database | None = None,
) -> int:
    url      = f"{BASE_URL}/api/lecture/lectures/{lecture_id}/details"
    response = session.get(url)
    if response.status_code != 200:
        log.warning("Failed to fetch lecture %s: %s", lecture_id, response.status_code)
        return 0

    units      = response.json().get("lectureUnits", [])
    downloaded = 0

    for unit in units:
        if unit.get("type") != "attachment":
            continue

        attachment = unit.get("attachment") or {}
        link       = attachment.get("link") or unit.get("link")
        unit_name  = unit.get("name", f"unit_{unit.get('id')}")
        if not link:
            log.debug("Lecture %s unit %r has no link, skipping", lecture_id, unit_name)
            continue

        file_url  = _link_to_student_url(link)
        ext       = Path(_filename_from_link(link, unit_name)).suffix or ".pdf"
        filename  = unit_name + ext
        dest      = Path(download_folder) / filename

        if http.download_file(session, file_url, dest):
            log.info("Downloaded: %s", filename)
            downloaded += 1
            if course:
                from connectors.notify import notify
                notify(course["shortName"], f"New material: {unit_name}")

        if db and course:
            doc = normalize_artemis_attachment(course, unit, local_path=str(dest))
            db.upsert_document(doc)

    log.info("Lecture %s: %d new file(s) downloaded.", lecture_id, downloaded)
    return downloaded


# --- Output ---

def debug_print(data) -> str:
    lines = []
    for course in data:
        total = course["scores"]["total"]
        lines.append(
            f"[{course['shortName']}] {course['title']} | {course['semester']} | "
            f"score:{total['absolute']}/{total['reachable']}pts({total['relative']}%)"
        )

        for ex in course["exercises"]:
            short     = ex["short"] or ex["title"][:12]
            bonus     = f"+{ex['bonus_points']}b" if ex["bonus_points"] else ""
            pts       = f"{ex['max_points']}pts{bonus}"
            countdown = _time_until(ex["due"])
            due_str   = f"due:{ex['due']}({countdown})" if ex["due"] and countdown else f"due:{ex['due']}" if ex["due"] else ""

            # type token
            ex_type = ex["type"] or "?"
            if ex_type == "programming":
                ex_type = f"prog/{ex.get('language') or '?'}"
            elif ex_type == "quiz" and ex.get("duration_min"):
                ex_type = f"quiz/{ex['duration_min']}min"

            # status token
            if ex["score"] is not None:
                tests  = f"({ex['passed_tests']}/{ex['total_tests']})" if ex["total_tests"] else ""
                rated  = "" if ex["rated"] else " unrated"
                fail   = " FAIL" if ex["build_failed"] else ""
                status = f"score:{ex['score']}%{tests}{rated}{fail}"
            else:
                status = ex["status"]

            parts = [short, ex_type, pts, ex["included"]]
            if due_str:
                parts.append(due_str)
            parts.append(status)

            lines.append(f"  {'|'.join(parts)}")

        lines.append("")

    return "\n".join(lines)


# --- Main ---

def main():
    session = _make_session()
    if not _ensure_authenticated(session):
        log.error("Authentication Failed - Exiting")
        return

    db   = Database()
    data = get_full_dashboard_data(session)
    if not data:
        return

    for course in data:
        for exercise in course["exercises"]:
            event = normalize_artemis_assignment(course, exercise)
            db.upsert_event(event)
        log.info("[%s] %d exercise(s) synced", course["shortName"], len(course["exercises"]))

        lectures = get_course_lectures(session, course["id"])
        if not lectures:
            continue
        log.info("[%s] %d lecture(s)", course["shortName"], len(lectures))
        for lecture in lectures:
            folder = os.path.join(RESOURCES_DIR, course["shortName"], str(lecture["id"]))
            sync_lecture_resources(session, lecture["id"], folder, course=course, db=db)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    main()
