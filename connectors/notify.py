"""macOS push notifications. Tries terminal-notifier first, falls back to osascript."""

import logging
import subprocess

log = logging.getLogger(__name__)


def _esc(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _osascript(title: str, message: str) -> None:
    script = f"display notification {_esc(message)} with title {_esc(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception as exc:
        log.warning("Notification failed: %s", exc)


def notify(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["terminal-notifier", "-title", title, "-message", message, "-sound", "default"],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        _osascript(title, message)
    except subprocess.CalledProcessError as exc:
        log.warning("terminal-notifier error: %s", exc)
        _osascript(title, message)
