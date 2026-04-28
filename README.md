# TUMsolidator

Consolidates Artemis, Moodle, and the TUM Campus Calendar into an LLM-powered study assistant.

## Features

- **Chat** — ask anything about your assignments, scores, schedule, or course materials
- **Briefing** — daily summary of deadlines, schedule, and priority recommendation
- **Agent** — autonomous mode: reads new PDFs, fires notifications, creates reminders, writes a digest
- **Check** — lightweight background check (no LLM): detects score/status changes, sets deadline reminders
- **Push notifications** — `terminal-notifier` or macOS native fallback
- **macOS Reminders** — deadline-proximity reminders synced to iPhone
- **PDF access** — lecture slides and tutorials uploaded to Anthropic Files API on demand

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```env
ANTHROPIC_API_KEY=sk-ant-...
TUM_USERNAME=go42tum
TUM_PASSWORD=your-password
TUM_CALENDAR=https://campus.tum.de/tumonline/wbKalender.ical?...
```

`TUM_CALENDAR` is the personal ICS URL from TUMOnline → Calendar → Export.

Optional — install `terminal-notifier` for richer notifications:

```bash
brew install terminal-notifier
```

## Usage

```bash
# Interactive chat
python main.py

# Daily briefing (single LLM call, then exit)
python main.py --brief

# Autonomous agent (LLM-driven: summaries, notifications, digest)
python main.py --agent

# Lightweight check (no LLM: score diffs + deadline reminders)
python main.py --check
```

## Background scheduling (launchd)

```bash
# Install both jobs (check every 2h + agent at 08:00 and 19:00)
python scripts/install_launchd.py

# Install individually
python scripts/install_launchd.py check
python scripts/install_launchd.py agent

# Remove all
python scripts/install_launchd.py uninstall
```

Logs land in `logs/check.log` and `logs/agent.log`.

## Sources

| Source | What it provides |
|---|---|
| Artemis | Assignments, scores, submission status, lecture attachments |
| Moodle | Course files (synced in background on chat startup) |
| TUM Campus Calendar | Schedule for the next 7 days |

## Data layout

```
resources/          # downloaded course materials (Artemis + Moodle)
  COURSE/
    LECTURE_ID/     # Artemis lecture attachments
    MODULE_NAME/    # Moodle module files
agent_memory.md     # agent digest history (last 7 entries injected into context)
state.json          # exercise snapshots + known files for diffing
.file_id_cache.json # Anthropic Files API upload cache
logs/               # launchd stdout/stderr
```
