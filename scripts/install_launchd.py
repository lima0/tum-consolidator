#!/usr/bin/env python3
"""
Install or remove TUMsolidator launchd agents.

Usage:
    python scripts/install_launchd.py           # install both
    python scripts/install_launchd.py sync       # install sync job only
    python scripts/install_launchd.py brief      # install brief job only
    python scripts/install_launchd.py uninstall  # remove all
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PYTHON      = str(PROJECT_DIR / ".venv" / "bin" / "python3")
AGENTS_DIR  = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR    = PROJECT_DIR / "logs"

JOBS = {
    "sync": {
        "label":    "com.tumsolidator.sync",
        "plist":    AGENTS_DIR / "com.tumsolidator.sync.plist",
        "args":     [PYTHON, str(PROJECT_DIR / "main.py"), "--sync", "--limit", "5"],
        "interval": 4 * 3600,   # every 4 hours
        "log":      LOGS_DIR / "sync.log",
    },
    "brief": {
        "label": "com.tumsolidator.brief",
        "plist": AGENTS_DIR / "com.tumsolidator.brief.plist",
        "args":  [PYTHON, str(PROJECT_DIR / "main.py"), "--brief"],
        "hour":  8,
        "log":   LOGS_DIR / "brief.log",
    },
}


def _plist_sync(job: dict) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>{job["label"]}</string>
    <key>ProgramArguments</key>
    <array>
        {"".join(f"<string>{a}</string>" for a in job["args"])}
    </array>
    <key>WorkingDirectory</key>  <string>{PROJECT_DIR}</string>
    <key>StartInterval</key>     <integer>{job["interval"]}</integer>
    <key>StandardOutPath</key>   <string>{job["log"]}</string>
    <key>StandardErrorPath</key> <string>{job["log"]}</string>
    <key>RunAtLoad</key>         <false/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
"""


def _plist_brief(job: dict) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>             <string>{job["label"]}</string>
    <key>ProgramArguments</key>
    <array>
        {"".join(f"<string>{a}</string>" for a in job["args"])}
    </array>
    <key>WorkingDirectory</key>  <string>{PROJECT_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>   <integer>{job["hour"]}</integer>
        <key>Minute</key> <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>   <string>{job["log"]}</string>
    <key>StandardErrorPath</key> <string>{job["log"]}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
"""


def install(name: str) -> None:
    job = JOBS[name]
    LOGS_DIR.mkdir(exist_ok=True)
    AGENTS_DIR.mkdir(exist_ok=True)

    content = _plist_sync(job) if name == "sync" else _plist_brief(job)
    job["plist"].write_text(content)
    print(f"Wrote {job['plist']}")

    # unload first in case already registered
    subprocess.run(["launchctl", "unload", str(job["plist"])],
                   capture_output=True)
    result = subprocess.run(["launchctl", "load", str(job["plist"])],
                            capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ERROR: {result.stderr.strip()}")
    else:
        print(f"  Loaded: {job['label']}")
        if name == "sync":
            print(f"  Runs every 4 hours. Logs → {job['log']}")
        else:
            print(f"  Runs daily at 08:00. Logs → {job['log']}")


def uninstall() -> None:
    for name, job in JOBS.items():
        if job["plist"].exists():
            subprocess.run(["launchctl", "unload", str(job["plist"])],
                           capture_output=True)
            job["plist"].unlink()
            print(f"Removed {job['label']}")
        else:
            print(f"Not installed: {job['label']}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"

    if not Path(PYTHON).exists():
        sys.exit(f"venv not found at {PYTHON} — run: pip install -r requirements.txt")

    if cmd == "uninstall":
        uninstall()
    elif cmd in JOBS:
        install(cmd)
    elif cmd == "all":
        for name in JOBS:
            install(name)
    else:
        sys.exit(f"Unknown command: {cmd}. Use: sync | brief | uninstall")


if __name__ == "__main__":
    main()
