"""Shared HTTP utilities for TUMsolidator connectors."""

import json
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def make_session(extra_headers: dict = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    if extra_headers:
        s.headers.update(extra_headers)
    return s


def save_cookies(session: requests.Session, path: str) -> None:
    with open(path, "w") as f:
        json.dump(requests.utils.dict_from_cookiejar(session.cookies), f)
    log.debug("Cookies saved to %s", path)


def load_cookies(session: requests.Session, path: str) -> bool:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return False
    session.cookies.update(requests.utils.cookiejar_from_dict(data))
    log.debug("Cookies loaded from %s", path)
    return True


def download_file(
    session: requests.Session,
    url: str,
    dest: Path,
    timemodified: int = 0,
) -> bool:
    """Download url to dest, skipping if already up-to-date. Streams content."""
    dest = Path(dest)
    if dest.exists():
        if not timemodified or int(dest.stat().st_mtime) >= timemodified:
            log.debug("Skipping (exists): %s", dest)
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = session.get(url, stream=True, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Failed to download %s: %s", url, exc)
        return False
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)
    if timemodified:
        os.utime(dest, (timemodified, timemodified))
    return True
