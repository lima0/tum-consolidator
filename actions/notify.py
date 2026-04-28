import json
import logging
import re
import shutil
import subprocess
from datetime import datetime

log = logging.getLogger(__name__)


def notify(title: str, message: str, subtitle: str = "") -> None:
    if shutil.which("terminal-notifier"):
        cmd = [
            "terminal-notifier",
            "-title", title,
            "-message", message,
            "-sound", "default",
            "-group", "tumsolidator",
        ]
        if subtitle:
            cmd += ["-subtitle", subtitle]
        subprocess.run(cmd, capture_output=True)
    else:
        safe_msg   = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        script = f'display notification "{safe_msg}" with title "{safe_title}"'
        subprocess.run(["osascript", "-e", script], capture_output=True)


def add_reminder(title: str, due: str) -> bool:
    """Create a macOS Reminder. due format: 'YYYY-MM-DD HH:MM'."""
    try:
        dt = datetime.strptime(due, "%Y-%m-%d %H:%M")
        apple_date = dt.strftime("%-d %B %Y at %H:%M:%S")
    except ValueError as e:
        log.warning("Cannot parse due date %r: %s", due, e)
        return False

    safe_title = title.replace('"', '\\"')
    script = f"""
tell application "Reminders"
    tell list "tasks"
        make new reminder with properties {{name:"{safe_title}", due date:date "{apple_date}"}}
    end tell
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    if result.returncode != 0:
        log.warning("Reminders AppleScript failed: %s", result.stderr.decode().strip())
    return result.returncode == 0


def read_completed_reminders(list_name: str = "tasks") -> list[str]:
    """
    Return titles of completed reminders in list_name.
    Completed reminders are those where `completed is true`.
    """
    safe_list = list_name.replace('"', '\\"')
    script = f"""
tell application "Reminders"
    set output to ""
    try
        set rl to list "{safe_list}"
        set doneItems to (every reminder of rl whose completed is true)
        repeat with r in doneItems
            set output to output & name of r & linefeed
        end repeat
    end try
    return output
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    if result.returncode != 0:
        log.warning("read_completed_reminders AppleScript failed: %s", result.stderr.decode().strip())
        return []

    raw = result.stdout.decode().strip()
    if not raw:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def clear_completed_reminders(list_name: str = "tasks") -> int:
    """Delete completed reminders from list_name. Returns count deleted."""
    safe_list = list_name.replace('"', '\\"')
    script = f"""
tell application "Reminders"
    set deleted to 0
    try
        set rl to list "{safe_list}"
        set doneItems to (every reminder of rl whose completed is true)
        repeat with r in doneItems
            delete r
            set deleted to deleted + 1
        end repeat
    end try
    return deleted
end tell
"""
    result = subprocess.run(["osascript", "-e", script], capture_output=True)
    if result.returncode != 0:
        log.warning("clear_completed_reminders failed: %s", result.stderr.decode().strip())
        return 0
    raw = result.stdout.decode().strip()
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 0
