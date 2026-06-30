# Slack Artist CRM Agent

A long-running Slack bot that team members talk to in natural language to log
conversations with artists, look up status, and store press pics / track files.
Backed by a single Google Sheet (Artists + Conversations tabs).

## When to use

Any time a team member finishes a touchpoint with an artist, they should drop a
note in Slack (channel @mention or DM to the bot). When anyone needs to know where
things stand with an artist, they ask the bot. The sheet is the source of truth.

## Architecture (WAT)

```
Slack (Socket Mode)
   │  @mention / DM / file_shared
   ▼
tools/slack_agent.py        ← long-running listener; auth + routing + audit
   │  text → intent JSON
   ▼
tools/slack_intent.py       ← Haiku classifier (claude-haiku-4-5-20251001)
   │
   ├── tools/artist_crm.py      ← Sheets CRUD (Artists + Conversations tabs)
   └── tools/slack_files.py     ← Slack file → Drive upload → URL
```

Claude only does two things: (1) classify intent (Haiku), (2) synthesize the
status summary on `query_artist` (Sonnet). All other work is deterministic
Python.

## Sheet schema

See [sheet_schema.md](sheet_schema.md). Two tabs; primary key on `Artists` is
`artist_id` (the Instagram handle without `@`). `Conversations` is append-only.

## One-time setup

### 1. Create the Slack app

1. https://api.slack.com/apps → **Create New App** → **From scratch** → pick the workspace.
2. **OAuth & Permissions → Bot Token Scopes**: add
   - `app_mentions:read`
   - `channels:history`
   - `chat:write`
   - `files:read`
   - `files:write`
   - `im:history`
   - `im:read`
   - `im:write`
   - `users:read`
3. **Event Subscriptions** → toggle ON. Subscribe to bot events:
   - `app_mention`
   - `message.im`
   - `file_shared`
4. **Socket Mode** → toggle ON → generate App-Level Token with `connections:write` scope. Save as `SLACK_APP_TOKEN`.
5. **Install to Workspace** → copy the Bot User OAuth Token → save as `SLACK_BOT_TOKEN`.
6. In Slack: `/invite @umbra` in every channel you want the bot to listen in.

### 2. Configure `server/.env`

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_AUTHORIZED_USER_IDS=U01ABC,U02DEF       # comma-separated Slack user IDs, or "*" for everyone in the workspace
SLACK_WORKSPACE_ID=T01XXXX                    # optional second guard (recommended when using "*")
SLACK_ADMIN_USER_IDS=U01ABC                   # seed admin(s) — always admin regardless of the Team tab
ARTIST_CRM_SHEET_URL=                         # auto-filled by --provision
ARTIST_CRM_TEAM_EMAILS=                       # optional — only needed if teammates want direct sheet access
ARTIST_CRM_DRIVE_FOLDER_ID=                   # optional — where uploads land
```

To find a Slack user ID: in Slack, click the user → **More** → **Copy member ID**.

To open the bot to **everyone in the workspace**, set `SLACK_AUTHORIZED_USER_IDS=*` (no
need to list IDs). Socket Mode only receives events from the workspace the app is installed
in, so "everyone" is bounded to that workspace; keep `SLACK_WORKSPACE_ID` set as a guard.

### 3. Install dependencies

```bash
pip3 install -r tools/requirements.txt
```

### 4. Provision the Google Sheet

```bash
python3 tools/artist_crm.py --provision
```

Opens an OAuth browser flow on first run (saves token to
`tools/.google_token_crm.json`). Creates the workbook, adds both tabs with
frozen headers, writes the resulting URL back into `server/.env` as
`ARTIST_CRM_SHEET_URL`. The agent uses its own OAuth token to read/write the
sheet, so the team doesn't need direct sheet access — they only talk to the bot.

If you do want one or more teammates to view/edit the raw sheet (for audit or
manual backup access), set `ARTIST_CRM_TEAM_EMAILS=alice@x.com,bob@x.com`
**before** running `--provision`; it'll share the new sheet with them as editors.

### 5. Smoke test the CRM library

```bash
python3 tools/artist_crm.py --self-test
```

Creates a throwaway sheet, exercises every CRUD function, deletes the sheet.
Should print `[OK] All self-test assertions passed.`

### 6. Run the agent

Local:
```bash
caffeinate -i python3 tools/slack_agent.py
```

Cloud (systemd) — mirror the deployment pattern in
[`../trading/deploy_agent.md`](../trading/deploy_agent.md).

## Daily operation — natural-language examples

| You say (DM or @mention) | What happens |
|---|---|
| `add new artist Sam Harper, instagram samharper, email sam@x.com, onboarded by alice` | New row in Artists tab |
| `logged a 30min call with Sam about cover art, he'll send refs Friday` | New row in Conversations tab |
| `Sam sent the cover art refs today` | New row in Conversations tab |
| `where are we at with Sam?` | Sonnet-synthesized summary — marks refs as received, not outstanding; includes a `*Marketing:*` line if any features are logged |
| `Sam was featured on a paid marketing campaign` | New row in Marketing tab (`feature_type=paid_campaign`) |
| `Yas is on the Daily Rush` | New row in Marketing tab (`feature_type=daily_press`, `placement="Daily Rush"`) |
| `the Daily Rush this week had Dru, Yas, and Sam` | One Marketing row per named artist (same feature/placement) |
| `how many campaigns has Sam been on?` | Counts Sam's `paid_campaign` features |
| `which artists haven't been on a paid campaign?` | Lists artists with zero `paid_campaign` features |
| `make @Jane marketing` (admin) | Grants Jane the marketing role (Team tab) |
| `what's my role?` / `what can I do?` | Shows the asker's roles + capabilities |
| `who's on the rise team?` | Lists members of that role |
| `update Sam support_level to Tier 1` | Cell update + `last_updated_at` refreshed |
| Drop image(s) with caption `press shots for Sam` | Files added to Sam's *Press Shots* folder; link → `press_pics_drive_url` |
| Drop file(s) with caption `marketing material for Sam` | Files added to Sam's *Marketing Material* folder; link → `marketing_material_drive_url` |
| Drop audio / `here's Sam's track data` | Files added to Sam's *Artist Info* folder; link → `artist_info_drive_url` (track data lives in Artist Info) |
| Drop file(s) with caption `rise material for Sam` | Files added to Sam's *Rise Material* folder; link → `rise_material_drive_url` |

**Drive folder layout (Shared Drive).** Files are organized inside the shared drive as:
```
Artist Repository/                 (holds the CRM sheet)
  Artist Assets/                   (auto-created)
    <name> [<artist_id>]/          (per artist)
      Press Shots/        ← press_pics_drive_url
      Marketing Material/ ← marketing_material_drive_url
      Artist Info/        ← artist_info_drive_url  (track data / masters / stems)
      Rise Material/      ← rise_material_drive_url (content ideas / references)
```
Each sheet field stores the link to its category subfolder; repeat uploads accumulate in the same folder (link unchanged). Folders are found-or-created by name (stable across restarts). The "Artist Assets" folder is auto-created inside the "Artist Repository" folder (discovered as the sheet's parent) and its ID persisted to `server/.env` as `ARTIST_CRM_ASSETS_FOLDER_ID`. Access comes from **Shared Drive membership** — no per-folder public sharing. Drop **multiple files in one message** and they all land together. A caption-less / non-audio upload prompts "which folder?" (Press Shots / Marketing Material / Artist Info / Rise Material).

**Drive access:** the agent authenticates to Google as `timmy.l@revomusic.ai` with the full `drive` scope, so it can create folders inside the shared drive. That account must remain a member of the shared drive with Content Manager rights.
| `share the spreadsheet link` / `where can I view the data?` | Replies with the view-only sheet URL |

The sheet is set to **"anyone with the link can view"** (read-only), so the link the bot
shares works for anyone it's given to. The bot only hands it to authorized Slack users, but
note the link itself is not access-controlled — treat it as internal. To lock it down again,
remove the `anyone` permission in the sheet's Share settings.

## How artist matching works

The agent uses `rapidfuzz` against the `name` and `artist_id` columns
(threshold 75). Exact case-insensitive matches resolve immediately. Multiple
fuzzy matches trigger a clarification prompt listing the candidates.

If the artist isn't found at all, the agent suggests using `add new artist ...`.

## How the smart summary works

When you ask "where are we at with Sam?", the agent:
1. Resolves the artist via fuzzy match.
2. Pulls the artist row + all `Conversations` rows for that `artist_id`, sorted chronologically.
3. Passes them to `claude-sonnet-4-6` with a prompt that says: "newer entries override older ones; don't list fulfilled promises as outstanding."
4. Replies in-thread with a tight, dated summary (~200 words).

This is the key feature: the LLM dedupes promises that have since been
delivered, so the team doesn't repeat work or re-promise things.

## How marketing tracking works

The marketing team logs **features / placements** in natural language — "Dru was
featured on a paid campaign", "Yas is on the Daily Rush". These are routed to
`log_marketing` (distinct from `log_conversation`, which is for touchpoints/promises)
and appended to the **Marketing** tab. The classifier auto-tags each one with a
controlled `feature_type` (`paid_campaign` / `daily_press` / `social` / `other`) and
extracts the named `placement` (e.g. "Daily Rush") when present — so counts stay
reliable across phrasings. A single message may name **multiple artists** ("the Daily
Rush this week had Dru, Yas, and Sam") — the agent logs one identical feature row per
named artist. If a name matches more than one artist, it logs the unambiguous ones and
**asks the user to clarify** the ambiguous one (listing the candidates); the reply, with
thread context, re-logs that artist with the same feature. Unknown names are flagged so
nothing is silently dropped.

Aggregate questions go to `query_marketing`: "how many campaigns has Dru been on?",
"how many Daily Rush features total?", "which artists haven't been on a paid campaign?",
"who's gotten the most marketing?". The agent builds a per-artist digest (counts by
type + recent features) and lets Sonnet answer over it. Every artist appears in the
digest — including those with zero features — so "never featured" queries work.

The per-artist summary ("where are we at with X?") also gains a one-line `*Marketing:*`
recap (e.g. "2 paid campaigns, 1 Daily Rush feature") when features exist.

## Roles & permissions

Write access is gated by role; reads are open to any authorized user. Roles live in the
**Team tab** and are managed by admins through the bot.

| Action | admin | marketing | rise | onboarding | no role |
|---|---|---|---|---|---|
| Add new artist | ✓ | — | — | ✓ | — |
| Update artist info (most profile fields) | ✓ | ✓ | ✓ | ✓ | — |
| Update `tier` | ✓ | — | ✓ | — | — |
| Update `rise_associate` | ✓ | — | ✓ | ✓ | — |
| Log a conversation | ✓ | — | ✓ | ✓ | — |
| Log a marketing feature | ✓ | ✓ | — | — | — |
| Upload → Marketing Material | ✓ | ✓ | — | — | — |
| Upload → Press Shots / Artist Info | ✓ | — | ✓ | ✓ | — |
| Upload → Rise Material | ✓ | — | ✓ | — | — |
| Manage roles (`make @x marketing`) | ✓ | — | — | — | — |
| Look things up (queries, links) | ✓ | ✓ | ✓ | ✓ | ✓ |

Most profile fields are editable by any write role; `tier` (rise/admin) and
`rise_associate` (rise/onboarding/admin) are restricted — see `FIELD_PERMISSIONS` in
`slack_agent.py`. In a multi-field update, the bot applies the fields you're allowed to
set and reports the ones you aren't. Denied actions reply with guidance (e.g. *"Logging
conversations is for the rise or onboarding team — ask an admin to grant you the role"*),
never a silent drop.

**Assigning roles (admin only):**
- `make @Jane marketing` / `add @Bob to the rise team` / `@Sam is now an admin`
- `remove @Jane from marketing`
- Mention the person with Slack's `@` autocomplete so the bot gets their user ID.

**Self-service:** anyone can ask `what's my role?` / `what can I do?`, and `who's on the
marketing team?` / `list the team`.

**Admin bootstrap:** set `SLACK_ADMIN_USER_IDS=U…,U…` in `server/.env` — these users are
always admin regardless of the Team tab (so you can never lock yourself out). The very first
admin must be seeded this way; after that, admins can grant roles via the bot.

## File upload edge cases

If a file is dropped without a clear artist or folder, the agent stashes it
in-memory **keyed by `channel`** (so it works in DMs, which have no threads) and
asks the missing question — "Which artist is this for?" and/or "Which folder?".
The stash remembers the **resolved artist** and any known category, so a terse
follow-up like `press pics` or `Dru` completes the upload. Multi-step works too:
drop file → "Which artist?" → `Dru` → "Which folder?" → `press shots` → uploaded.
The bot never loses the files mid-flow; if a reply is still incomplete it re-asks.

In-memory means: **if the agent process restarts before the follow-up arrives,
the pending file is lost.** The user can re-drop it.

## Concurrency

- `Conversations` tab uses `values.append` with `INSERT_ROWS` — naturally
  race-safe under simultaneous writes from multiple team members.
- `Artists` tab updates use read-then-write on a single cell. Two simultaneous
  edits to the same artist row will last-write-wins. Accepted for v1: edits are
  rare and operators are humans on Slack.

## Audit log

Every step is logged via `tools/audit_log.py`. Tail recent activity:

```bash
python3 tools/audit_log.py --tail 30
```

Event types written by the agent:
- `slack_in` — message received from an authorized user
- `slack_rejected` — message from a non-allowlisted user (bot stays silent)
- `slack_decision` — intent classifier output
- `slack_action` — successful handler completion (`append_conversation`, `add_artist`, `update_artist_field`, `set_artist_file_url`, `summarize_artist`)
- `slack_file_event` — `file_shared` event ack
- `slack_error` — exception trace

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Bot doesn't respond in a channel | Did you `/invite @umbra`? Is the user in `SLACK_AUTHORIZED_USER_IDS`? |
| Bot doesn't respond to DM | Confirm `message.im` subscription is enabled; restart the agent after toggling. |
| "ARTIST_CRM_SHEET_URL not set" on startup | Run `python3 tools/artist_crm.py --provision`. |
| OAuth re-prompt every run | Token file `tools/.google_token_crm.json` may have been deleted. Re-auth and keep it. |
| Wrong artist matched | Use the full Instagram handle, or run `python3 tools/artist_crm.py --find <text>` to see what the matcher returns. |
| Summary is stale / wrong | Tail audit log for `slack_decision` + `slack_action` — confirm conversations were logged. |
| File upload silently fails | Check Drive scopes on the CRM token — must include `drive.file`. Re-provision to re-prompt OAuth. |

## CLI escape hatches

`tools/artist_crm.py` works without the Slack agent for ops + debugging:

```bash
python3 tools/artist_crm.py --list                       # all artists
python3 tools/artist_crm.py --find "sam"                 # fuzzy lookup
python3 tools/artist_crm.py --summary samharper          # artist + all conversations
```
