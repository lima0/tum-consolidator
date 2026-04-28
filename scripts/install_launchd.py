#!/usr/bin/env python3
"""
Install TUMsolidator background jobs as launchd agents.

Jobs:
  check  — every 2h: detect Artemis changes, set reminders, create Calendar events
  agent  — 08:00 + 19:00: LLM-driven digest, PDF summaries, dashboard
  sync   — every 30min: read completed Reminders back into actions_taken
  plan   — Sunday 22:00: weekly plan synthesis → Calendar events

Usage:
    python scripts/install_launchd.py [check|agent|sync|plan|all]  # install
    python scripts/install_launchd.py uninstall                     # remove all
"""
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PYTHON      = sys.executable
LOGS_DIR    = PROJECT_DIR / "logs"
AGENTS_DIR  = Path.home() / "Library" / "LaunchAgents"

JOBS = {
    "check": {
        "label":    "com.tumsolidator.check",
        "flag":     "--check",
        "interval": 7200,          # every 2 hours
        "at_load":  True,
    },
    "agent": {
        "label":    "com.tumsolidator.agent",
        "flag":     "--agent",
        "hours":    [8, 19],       # 08:00 and 19:00
        "at_load":  False,
    },
    "sync": {
        "label":    "com.tumsolidator.sync",
        "flag":     "--sync",
        "interval": 1800,          # every 30 minutes
        "at_load":  False,
    },
    "plan": {
        "label":    "com.tumsolidator.plan",
        "flag":     "--plan",
        "hours":    [22],          # Sunday 22:00
        "weekdays": [1],           # 1=Sunday in launchd
        "at_load":  False,
    },
}


def _plist_interval(job: dict) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{job["label"]}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON}</string>
        <string>{PROJECT_DIR / "main.py"}</string>
        <string>{job["flag"]}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT_DIR}</string>
    <key>StartInterval</key>
    <integer>{job["interval"]}</integer>
    <key>RunAtLoad</key>
    <{"true" if job["at_load"] else "false"}/>
    <key>StandardOutPath</key>
    <string>{LOGS_DIR / (job["label"].split(".")[-1] + ".log")}</string>
    <key>StandardErrorPath</key>
    <string>{LOGS_DIR / (job["label"].split(".")[-1] + ".err")}</string>
</dict>
</plist>
"""


def _plist_calendar(job: dict) -> str:
    weekdays = job.get("weekdays", [])
    entries = []
    for h in job["hours"]:
        if weekdays:
            for wd in weekdays:
                entries.append(
                    f"    <dict><key>Weekday</key><integer>{wd}</integer>"
                    f"<key>Hour</key><integer>{h}</integer>"
                    f"<key>Minute</key><integer>0</integer></dict>"
                )
        else:
            entries.append(
                f"    <dict><key>Hour</key><integer>{h}</integer>"
                f"<key>Minute</key><integer>0</integer></dict>"
            )
    cal_entries = "\n".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{job["label"]}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{PYTHON}</string>
        <string>{PROJECT_DIR / "main.py"}</string>
        <string>{job["flag"]}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{PROJECT_DIR}</string>
    <key>StartCalendarInterval</key>
    <array>
{cal_entries}
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{LOGS_DIR / (job["label"].split(".")[-1] + ".log")}</string>
    <key>StandardErrorPath</key>
    <string>{LOGS_DIR / (job["label"].split(".")[-1] + ".err")}</string>
</dict>
</plist>
"""


def _install_job(name: str) -> None:
    job       = JOBS[name]
    plist     = AGENTS_DIR / f"{job['label']}.plist"
    content   = _plist_calendar(job) if "hours" in job else _plist_interval(job)

    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    plist.write_text(content)

    result = subprocess.run(["launchctl", "load", str(plist)])
    if result.returncode == 0:
        schedule = f"every {job['interval']//3600}h" if "interval" in job else f"at {', '.join(f'{h:02d}:00' for h in job['hours'])}"
        print(f"  {name}: loaded ({schedule})")
    else:
        print(f"  {name}: launchctl load failed — check {plist}")


def install(targets: list[str]) -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    print("Installing:")
    for name in targets:
        _install_job(name)
    print(f"Logs: {LOGS_DIR}/")


def uninstall() -> None:
    for job in JOBS.values():
        plist = AGENTS_DIR / f"{job['label']}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
    print("Uninstalled all TUMsolidator agents.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg == "uninstall":
        uninstall()
    elif arg in JOBS:
        install([arg])
    else:
        install(list(JOBS.keys()))
