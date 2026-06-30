# Slack Artist CRM

A Slack-native CRM for tracking artists and clients through an acquisition pipeline. Built to replace manual status updates with a conversational interface — ask questions, log interactions, and pull reports directly from Slack without touching a spreadsheet.

---

## How it works

The system runs as a Slack Socket Mode bot. Team members interact with it in plain language — the bot uses Claude to classify intent, routes the request to the right operation, and reads or writes to a Google Sheets backend.

**Intent classification** — `slack_intent.py` uses Claude Haiku to parse incoming messages and identify what the user is trying to do (look up an artist, log a conversation, update a status, pull a report, etc.).

**Data layer** — `artist_crm.py` handles all reads and writes against Google Sheets. Four tabs: Artists, Conversations, Marketing, and Team. Fuzzy artist name matching via rapidfuzz so partial or misspelled names still resolve correctly.

**File ingestion** — `slack_files.py` picks up files dropped in Slack (press shots, one-sheets, etc.) and routes them to the corresponding artist's Google Drive folder.

**Summaries** — `slack_agent.py` uses Claude Sonnet to generate conversation summaries and status overviews on request.

---

## Stack

- **Claude** (Anthropic) — intent classification (Haiku) and summaries (Sonnet)
- **Slack Bolt** — Socket Mode bot framework
- **Google Sheets** — persistent data store (Artists, Conversations, Marketing, Team tabs)
- **Google Drive** — file storage for artist assets
- **rapidfuzz** — fuzzy artist name matching

---

## Structure

- tools/ — 5 Python modules: the bot entry point, intent classifier, CRM data layer, file ingestion, and broadcast helper
- workflows/ — 2 markdown docs: sheet schema and Slack app setup SOP
- server/.env.example — all 12 required environment variables with descriptions

---

## Setup

1. Clone the repo
2. Copy `server/.env.example` to `server/.env` and fill in your keys
3. Install deps: `pip install -r tools/requirements.txt`
4. Set up a Slack app with Socket Mode enabled and the required bot scopes (see `workflows/slack_agent.md`)
5. Complete Google OAuth flow on first run — a token file will be written locally
6. Run: `python tools/slack_agent.py`

---
