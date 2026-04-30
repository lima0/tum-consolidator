"""
tum_moodle.py  --  TUM Moodle crawler via Shibboleth SSO

Flow (from HAR analysis):
  0. GET  moodle.tum.de/Shibboleth.sso/Login  -> 302 to login.tum.de
  1. GET  login.tum.de/idp/...?SAMLRequest=... -> 302 to first IdP form
  2. (optional) localStorage probe form -> POST -> credential form
  3. POST credential form -> 302 to consent form
  4. POST consent form -> 200 (SAMLResponse auto-submit)
  5. POST moodle.tum.de/Shibboleth.sso/SAML2/POST -> 302 to auth/shibboleth/index.php
  6. GET  moodle.tum.de/auth/shibboleth/index.php  -> 303 to /my/
  7. GET  moodle.tum.de/my/  -> 200 (authenticated dashboard)
"""

import os
import re
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from connectors import http
from db import Database
from normalizer import normalize_moodle_document

log = logging.getLogger(__name__)

MOODLE_BASE = "https://www.moodle.tum.de"
SSO_ENTRY   = (
    f"{MOODLE_BASE}/Shibboleth.sso/Login"
    "?providerId=https%3A%2F%2Ftumidp.lrz.de%2Fidp%2Fshibboleth"
    "&target=https%3A%2F%2Fwww.moodle.tum.de%2Fauth%2Fshibboleth%2Findex.php"
)

DOWNLOADABLE_MODULES = {"resource", "folder"}



def parse_form(html: str, base_url: str) -> tuple[str, dict]:
    """
    Extract action URL and all submittable fields from the first <form>.

    Returns a dict where multi-value fields (e.g. repeated checkboxes with the
    same name) are stored as lists — requests.post(data=...) handles these
    correctly by repeating the key in the body.

    <button type="submit"> elements are included so that Shibboleth IdP
    event-ID buttons (e.g. _eventId_proceed) are sent. When there are multiple
    submit buttons, the accepting one is preferred over the declining one.
    """
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        raise RuntimeError("No <form> found in page")

    action = urljoin(base_url, form.get("action", base_url))
    fields: dict = {}

    def _add(name, value):
        """Append value; convert to list on second write (multi-value fields)."""
        if name not in fields:
            fields[name] = value
        elif isinstance(fields[name], list):
            fields[name].append(value)
        else:
            fields[name] = [fields[name], value]

    # Collect submit elements separately so we can pick the right one.
    submit_els = []

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                _add(name, inp.get("value", "on"))
        elif itype == "submit":
            submit_els.append((name, inp.get("value", "")))
        else:
            fields[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        selected = sel.find("option", selected=True)
        option = selected or sel.find("option")
        fields[name] = option.get("value", "") if option else ""

    for btn in form.find_all("button"):
        if (btn.get("type") or "submit").lower() == "submit" and btn.get("name"):
            submit_els.append((btn["name"], btn.get("value", "")))

    # Pick the accepting submit element over the declining one.
    # IMPORTANT: check REJECT before ACCEPT — "donotconsent" contains "consent".
    REJECT_KEYWORDS = ("donotconsent", "decline", "deny", "cancel", "reject")
    ACCEPT_KEYWORDS = ("proceed", "accept", "yes", "allow", "saveconsent", "consent")
    def _submit_priority(item):
        n = item[0].lower()
        if any(k in n for k in REJECT_KEYWORDS):
            return 2
        if any(k in n for k in ACCEPT_KEYWORDS):
            return 0
        return 1
    submit_els.sort(key=_submit_priority)
    if submit_els:
        name, value = submit_els[0]
        fields[name] = value
        log.debug("parse_form: chose submit %r=%r", name, value)

    return action, fields


def login(username: str, password: str) -> requests.Session:
    if not username or not password:
        raise RuntimeError("TUM_USERNAME and TUM_PASSWORD environment variables must be set")

    s = http.make_session()

    # Step 0-1: hit SSO entry, follow redirects to the first IdP form
    log.debug("Step 0-1: initiating Shibboleth SSO flow")
    resp = s.get(SSO_ENTRY, allow_redirects=True)
    resp.raise_for_status()

    action, fields = parse_form(resp.text, resp.url)
    log.debug("First IdP form at %s: fields=%s", resp.url, list(fields.keys()))

    if "csrf_token" not in fields:
        raise RuntimeError("csrf_token not found -- login form structure may have changed")

    # Step 2 (conditional): The IdP sometimes shows a localStorage-probe form
    # before the credential form (fields contain shib_idp_ls_*).  When it does,
    # simulate a browser with working localStorage and submit to advance.
    # When the IdP skips this step we land directly on the credential form.
    if "j_username" not in fields:
        log.debug("Step 2: submitting localStorage probe at %s", resp.url)
        for key in list(fields.keys()):
            if key.startswith("shib_idp_ls_success."):
                fields[key] = "true"
        if "shib_idp_ls_supported" in fields:
            fields["shib_idp_ls_supported"] = "true"
        time.sleep(0.3)
        resp = s.post(action, data=fields, allow_redirects=True)
        resp.raise_for_status()
        log.debug("Step 2: probe response URL: %s", resp.url)
        action, fields = parse_form(resp.text, resp.url)
        log.debug("Step 2: credential form fields: %s", list(fields.keys()))
    else:
        log.debug("Step 2: skipped (IdP presented credential form directly at %s)", resp.url)

    if "j_username" not in fields or "j_password" not in fields:
        raise RuntimeError(f"Credential form not found at {resp.url} -- fields: {list(fields.keys())}")

    # Step 3: fill and submit credentials.
    # Drop _shib_idp_revokeConsent so we never accidentally wipe the IdP's
    # saved consent record on behalf of the user.
    fields.pop("_shib_idp_revokeConsent", None)
    fields["j_username"]       = username
    fields["j_password"]       = password
    fields["donotcache"]       = "1"
    fields["_eventId_proceed"] = ""

    log.debug("Step 3: submitting credentials to %s", action)
    time.sleep(0.5)
    resp = s.post(action, data=fields, allow_redirects=True)
    resp.raise_for_status()
    log.debug("Step 3: response URL: %s", resp.url)

    # Wrong credentials → IdP redisplays the credential form with an error
    if "j_password" in resp.text and "j_username" in resp.text:
        soup = BeautifulSoup(resp.text, "html.parser")
        err = soup.find(class_=re.compile(r"error|alert|message", re.I))
        msg = err.get_text(strip=True) if err else "credential form re-displayed"
        raise RuntimeError(f"Authentication failed (wrong credentials?): {msg}")

    # Step 4: walk through any remaining IdP forms (consent, MFA, …) until
    # we reach the SAMLResponse auto-submit form.
    for step in range(5):
        action, fields = parse_form(resp.text, resp.url)
        log.debug("Step 4.%d at %s: fields=%s", step, resp.url, list(fields.keys()))

        if "SAMLResponse" in fields:
            break

        if "j_username" in fields or "j_password" in fields:
            err_soup = BeautifulSoup(resp.text, "html.parser")
            err = err_soup.find(class_=re.compile(r"error|alert|message", re.I))
            msg = err.get_text(strip=True) if err else "credential form re-displayed"
            raise RuntimeError(f"Authentication failed (wrong credentials?): {msg}")

        log.debug("Step 4.%d: submitting intermediate form at %s (fields=%s)",
                 step, resp.url, list(fields.keys()))
        time.sleep(0.3)
        resp = s.post(action, data=fields, allow_redirects=True)
        resp.raise_for_status()
        log.debug("Step 4.%d: -> %s %s (%d bytes)",
                 step, resp.status_code, resp.url, len(resp.text))
    else:
        raise RuntimeError("SAMLResponse not found after 5 intermediate IdP steps")

    # Step 5: POST the SAMLResponse to the Moodle SP
    log.debug("Step 5: posting SAMLResponse to Moodle SP")
    saml_action, saml_fields = parse_form(resp.text, resp.url)

    if "SAMLResponse" not in saml_fields:
        raise RuntimeError("SAMLResponse not found -- IdP may have returned an error page")

    resp = s.post(saml_action, data=saml_fields, allow_redirects=True)
    resp.raise_for_status()

    if "/my/" not in resp.url and "moodle" not in resp.url:
        raise RuntimeError(f"Unexpected final URL after auth: {resp.url}")

    log.debug("Authenticated. Final URL: %s", resp.url)
    return s


def get_sesskey_and_userid(s: requests.Session) -> tuple[str, int]:
    """Extract sesskey and the logged-in user's numeric ID from the dashboard."""
    resp = s.get(f"{MOODLE_BASE}/my/")
    resp.raise_for_status()

    match = re.search(r'"sesskey"\s*:\s*"([^"]+)"', resp.text)
    if not match:
        match = re.search(r'sesskey=([A-Za-z0-9]+)', resp.text)
    if not match:
        raise RuntimeError("Could not extract sesskey from dashboard")
    sesskey = match.group(1)

    uid_match = re.search(r'"userid"\s*:\s*(\d+)', resp.text)
    if not uid_match:
        uid_match = re.search(r'data-userid="(\d+)"', resp.text)
    if not uid_match:
        # Fall back: /user/edit.php?id=NNN redirect after login
        r2 = s.get(f"{MOODLE_BASE}/user/profile.php", allow_redirects=True)
        uid_match = re.search(r'[?&]id=(\d+)', r2.url)
    if not uid_match:
        raise RuntimeError("Could not extract user ID from dashboard")
    userid = int(uid_match.group(1))

    return sesskey, userid


def get_enrolled_courses(s: requests.Session, sesskey: str, userid: int) -> list[dict]:
    """
    Return only currently-active ('inprogress') enrolled courses.

    Tries three approaches in order:
      1. core_course_get_enrolled_courses_by_timeline_classification (inprogress)
         — what the browser uses when filtering My courses to the current semester.
      2. core_enrol_get_users_courses — full list, no semester filter.
      3. HTML scrape of /my/ — last resort, also no semester filter.
    """
    ajax_base = f"{MOODLE_BASE}/lib/ajax/service.php?sesskey={sesskey}&info="

    def _to_course_list(raw_courses):
        return [
            {
                "id":        c["id"],
                "shortname": c.get("shortname", ""),
                "fullname":  c["fullname"],
                "url":       f"{MOODLE_BASE}/course/view.php?id={c['id']}",
            }
            for c in raw_courses
        ]

    # 1. Timeline-filtered list — only courses currently in progress
    try:
        url = ajax_base + "core_course_get_enrolled_courses_by_timeline_classification"
        payload = [{
            "index": 0,
            "methodname": "core_course_get_enrolled_courses_by_timeline_classification",
            "args": {
                "offset": 0,
                "limit": 0,
                "classification": "inprogress",
                "sort": "fullname",
                "customfieldname": "",
                "customfieldvalue": "",
            },
        }]
        resp = s.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data[0].get("error"):
            # Response is either {"courses": [...]} or the list directly
            raw = data[0]["data"]
            courses_raw = raw.get("courses", raw) if isinstance(raw, dict) else raw
            if courses_raw:
                log.debug("Timeline classification: %d in-progress course(s)", len(courses_raw))
                return _to_course_list(courses_raw)
        log.warning("core_course_get_enrolled_courses_by_timeline_classification error: %s", data[0])
    except Exception as exc:
        log.warning("core_course_get_enrolled_courses_by_timeline_classification failed (%s)", exc)

    # 2. Full enrollment list (no semester filter)
    try:
        url = ajax_base + "core_enrol_get_users_courses"
        payload = [{"index": 0, "methodname": "core_enrol_get_users_courses", "args": {"userid": userid}}]
        resp = s.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data[0].get("error"):
            log.debug("core_enrol_get_users_courses: %d course(s) (all semesters)", len(data[0]["data"]))
            return _to_course_list(data[0]["data"])
        log.warning("core_enrol_get_users_courses returned error: %s", data[0])
    except Exception as exc:
        log.warning("core_enrol_get_users_courses failed (%s), falling back to HTML scrape", exc)

    # 3. HTML scrape (no semester filter)
    return _scrape_courses_from_dashboard(s)


def _scrape_courses_from_dashboard(s: requests.Session) -> list[dict]:
    resp = s.get(f"{MOODLE_BASE}/my/", allow_redirects=True)
    if "moodle.tum.de/my/" not in resp.url:
        log.warning("HTML scrape: expected /my/ but landed at %s — session likely invalid", resp.url)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    courses = []
    seen = set()
    for a in soup.select("a[href*='/course/view.php']"):
        href = a["href"]
        m = re.search(r'id=(\d+)', href)
        if href not in seen and m:
            seen.add(href)
            # Prefer the title attribute — it contains only the course name,
            # without the coc-metainfo "(Semester | Category)" suffix that
            # get_text() would include.
            fullname = a.get("title") or a.get_text(strip=True)
            courses.append({
                "id":        int(m.group(1)),
                "shortname": "",
                "fullname":  fullname,
                "url":       href,
            })

    if not courses:
        log.warning("HTML scrape returned 0 courses — check that /my/ shows enrolled courses "
                    "and that the session has full SAML attributes (\"userid\" in page: %s)",
                    '"userid"' in resp.text)
    else:
        log.debug("HTML scrape found %d course(s)", len(courses))
    return courses


def get_course_content(s: requests.Session, course_id: int) -> list[dict]:
    """
    Fetch course sections and downloadable modules.

    TUM Moodle has core_course_get_contents disabled (servicenotavailable),
    so we scrape the course page HTML directly — the same source the browser
    uses.  Returns the same section/module structure that download_course_files
    expects; contents is always empty here, so _resolve_resource_url handles
    each module on download.
    """
    url = f"{MOODLE_BASE}/course/view.php?id={course_id}"
    resp = s.get(url, allow_redirects=True)
    if resp.status_code != 200 or f"id={course_id}" not in resp.url:
        log.warning("Could not load course page for %d (status %s, url %s)",
                    course_id, resp.status_code, resp.url)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    sections = []

    # Moodle 4.x renders sections as <li id="section-N" …> or <div id="section-N" …>
    for sec_el in soup.find_all(id=re.compile(r'^section-\d+$')):
        sec_num_str = sec_el["id"].split("-")[1]
        section_num = int(sec_num_str)

        # Section name — try several selectors used by different Moodle themes
        name_el = (
            sec_el.find(class_="sectionname")
            or sec_el.find(class_="section-title")
            or sec_el.find("h3")
            or sec_el.find("h4")
        )
        # Strip any "accesshide" spans (screen-reader-only text) before reading
        if name_el:
            for hidden in name_el.find_all(class_="accesshide"):
                hidden.decompose()
        sec_name = name_el.get_text(strip=True) if name_el else f"Section {section_num}"

        mods = []
        for act_el in sec_el.find_all(class_=re.compile(r'\bmodtype_(resource|folder)\b')):
            # Module type from CSS class
            classes = " ".join(act_el.get("class", []))
            modtype = "folder" if "modtype_folder" in classes else "resource"

            # The activity link — skip management/admin links
            a = act_el.find(
                "a",
                href=re.compile(r'/mod/(resource|folder)/view\.php')
            )
            if not a:
                continue
            href = a["href"]
            m_id = re.search(r'\bid=(\d+)', href)
            if not m_id:
                continue
            mod_id = int(m_id.group(1))

            # Module name — strip the screen-reader type suffix ("Datei", "Ordner", …)
            name_span = act_el.find(class_="instancename")
            if name_span:
                for hidden in name_span.find_all(class_="accesshide"):
                    hidden.decompose()
                mod_name = name_span.get_text(strip=True)
            else:
                mod_name = a.get_text(strip=True) or f"module_{mod_id}"

            mods.append({
                "id":       mod_id,
                "name":     mod_name,
                "modname":  modtype,
                "module":   modtype,
                "url":      href,
                "instance": None,
                "contents": [],
            })

        sections.append({
            "section": section_num,
            "name":    sec_name,
            "modules": mods,
        })

    log.debug("Course %d: found %d section(s) with %d downloadable module(s)",
             course_id,
             len(sections),
             sum(len(sec["modules"]) for sec in sections))
    return sections


def _sanitize(name: str) -> str:
    """Make a string safe to use as a file/directory name."""
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'_+', '_', name)
    name = name.lstrip('.')  # prevent hidden files and relative-path components (..)
    return name[:200] or "unnamed"


def download_file(
    s: requests.Session,
    fileurl: str,
    dest: Path,
    timemodified: int = 0,
) -> bool:
    """
    Download a single file from a Moodle pluginfile URL to dest.
    Skips if dest exists and its mtime matches timemodified (server-side).
    Returns True if the file was written, False if skipped.
    """
    if dest.exists():
        if not timemodified or int(dest.stat().st_mtime) >= timemodified:
            log.debug("Skipping (exists): %s", dest)
            return False

    dest.parent.mkdir(parents=True, exist_ok=True)

    log.debug("Downloading: %s", dest)
    try:
        resp = s.get(fileurl, stream=True, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to download %s: %s", fileurl, exc)
        return False

    # Honour Content-Disposition filename if the URL has no obvious extension
    if not dest.suffix:
        cd = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, re.I)
        if m:
            dest = dest.parent / _sanitize(m.group(1).strip())

    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    if timemodified:
        os.utime(dest, (timemodified, timemodified))

    return True


def download_course_files(
    s: requests.Session,
    courses: list[dict],
    output_dir: str = "resources",
    db: Database | None = None,
) -> None:
    """
    For every enrolled course, enumerate all downloadable modules
    (resource, folder) and save them to:
      <output_dir>/<course_shortname>/<section_name>/<filename>
    """
    base = Path(output_dir)
    total_downloaded = 0
    total_skipped    = 0

    for course in courses:
        cid       = course.get("id")
        shortname = _sanitize(course.get("shortname") or course.get("fullname", f"course_{cid}"))
        fullname  = course.get("fullname", shortname)
        log.debug("Course: %s (%s)", fullname, cid)

        sections = get_course_content(s, cid)
        if not sections:
            log.warning("No content returned for course %s", cid)
            continue

        for section in sections:
            sec_name = _sanitize(section["name"])
            sec_dir  = base / shortname / sec_name

            for mod in section["modules"]:
                # Only resource and folder modules carry downloadable files
                modname_raw = (mod.get("modname") or "").lower()
                if modname_raw not in DOWNLOADABLE_MODULES:
                    continue

                contents = mod.get("contents") or []
                if not contents:
                    # Some resource modules redirect to the file; try via view URL
                    if mod.get("url"):
                        contents = _resolve_resource_url(s, mod["url"])

                mod_dir = sec_dir / _sanitize(mod["name"])

                for entry in contents:
                    if entry.get("type") != "file":
                        continue
                    fileurl      = entry.get("fileurl", "")
                    filename     = _sanitize(entry.get("filename") or Path(urlparse(fileurl).path).name or "file")
                    timemodified = int(entry.get("timemodified") or 0)

                    if not fileurl:
                        continue

                    dest = mod_dir / filename
                    wrote = download_file(s, fileurl, dest, timemodified)
                    if wrote:
                        total_downloaded += 1
                    else:
                        total_skipped += 1

                    if db:
                        doc = normalize_moodle_document(course, mod, entry, local_path=str(dest))
                        db.upsert_document(doc)

                    time.sleep(0.3)

    log.info("Done. %d file(s) downloaded, %d skipped (up-to-date).", total_downloaded, total_skipped)


def _resolve_resource_url(s: requests.Session, view_url: str) -> list[dict]:
    """
    Fallback: open the mod/resource/view.php page and scrape the pluginfile link.
    Returns a list with a single file-entry dict, or empty list on failure.
    """
    try:
        resp = s.get(view_url, allow_redirects=True, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        return []

    # If the server redirected straight to a pluginfile URL, use that
    if "pluginfile.php" in resp.url:
        filename = Path(urlparse(resp.url).path).name
        return [{"type": "file", "fileurl": resp.url, "filename": filename, "timemodified": 0}]

    # Otherwise scrape pluginfile links from the page
    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for a in soup.find_all("a", href=re.compile(r"pluginfile\.php")):
        href = a["href"].split("?")[0]  # strip token query params
        results.append({
            "type":         "file",
            "fileurl":      a["href"],
            "filename":     Path(urlparse(href).path).name,
            "timemodified": 0,
        })
    return results


def get_session(
    username: str,
    password: str,
    cookie_path: str = "moodle_cookies.json",
) -> requests.Session:
    """
    Try to reuse a saved session; fall back to full login if the session
    is expired, missing user attributes, or the cookie file doesn't exist.
    """
    s = http.make_session()
    if http.load_cookies(s, cookie_path):
        # Validate with a non-redirecting GET so an expired session (which
        # would 302 to the IdP) fails the status check immediately.
        # Also require "userid" in the page — a session created without the
        # SAML user-attribute assertion passes the sesskey check but has no
        # userid and all AJAX calls will fail.
        resp = s.get(f"{MOODLE_BASE}/my/", allow_redirects=False)
        if resp.status_code == 200 and "sesskey" in resp.text and '"userid"' in resp.text:
            log.debug("Reusing saved session from %s", cookie_path)
            return s
        log.info("Saved session invalid or incomplete, logging in again")

    s = login(username, password)
    http.save_cookies(s, cookie_path)
    return s


# ── main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG", "").lower() == "true" else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    username = os.environ.get("TUM_USERNAME")
    password = os.environ.get("TUM_PASSWORD")
    if not username or not password:
        sys.exit("Set TUM_USERNAME and TUM_PASSWORD environment variables first.")

    output_dir = os.environ.get("MOODLE_OUTPUT_DIR", "resources")

    session = get_session(username, password)
    sesskey, userid = get_sesskey_and_userid(session)
    log.info("sesskey=%s userid=%d", sesskey, userid)

    courses = get_enrolled_courses(session, sesskey, userid)
    print(f"\nEnrolled in {len(courses)} course(s):")
    for c in courses:
        print(f"  [{c.get('id', '?'):>6}] {c.get('fullname') or c.get('shortname')}")

    print(f"\nDownloading course files to '{output_dir}/' ...")
    db = Database()
    download_course_files(session, courses, output_dir=output_dir, db=db)