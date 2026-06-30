# Artist CRM — Sheet Schema

This file locks the canonical column order for the Google Sheet that backs
`tools/artist_crm.py`. If you change a column name or add/remove a column, you
must update both this doc and `ARTIST_COLUMNS` / `CONVERSATION_COLUMNS` in
`tools/artist_crm.py` (and downstream callers).

## Workbook

One Google Sheet, two tabs. Created by `python3 tools/artist_crm.py --provision`.

## Tab: `Artists`

One row per artist. Primary key: `artist_id`. Row 1 is frozen (header).

| Column | Purpose | Notes |
|---|---|---|
| `artist_id` | Primary key — Instagram handle without `@` | Immutable. Lowercase by convention. |
| `name` | Display name | "Sam Harper" |
| `email` | Primary contact email | |
| `location` | Where the artist is based | Free text, e.g. "Toronto, CA" |
| `genre` | Primary genre / sound | Free text, e.g. "alt-pop" |
| `instagram_url` | Full IG URL | `https://instagram.com/samharper` |
| `instagram_followers` | Follower count snapshot | String — agent doesn't auto-refresh |
| `tiktok_url` | Full TikTok URL | `https://tiktok.com/@samtok` |
| `tiktok_followers` | TikTok follower count snapshot | String |
| `spotify_url` | Spotify artist link | `https://open.spotify.com/artist/...` |
| `spotify_monthly_listeners` | Spotify monthly listener count | String |
| `press_pics_drive_url` | Drive **folder** link → "Press Shots" | Folder auto-created on first upload; files accumulate. Set by `set_artist_file_url`. |
| `marketing_material_drive_url` | Drive **folder** link → "Marketing Material" | Promo / ads / graphics. Auto-created on first upload. |
| `artist_info_drive_url` | Drive **folder** link → "Artist Info" | Includes track data / masters / stems (renamed from `track_data_drive_url`). |
| `rise_material_drive_url` | Drive **folder** link → "Rise Material" | Content ideas / references / mockups (renamed from `content_ideas_drive_url`). |
| `onboarded_by` | Team member who onboarded the artist | Free-text or Slack handle |
| `rise_associate` | Team member acting as the artist's Rise Associate | Comma-separated if multiple |
| `tier` | Support tier or category | e.g. "Tier 1", "Tier 2" |
| `created_at` | PT ISO8601 timestamp of row creation | Immutable. Auto-set on `add_artist`. |
| `last_updated_at` | PT ISO8601 of most recent field change | Auto-refreshed on every `update_artist_field` call |

## Tab: `Conversations`

One row per logged touchpoint. **Append-only** — natively race-safe under
concurrent Slack writes. Row 1 is frozen (header).

| Column | Purpose | Notes |
|---|---|---|
| `artist_id` | Foreign key to `Artists.artist_id` | Joined on read |
| `date_iso` | PT ISO8601 timestamp of the conversation | Auto-set to "now" in `America/Los_Angeles` if not provided |
| `author_slack_id` | Slack user ID of the person logging | e.g. `U01ABC` — opaque, canonical |
| `created_by` | Human-readable display name of the author | Resolved from Slack at log time via `users.info` (cached). Surface for sheet viewers; the agent uses this in summaries. |
| `channel` | Slack channel ID where it was logged | DMs have IDs starting with `D` |
| `summary` | One-line distillation of the touchpoint | Classifier extracts this from natural-language input |

## Tab: `Marketing`

One row per marketing feature / placement secured for an artist. **Append-only**,
like Conversations. Row 1 is frozen (header). Lets the team log "Dru was featured on
a paid campaign" / "Yas was on the Daily Rush" and later ask aggregate questions
(counts by type, which artists were never featured, etc.).

| Column | Purpose | Notes |
|---|---|---|
| `artist_id` | Foreign key to `Artists.artist_id` | Joined on read |
| `date_iso` | PT ISO8601 timestamp of the feature | Auto-set to "now" in `America/Los_Angeles` if not provided |
| `author_slack_id` | Slack user ID of the person logging | e.g. `U01ABC` |
| `created_by` | Human-readable display name of the author | Resolved from Slack at log time |
| `channel` | Slack channel ID where it was logged | |
| `feature_type` | **Controlled vocabulary** | One of `paid_campaign`, `daily_press`, `social`, `other`. Classifier auto-tags; unknown values coerced to `other` in `append_marketing`. Keeps counts reliable. |
| `placement` | Specific named outlet / campaign | Free text, e.g. "Daily Rush", "Spring IG ads". May be blank. |
| `summary` | One-line distillation of the feature | Classifier extracts this from natural-language input |

The controlled `feature_type` set lives in `artist_crm.MARKETING_FEATURE_TYPES` and is
mirrored in `slack_intent.MARKETING_FEATURE_TYPES`. The agent reasons over this tab for
`query_marketing` and adds a `*Marketing:*` recap line to the per-artist summary.

## Tab: `Team`

Maps Slack users to roles for access control. Managed by admins through the bot
("make @Jane marketing"); admins may also be seeded in `server/.env` as
`SLACK_ADMIN_USER_IDS`. Row 1 is frozen (header).

| Column | Purpose | Notes |
|---|---|---|
| `user_id` | Slack user ID | Primary key, e.g. `U06ABC` |
| `name` | Display name | Resolved from Slack at assign time |
| `roles` | Comma-separated roles | Subset of `admin`, `marketing`, `rise`, `onboarding`. A user may hold several. |
| `added_by` | Admin who set the role | Display name |
| `added_at` | PT ISO8601 of last change | |

Valid roles live in `artist_crm.VALID_ROLES` (mirrored in `slack_intent.VALID_ROLES`).
Permission rules are enforced in `slack_agent.py` (`ROLE_PERMISSIONS`, `FILE_CATEGORY_ROLES`).
Removing a user's last role deletes their row.

## Pruning

Conversations are only deleted explicitly via `artist_crm.delete_conversation(artist_id, date_iso)`. The agent doesn't auto-prune yet; tied to a future "compact stale entries" feature once we see real volume. The `Marketing` tab has no delete helper yet (edit the sheet directly if needed).

## Adding a new field

1. Add the column name to the appropriate list in `tools/artist_crm.py`.
2. Update the table in this file.
3. If user-editable from Slack, also add the field to `ALLOWED_FIELDS` in `tools/slack_intent.py` and update the system prompt's example.
4. For existing sheets: manually add the column header to the next free position before the tool tries to read it (or re-provision into a fresh sheet).
