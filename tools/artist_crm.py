#!/usr/bin/env python3
"""
Umbra — Artist CRM (Google Sheets-backed)

Pure CRM library used by tools/slack_agent.py. Manages two tabs in one Google Sheet:

    Artists tab       — one row per artist (primary key = artist_id = IG handle)
    Conversations tab — one row per logged touchpoint (append-only, race-safe)

Reuses auth + service builders from tools/crm_common.py.

CLI:
    python3 tools/artist_crm.py --provision
    python3 tools/artist_crm.py --self-test
    python3 tools/artist_crm.py --find "sam"
    python3 tools/artist_crm.py --summary samharper
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# macOS Python.framework ships without root certs; point SSL at certifi's bundle.
# Must happen before any HTTP libraries load their SSL contexts.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

PT = ZoneInfo("America/Los_Angeles")

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Add tools dir to path so we can import from crm_common
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crm_common import (  # noqa: E402
    _load_env,
    extract_file_id,
    get_credentials,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    # Full Drive access — required to work inside a user-created folder in a
    # Shared Drive (the narrow drive.file scope can't see folders the app didn't
    # create, nor enumerate Shared Drives).
    "https://www.googleapis.com/auth/drive",
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(PROJECT_ROOT, "server", ".env")
TOKEN_PATH = os.path.join(SCRIPT_DIR, ".google_token_crm.json")

ARTISTS_TAB = "Artists"
CONVERSATIONS_TAB = "Conversations"
MARKETING_TAB = "Marketing"
TEAM_TAB = "Team"

ARTIST_COLUMNS = [
    "artist_id",
    "name",
    "email",
    "location",
    "genre",
    "instagram_url",
    "instagram_followers",
    "tiktok_url",
    "tiktok_followers",
    "spotify_url",
    "spotify_monthly_listeners",
    "press_pics_drive_url",
    "marketing_material_drive_url",
    "artist_info_drive_url",
    "rise_material_drive_url",
    "onboarded_by",
    "rise_associate",
    "tier",
    "created_at",
    "last_updated_at",
]

# Rename mapping for backwards-compatible migration of older sheets.
ARTIST_COLUMN_RENAMES = {
    "follower_engagement": "instagram_engagement",
    "working_with": "rise_associate",
    "support_level": "tier",
    "content_ideas": "rise_material_drive_url",
    "content_ideas_drive_url": "rise_material_drive_url",
    "track_data_drive_url": "artist_info_drive_url",
}

CONVERSATION_COLUMNS = [
    "artist_id",
    "date_iso",
    "author_slack_id",
    "created_by",
    "channel",
    "summary",
]

MARKETING_COLUMNS = [
    "artist_id",
    "date_iso",
    "author_slack_id",
    "created_by",
    "channel",
    "feature_type",
    "placement",
    "summary",
]

# Controlled vocabulary for marketing feature_type — keeps counts reliable.
# The classifier auto-tags each log; anything unrecognized is coerced to "other".
MARKETING_FEATURE_TYPES = {"paid_campaign", "daily_press", "social", "other"}

TEAM_COLUMNS = [
    "user_id",
    "name",
    "roles",
    "added_by",
    "added_at",
]

# Roles a team member can hold (a user may hold several). Used for access control.
VALID_ROLES = {"admin", "marketing", "rise", "onboarding"}

FILE_URL_FIELDS = {
    "press_pics_drive_url",
    "marketing_material_drive_url",
    "artist_info_drive_url",
    "rise_material_drive_url",
}


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _get_clients():
    """Return (sheets_svc, drive_svc) authenticated for CRM scopes."""
    _load_env()
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise RuntimeError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing in server/.env")

    # get_credentials reads the module-level SCOPES from crm_common at call time.
    # Patch it to this module's write SCOPES for the duration of this call.
    import crm_common
    original_scopes = crm_common.SCOPES
    crm_common.SCOPES = SCOPES
    try:
        creds = get_credentials(TOKEN_PATH, client_id, client_secret)
    finally:
        crm_common.SCOPES = original_scopes

    sheets_svc = build("sheets", "v4", credentials=creds)
    drive_svc = build("drive", "v3", credentials=creds)
    return sheets_svc, drive_svc


def _get_sheet_id() -> str:
    url = os.getenv("ARTIST_CRM_SHEET_URL", "").strip()
    if not url:
        raise RuntimeError(
            "ARTIST_CRM_SHEET_URL not set in server/.env. "
            "Run: python3 tools/artist_crm.py --provision"
        )
    return extract_file_id(url)


def _now_iso() -> str:
    """Return current PT (America/Los_Angeles) time as ISO 8601 with offset."""
    return datetime.now(PT).isoformat(timespec="seconds")


def _col_letter(index_zero: int) -> str:
    """0 -> A, 1 -> B, ..., 25 -> Z, 26 -> AA."""
    s = ""
    n = index_zero
    while True:
        s = chr(ord("A") + (n % 26)) + s
        n = n // 26 - 1
        if n < 0:
            return s


# ---------------------------------------------------------------------------
# Sheet provisioning
# ---------------------------------------------------------------------------

def provision_sheet(team_emails: list[str] | None = None, title: str | None = None) -> str:
    """Create the CRM workbook with both tabs, share with team, persist URL to .env.

    Idempotency: if ARTIST_CRM_SHEET_URL is already set in .env, returns it without
    creating a new sheet. To force provisioning, clear the env var first.
    """
    _load_env()
    existing = os.getenv("ARTIST_CRM_SHEET_URL", "").strip()
    if existing:
        print(f"Already provisioned: {existing}")
        return existing

    sheets_svc, drive_svc = _get_clients()
    title = title or f"Umbra Artist CRM — {datetime.now().strftime('%Y-%m-%d')}"

    spreadsheet = sheets_svc.spreadsheets().create(
        body={
            "properties": {"title": title},
            "sheets": [
                {"properties": {"title": ARTISTS_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": CONVERSATIONS_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": MARKETING_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": TEAM_TAB, "gridProperties": {"frozenRowCount": 1}}},
            ],
        }
    ).execute()
    sheet_id = spreadsheet["spreadsheetId"]
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    # Write headers
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{ARTISTS_TAB}!A1", "values": [ARTIST_COLUMNS]},
                {"range": f"{CONVERSATIONS_TAB}!A1", "values": [CONVERSATION_COLUMNS]},
                {"range": f"{MARKETING_TAB}!A1", "values": [MARKETING_COLUMNS]},
                {"range": f"{TEAM_TAB}!A1", "values": [TEAM_COLUMNS]},
            ],
        },
    ).execute()

    # Bold the header rows
    header_format_requests = []
    for tab_props in spreadsheet["sheets"]:
        header_format_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": tab_props["properties"]["sheetId"],
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": header_format_requests},
    ).execute()

    # Optional: share with teammates who want direct sheet access. Skipped
    # entirely if ARTIST_CRM_TEAM_EMAILS is empty — the agent reads/writes via
    # its own OAuth token so the team doesn't need sheet access to use the bot.
    emails = team_emails or _emails_from_env()
    for email in emails:
        try:
            drive_svc.permissions().create(
                fileId=sheet_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
            ).execute()
            print(f"  Shared with {email}")
        except HttpError as e:
            print(f"  Failed to share with {email}: {e}", file=sys.stderr)

    # Persist URL into .env
    _set_env_var("ARTIST_CRM_SHEET_URL", url)
    print(f"\nProvisioned: {url}")
    print(f"Saved to {ENV_PATH}")
    return url


def _emails_from_env() -> list[str]:
    raw = os.getenv("ARTIST_CRM_TEAM_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _set_env_var(key: str, value: str) -> None:
    """Append-or-update a key in server/.env in place."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _read_tab(sheets_svc, sheet_id: str, tab: str, columns: list[str]) -> list[dict]:
    """Read a tab into a list of labeled dicts. Skips the header row."""
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=tab,
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 2:
        return []
    out = []
    for row in rows[1:]:
        # Right-pad missing trailing cells
        padded = row + [""] * (len(columns) - len(row))
        out.append({columns[i]: padded[i] for i in range(len(columns))})
    return out


def read_artists() -> list[dict]:
    sheets_svc, _ = _get_clients()
    return _read_tab(sheets_svc, _get_sheet_id(), ARTISTS_TAB, ARTIST_COLUMNS)


def read_conversations(artist_id: str | None = None) -> list[dict]:
    sheets_svc, _ = _get_clients()
    all_rows = _read_tab(sheets_svc, _get_sheet_id(), CONVERSATIONS_TAB, CONVERSATION_COLUMNS)
    if artist_id:
        all_rows = [r for r in all_rows if r["artist_id"] == artist_id]
    all_rows.sort(key=lambda r: r.get("date_iso", ""))
    return all_rows


def read_marketing(artist_id: str | None = None) -> list[dict]:
    sheets_svc, _ = _get_clients()
    all_rows = _read_tab(sheets_svc, _get_sheet_id(), MARKETING_TAB, MARKETING_COLUMNS)
    if artist_id:
        all_rows = [r for r in all_rows if r["artist_id"] == artist_id]
    all_rows.sort(key=lambda r: r.get("date_iso", ""))
    return all_rows


# ---------------------------------------------------------------------------
# Team / roles
# ---------------------------------------------------------------------------

def _parse_roles(raw: str) -> set[str]:
    """Parse a comma-separated roles cell into a clean set of valid roles."""
    return {r.strip().lower() for r in (raw or "").split(",") if r.strip().lower() in VALID_ROLES}


def read_team() -> list[dict]:
    sheets_svc, _ = _get_clients()
    return _read_tab(sheets_svc, _get_sheet_id(), TEAM_TAB, TEAM_COLUMNS)


def get_user_roles(user_id: str) -> set[str]:
    """Return the set of roles assigned to a Slack user_id (empty if none/not found)."""
    user_id = (user_id or "").strip()
    if not user_id:
        return set()
    for row in read_team():
        if row.get("user_id", "").strip() == user_id:
            return _parse_roles(row.get("roles", ""))
    return set()


def set_user_roles(user_id: str, roles: set[str] | list[str], name: str = "", added_by: str = "") -> dict:
    """Upsert a Team row for user_id with the given roles. Removing all roles deletes the row.

    Returns the resulting row dict (or {"user_id", "roles": ""} when the row was removed).
    """
    user_id = (user_id or "").strip()
    if not user_id:
        raise ValueError("user_id is required")
    clean = sorted({r.strip().lower() for r in roles if r.strip().lower() in VALID_ROLES})

    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()
    rows = _read_tab(sheets_svc, sheet_id, TEAM_TAB, TEAM_COLUMNS)

    row_index = None
    for i, r in enumerate(rows):
        if r.get("user_id", "").strip() == user_id:
            row_index = i
            break

    # No roles left → delete the row if it exists; otherwise no-op.
    if not clean:
        if row_index is not None:
            _delete_team_row(sheets_svc, sheet_id, row_index)
        return {"user_id": user_id, "name": name, "roles": "", "added_by": added_by, "added_at": ""}

    now = _now_iso()
    if row_index is None:
        row = [user_id, name, ",".join(clean), added_by, now]
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{TEAM_TAB}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
    else:
        existing = rows[row_index]
        # Preserve existing name/added_by if no new value supplied.
        final_name = name or existing.get("name", "")
        final_added_by = added_by or existing.get("added_by", "")
        sheet_row = row_index + 2  # header + 1-indexed
        row = [user_id, final_name, ",".join(clean), final_added_by, now]
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{TEAM_TAB}!A{sheet_row}:{_col_letter(len(TEAM_COLUMNS) - 1)}{sheet_row}",
            valueInputOption="RAW",
            body={"values": [row]},
        ).execute()
        name, added_by = final_name, final_added_by

    return {"user_id": user_id, "name": name, "roles": ",".join(clean), "added_by": added_by, "added_at": now}


def add_user_role(user_id: str, role: str, name: str = "", added_by: str = "") -> dict:
    """Grant one role to a user (preserves existing roles)."""
    roles = get_user_roles(user_id)
    roles.add(role.strip().lower())
    return set_user_roles(user_id, roles, name=name, added_by=added_by)


def remove_user_role(user_id: str, role: str, added_by: str = "") -> dict:
    """Revoke one role from a user (preserves the rest)."""
    roles = get_user_roles(user_id)
    roles.discard(role.strip().lower())
    return set_user_roles(user_id, roles, added_by=added_by)


def _delete_team_row(sheets_svc, sheet_id: str, row_index: int) -> None:
    """Delete a data row (0-indexed among data rows) from the Team tab."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    team_tab_id = next(
        s["properties"]["sheetId"] for s in meta["sheets"]
        if s["properties"]["title"] == TEAM_TAB
    )
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": team_tab_id,
                    "dimension": "ROWS",
                    "startIndex": row_index + 1,  # +1 for header
                    "endIndex": row_index + 2,
                }
            }
        }]},
    ).execute()


# ---------------------------------------------------------------------------
# Fuzzy artist lookup
# ---------------------------------------------------------------------------

def find_artist(text: str, threshold: int = 75) -> dict:
    """Match a free-text reference to artists in the sheet.

    Returns {"matches": [artist_dict, ...], "confidence": "exact|fuzzy|none"}.
    Exact = artist_id or name matches case-insensitively.
    Fuzzy = rapidfuzz score >= threshold on either field.
    """
    from rapidfuzz import fuzz, process

    text_clean = text.strip().lstrip("@").lower()
    if not text_clean:
        return {"matches": [], "confidence": "none"}

    artists = read_artists()
    if not artists:
        return {"matches": [], "confidence": "none"}

    # Exact match on artist_id or name (case-insensitive)
    exact = [
        a for a in artists
        if a["artist_id"].lower() == text_clean or a["name"].lower() == text_clean
    ]
    if exact:
        return {"matches": exact, "confidence": "exact"}

    # Fuzzy match — score against the better of (name, artist_id)
    candidates = []
    for a in artists:
        score = max(
            fuzz.WRatio(text_clean, a["name"].lower()),
            fuzz.WRatio(text_clean, a["artist_id"].lower()),
        )
        if score >= threshold:
            candidates.append((score, a))
    candidates.sort(key=lambda x: -x[0])
    if candidates:
        return {"matches": [a for _, a in candidates[:5]], "confidence": "fuzzy"}

    return {"matches": [], "confidence": "none"}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_artist(fields: dict[str, Any]) -> dict:
    """Append a new artist row. Required: artist_id. Returns the new row dict."""
    if not fields.get("artist_id"):
        raise ValueError("artist_id is required (use Instagram handle without @)")
    artist_id = fields["artist_id"].lstrip("@").strip()
    fields["artist_id"] = artist_id

    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    # Reject if artist already exists
    existing = _read_tab(sheets_svc, sheet_id, ARTISTS_TAB, ARTIST_COLUMNS)
    if any(a["artist_id"] == artist_id for a in existing):
        raise ValueError(f"Artist '{artist_id}' already exists")

    now = _now_iso()
    fields.setdefault("created_at", now)
    fields["last_updated_at"] = now

    # Build the row in column order; ignore unknown fields silently
    row = [str(fields.get(col, "")) for col in ARTIST_COLUMNS]

    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{ARTISTS_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

    return {ARTIST_COLUMNS[i]: row[i] for i in range(len(ARTIST_COLUMNS))}


def update_artist_field(artist_id: str, field: str, value: str) -> dict:
    """Update a single cell on an artist row. Refreshes last_updated_at."""
    if field not in ARTIST_COLUMNS:
        raise ValueError(f"Unknown field '{field}'. Allowed: {ARTIST_COLUMNS}")
    if field in ("artist_id", "created_at"):
        raise ValueError(f"Field '{field}' is immutable")

    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()
    artist_id = artist_id.lstrip("@").strip()

    rows = _read_tab(sheets_svc, sheet_id, ARTISTS_TAB, ARTIST_COLUMNS)
    row_index = None
    for i, a in enumerate(rows):
        if a["artist_id"] == artist_id:
            row_index = i
            break
    if row_index is None:
        raise ValueError(f"Artist '{artist_id}' not found")

    # Row in the sheet = header (row 1) + data offset (1-indexed)
    sheet_row = row_index + 2
    field_col = _col_letter(ARTIST_COLUMNS.index(field))
    updated_col = _col_letter(ARTIST_COLUMNS.index("last_updated_at"))
    now = _now_iso()

    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{ARTISTS_TAB}!{field_col}{sheet_row}", "values": [[str(value)]]},
                {"range": f"{ARTISTS_TAB}!{updated_col}{sheet_row}", "values": [[now]]},
            ],
        },
    ).execute()

    updated = dict(rows[row_index])
    updated[field] = str(value)
    updated["last_updated_at"] = now
    return updated


def set_artist_file_url(artist_id: str, field: str, drive_url: str) -> dict:
    """Convenience wrapper for the two URL columns."""
    if field not in FILE_URL_FIELDS:
        raise ValueError(f"set_artist_file_url only handles {FILE_URL_FIELDS}")
    return update_artist_field(artist_id, field, drive_url)


def append_conversation(
    artist_id: str,
    author_slack_id: str,
    channel: str,
    summary: str,
    created_by: str = "",
    date_iso: str | None = None,
) -> dict:
    """Append a conversation row. Naturally race-safe under concurrent writes."""
    artist_id = artist_id.lstrip("@").strip()
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()
    date_iso = date_iso or _now_iso()

    row = [artist_id, date_iso, author_slack_id, created_by, channel, summary]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{CONVERSATIONS_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {CONVERSATION_COLUMNS[i]: row[i] for i in range(len(CONVERSATION_COLUMNS))}


def append_marketing(
    artist_id: str,
    author_slack_id: str,
    channel: str,
    feature_type: str,
    placement: str,
    summary: str,
    created_by: str = "",
    date_iso: str | None = None,
) -> dict:
    """Append a marketing-feature row. Naturally race-safe under concurrent writes.

    `feature_type` is coerced to "other" if it isn't in MARKETING_FEATURE_TYPES, so
    aggregate counts always group on a known, finite set of categories.
    """
    artist_id = artist_id.lstrip("@").strip()
    feature_type = (feature_type or "").strip().lower()
    if feature_type not in MARKETING_FEATURE_TYPES:
        feature_type = "other"
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()
    date_iso = date_iso or _now_iso()

    row = [artist_id, date_iso, author_slack_id, created_by, channel, feature_type, placement, summary]
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{MARKETING_TAB}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    return {MARKETING_COLUMNS[i]: row[i] for i in range(len(MARKETING_COLUMNS))}


def migrate_artists_schema() -> None:
    """Re-align the Artists tab to the current ARTIST_COLUMNS, preserving data.

    Applies known column renames (ARTIST_COLUMN_RENAMES), keeps every existing
    value by matching on column name, and leaves any newly-added column blank.
    Idempotent — skips if the header already matches ARTIST_COLUMNS exactly.
    Re-run this after adding or renaming a column in ARTIST_COLUMNS.
    """
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=ARTISTS_TAB,
    ).execute()
    rows = result.get("values", [])

    if not rows:
        # Empty tab — just write the new header and exit.
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{ARTISTS_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [ARTIST_COLUMNS]},
        ).execute()
        print("Artists tab was empty — wrote new header. Done.")
        return

    old_headers = [h.strip() for h in rows[0]]
    if old_headers == ARTIST_COLUMNS:
        print("Artists header already matches the current schema — nothing to do.")
        return

    # Build per-row dicts keyed by OLD header names, applying renames so old
    # names point at new keys. Then construct each new row by reading from
    # the dict in NEW column order (missing keys → blank).
    new_rows = [ARTIST_COLUMNS]
    for r in rows[1:]:
        padded = r + [""] * (len(old_headers) - len(r))
        row_dict: dict[str, str] = {}
        for i, header in enumerate(old_headers):
            key = ARTIST_COLUMN_RENAMES.get(header, header)
            row_dict[key] = padded[i] if i < len(padded) else ""
        new_row = [row_dict.get(col, "") for col in ARTIST_COLUMNS]
        new_rows.append(new_row)

    sheets_svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=ARTISTS_TAB,
    ).execute()
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{ARTISTS_TAB}!A1",
        valueInputOption="RAW",
        body={"values": new_rows},
    ).execute()

    print(
        f"Re-aligned Artists tab to current schema. "
        f"{len(new_rows) - 1} row(s) preserved; "
        f"{len(ARTIST_COLUMNS) - len(old_headers)} new column(s) added."
    )


def migrate_conversations_add_created_by() -> None:
    """One-time migration: inserts the `created_by` column into the Conversations
    tab and backfills existing rows by resolving each row's `author_slack_id` to
    a Slack display name. Idempotent — skips if the column is already present.
    """
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=CONVERSATIONS_TAB,
    ).execute()
    rows = result.get("values", [])

    if not rows:
        # Empty tab — write the new header and exit.
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{CONVERSATIONS_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [CONVERSATION_COLUMNS]},
        ).execute()
        print("Conversations tab was empty — wrote new header. Done.")
        return

    headers = [h.strip().lower() for h in rows[0]]
    is_legacy = "created_by" not in headers

    # Index helpers — old schema indexes are positional, new schema by name.
    if is_legacy:
        # Legacy: artist_id, date_iso, author_slack_id, channel, summary
        author_idx, created_by_idx = 2, None
    else:
        author_idx = headers.index("author_slack_id")
        created_by_idx = headers.index("created_by")

    # Figure out which author_slack_ids need resolution
    need_resolution: set[str] = set()
    for r in rows[1:]:
        author_id = r[author_idx] if len(r) > author_idx else ""
        if not author_id:
            continue
        if is_legacy:
            need_resolution.add(author_id)
        else:
            current_name = r[created_by_idx] if len(r) > created_by_idx else ""
            if not current_name:
                need_resolution.add(author_id)

    if not is_legacy and not need_resolution:
        print("Already migrated and fully backfilled — nothing to do.")
        return

    # Resolve U-IDs to display names via Slack (best-effort).
    name_lookup: dict[str, str] = {}
    if need_resolution:
        bot_token = os.getenv("SLACK_BOT_TOKEN")
        if bot_token:
            try:
                from slack_sdk import WebClient
                slack = WebClient(token=bot_token)
                for uid in need_resolution:
                    try:
                        info = slack.users_info(user=uid)
                        if info.get("ok"):
                            u = info.get("user", {}) or {}
                            profile = u.get("profile", {}) or {}
                            name_lookup[uid] = (
                                profile.get("display_name")
                                or profile.get("real_name")
                                or u.get("real_name")
                                or u.get("name")
                                or uid
                            )
                    except Exception as e:
                        print(f"  Could not resolve {uid}: {e}")
            except ImportError:
                print("  slack_sdk not installed — backfill will be blank for existing rows")
        else:
            print("  SLACK_BOT_TOKEN not set — backfill will be blank for existing rows")

    # Rewrite the entire tab with the new 6-column schema.
    new_rows = [CONVERSATION_COLUMNS]
    for r in rows[1:]:
        if is_legacy:
            padded = r + [""] * (5 - len(r))
            artist_id, date_iso, author_id, channel, summary = padded[:5]
            created_by = name_lookup.get(author_id, "") if author_id else ""
        else:
            padded = r + [""] * (6 - len(r))
            artist_id, date_iso, author_id, created_by, channel, summary = padded[:6]
            if author_id and not created_by:
                created_by = name_lookup.get(author_id, "")
        new_rows.append([artist_id, date_iso, author_id, created_by, channel, summary])

    sheets_svc.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=CONVERSATIONS_TAB,
    ).execute()
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{CONVERSATIONS_TAB}!A1",
        valueInputOption="RAW",
        body={"values": new_rows},
    ).execute()

    action = "migrated + backfilled" if is_legacy else "backfilled"
    print(
        f"{action.capitalize()} {len(new_rows) - 1} conversation row(s). "
        f"Resolved {len(name_lookup)} unique author(s) from Slack."
    )


def add_marketing_tab() -> None:
    """Add the `Marketing` tab to an existing CRM workbook. Idempotent.

    Run once against the live sheet (which already has Artists + Conversations) to
    create the third tab with a frozen, bolded header. Safe to re-run — skips if the
    tab already exists.
    """
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if MARKETING_TAB in existing:
        print(f"'{MARKETING_TAB}' tab already exists — nothing to do.")
        return

    # Create the tab with a frozen header row.
    add_resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "addSheet": {
                "properties": {
                    "title": MARKETING_TAB,
                    "gridProperties": {"frozenRowCount": 1},
                }
            }
        }]},
    ).execute()
    new_tab_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    # Write the header row.
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{MARKETING_TAB}!A1",
        valueInputOption="RAW",
        body={"values": [MARKETING_COLUMNS]},
    ).execute()

    # Bold the header.
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": new_tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }]},
    ).execute()

    print(f"Created '{MARKETING_TAB}' tab with header: {', '.join(MARKETING_COLUMNS)}")


def _add_tab(tab_title: str, columns: list[str]) -> None:
    """Add a tab with a frozen, bolded header to the existing CRM workbook. Idempotent."""
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"] for s in meta["sheets"]}
    if tab_title in existing:
        print(f"'{tab_title}' tab already exists — nothing to do.")
        return

    add_resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "addSheet": {"properties": {"title": tab_title, "gridProperties": {"frozenRowCount": 1}}}
        }]},
    ).execute()
    new_tab_id = add_resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    sheets_svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab_title}!A1",
        valueInputOption="RAW",
        body={"values": [columns]},
    ).execute()

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "repeatCell": {
                "range": {"sheetId": new_tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }]},
    ).execute()

    print(f"Created '{tab_title}' tab with header: {', '.join(columns)}")


def add_team_tab() -> None:
    """Add the `Team` tab to an existing CRM workbook. Idempotent."""
    _add_tab(TEAM_TAB, TEAM_COLUMNS)


def delete_conversation(artist_id: str, date_iso: str) -> bool:
    """Delete a single conversation row by (artist_id, date_iso) pair.

    Used by the agent when pruning stale / redundant entries.
    """
    sheets_svc, _ = _get_clients()
    sheet_id = _get_sheet_id()

    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=CONVERSATIONS_TAB,
    ).execute()
    rows = result.get("values", [])
    target_row = None
    for i in range(1, len(rows)):
        if len(rows[i]) >= 2 and rows[i][0] == artist_id and rows[i][1] == date_iso:
            target_row = i  # 0-indexed including header
            break
    if target_row is None:
        return False

    # Find the sheetId for the Conversations tab
    meta = sheets_svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    conv_tab_id = next(
        s["properties"]["sheetId"] for s in meta["sheets"]
        if s["properties"]["title"] == CONVERSATIONS_TAB
    )

    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": conv_tab_id,
                    "dimension": "ROWS",
                    "startIndex": target_row,
                    "endIndex": target_row + 1,
                }
            }
        }]},
    ).execute()
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """End-to-end smoke test against a throwaway sheet.

    Does NOT touch ARTIST_CRM_SHEET_URL in .env. Creates its own ephemeral sheet,
    runs every public function, then deletes the sheet.
    """
    import tempfile
    _load_env()

    print("== Self-test: provisioning a throwaway CRM sheet ==")
    sheets_svc, drive_svc = _get_clients()
    title = f"Umbra CRM Self-Test — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    spreadsheet = sheets_svc.spreadsheets().create(
        body={
            "properties": {"title": title},
            "sheets": [
                {"properties": {"title": ARTISTS_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": CONVERSATIONS_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": MARKETING_TAB, "gridProperties": {"frozenRowCount": 1}}},
                {"properties": {"title": TEAM_TAB, "gridProperties": {"frozenRowCount": 1}}},
            ],
        }
    ).execute()
    sheet_id = spreadsheet["spreadsheetId"]
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{ARTISTS_TAB}!A1", "values": [ARTIST_COLUMNS]},
                {"range": f"{CONVERSATIONS_TAB}!A1", "values": [CONVERSATION_COLUMNS]},
                {"range": f"{MARKETING_TAB}!A1", "values": [MARKETING_COLUMNS]},
                {"range": f"{TEAM_TAB}!A1", "values": [TEAM_COLUMNS]},
            ],
        },
    ).execute()

    # Temporarily redirect the env var for the duration of the test
    saved_url = os.environ.get("ARTIST_CRM_SHEET_URL")
    os.environ["ARTIST_CRM_SHEET_URL"] = f"https://docs.google.com/spreadsheets/d/{sheet_id}"

    try:
        print("[1] add_artist(samharper)")
        a = add_artist({
            "artist_id": "samharper",
            "name": "Sam Harper",
            "email": "sam@example.com",
            "instagram_url": "https://instagram.com/samharper",
            "instagram_followers": "12500",
            "onboarded_by": "alice",
            "tier": "Tier 2",
        })
        assert a["artist_id"] == "samharper", a

        print("[2] add_artist(sammyholden) — for fuzzy ambiguity")
        add_artist({
            "artist_id": "sammyholden",
            "name": "Sammy Holden",
            "email": "sammy@example.com",
        })

        print("[3] read_artists()")
        artists = read_artists()
        assert len(artists) == 2, artists

        print("[4] find_artist('samharper') — exact")
        r = find_artist("samharper")
        assert r["confidence"] == "exact" and len(r["matches"]) == 1, r

        print("[5] find_artist('sam') — fuzzy, expect both matches")
        r = find_artist("sam")
        assert r["confidence"] == "fuzzy" and len(r["matches"]) >= 2, r

        print("[6] append_conversation x3")
        append_conversation("samharper", "U001", "C_general", "Onboarded — promised to help promote new track")
        time.sleep(1.0)
        append_conversation("samharper", "U002", "C_marketing", "Pushed the new track on our IG story + TikTok")
        time.sleep(1.0)
        append_conversation("samharper", "U001", "C_general", "Sam thanked us for the promo, asked about cover art help")

        print("[7] read_conversations('samharper')")
        convs = read_conversations("samharper")
        assert len(convs) == 3, convs
        # Chronological order
        assert convs[0]["date_iso"] < convs[-1]["date_iso"], convs

        print("[8] update_artist_field(samharper, tier, 'Tier 1')")
        u = update_artist_field("samharper", "tier", "Tier 1")
        assert u["tier"] == "Tier 1", u
        assert u["last_updated_at"] != a["last_updated_at"]

        print("[9] set_artist_file_url(samharper, press_pics_drive_url, ...)")
        set_artist_file_url("samharper", "press_pics_drive_url", "https://drive.google.com/uc?id=fake")
        artists = read_artists()
        sam = next(a for a in artists if a["artist_id"] == "samharper")
        assert sam["press_pics_drive_url"].startswith("https://drive.google.com"), sam

        print("[10] delete_conversation(samharper, oldest)")
        oldest = convs[0]
        ok = delete_conversation("samharper", oldest["date_iso"])
        assert ok, "delete_conversation should succeed"
        remaining = read_conversations("samharper")
        assert len(remaining) == 2

        print("[11] append_marketing x3 + read_marketing")
        append_marketing("samharper", "U001", "C_marketing", "paid_campaign", "Spring IG ads", "Featured in spring paid campaign")
        time.sleep(1.0)
        append_marketing("samharper", "U002", "C_marketing", "daily_press", "Daily Rush", "Featured on the Daily Rush")
        time.sleep(1.0)
        # Unknown type should coerce to "other"
        m = append_marketing("samharper", "U001", "C_marketing", "billboard", "Times Square", "Billboard placement")
        assert m["feature_type"] == "other", m
        mkt = read_marketing("samharper")
        assert len(mkt) == 3, mkt
        assert mkt[0]["date_iso"] < mkt[-1]["date_iso"], mkt
        assert {r["feature_type"] for r in mkt} == {"paid_campaign", "daily_press", "other"}, mkt

        print("[12] team roles: set / get / add / remove")
        set_user_roles("U_AGENT1", {"marketing"}, name="Marky", added_by="admin1")
        assert get_user_roles("U_AGENT1") == {"marketing"}, get_user_roles("U_AGENT1")
        add_user_role("U_AGENT1", "rise")
        assert get_user_roles("U_AGENT1") == {"marketing", "rise"}, get_user_roles("U_AGENT1")
        remove_user_role("U_AGENT1", "marketing")
        assert get_user_roles("U_AGENT1") == {"rise"}, get_user_roles("U_AGENT1")
        # Invalid roles dropped; unknown user has no roles.
        set_user_roles("U_AGENT2", {"admin", "bogus"}, name="Addie", added_by="admin1")
        assert get_user_roles("U_AGENT2") == {"admin"}, get_user_roles("U_AGENT2")
        assert get_user_roles("U_NOBODY") == set()
        # Removing the last role deletes the row.
        remove_user_role("U_AGENT1", "rise")
        assert get_user_roles("U_AGENT1") == set()
        assert all(r["user_id"] != "U_AGENT1" for r in read_team())

        print("[13] add_artist(samharper) again — should reject")
        try:
            add_artist({"artist_id": "samharper", "name": "dupe"})
            assert False, "expected duplicate rejection"
        except ValueError:
            pass

        print("\n[OK] All self-test assertions passed.")
    finally:
        # Cleanup: delete the throwaway sheet
        try:
            drive_svc.files().delete(fileId=sheet_id).execute()
            print(f"\nCleaned up throwaway sheet {sheet_id}")
        except HttpError as e:
            print(f"\nWARNING: could not delete throwaway sheet {sheet_id}: {e}")
        if saved_url is None:
            os.environ.pop("ARTIST_CRM_SHEET_URL", None)
        else:
            os.environ["ARTIST_CRM_SHEET_URL"] = saved_url


def main():
    ap = argparse.ArgumentParser(description="Umbra Artist CRM")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--provision", action="store_true",
                   help="Create the CRM sheet (idempotent) and persist URL to server/.env")
    g.add_argument("--self-test", action="store_true",
                   help="End-to-end smoke test against a throwaway sheet")
    g.add_argument("--find", metavar="TEXT", help="Fuzzy-search artists")
    g.add_argument("--list", action="store_true", help="List all artists")
    g.add_argument("--summary", metavar="ARTIST_ID",
                   help="Print artist row + all conversations chronologically")
    g.add_argument("--migrate-conversations", action="store_true",
                   help="One-time: add `created_by` column to Conversations tab + backfill from Slack")
    g.add_argument("--migrate-artists", action="store_true",
                   help="Re-align the Artists tab to the current ARTIST_COLUMNS (preserves data; run after adding/renaming a column)")
    g.add_argument("--migrate-add-marketing", action="store_true",
                   help="One-time: add the `Marketing` tab to the existing CRM sheet (idempotent)")
    g.add_argument("--migrate-add-team", action="store_true",
                   help="One-time: add the `Team` tab (role assignments) to the existing CRM sheet (idempotent)")

    ap.add_argument("--emails", default=None,
                    help="Comma-separated team emails to share the new sheet with "
                         "(defaults to ARTIST_CRM_TEAM_EMAILS in .env)")
    args = ap.parse_args()

    if args.provision:
        emails = [e.strip() for e in args.emails.split(",")] if args.emails else None
        provision_sheet(emails)
    elif args.self_test:
        _self_test()
    elif args.migrate_conversations:
        migrate_conversations_add_created_by()
    elif args.migrate_artists:
        migrate_artists_schema()
    elif args.migrate_add_marketing:
        add_marketing_tab()
    elif args.migrate_add_team:
        add_team_tab()
    elif args.find:
        r = find_artist(args.find)
        print(json.dumps(r, indent=2, default=str))
    elif args.list:
        for a in read_artists():
            print(f"  {a['artist_id']:20s}  {a['name']:30s}  {a['tier']}")
    elif args.summary:
        artists = read_artists()
        match = next((a for a in artists if a["artist_id"] == args.summary), None)
        if not match:
            print(f"Artist '{args.summary}' not found", file=sys.stderr)
            sys.exit(1)
        print("== Artist ==")
        for k, v in match.items():
            print(f"  {k}: {v}")
        print("\n== Conversations ==")
        for c in read_conversations(args.summary):
            print(f"  [{c['date_iso']}] {c['author_slack_id']} in #{c['channel']}: {c['summary']}")
        print("\n== Marketing ==")
        for m in read_marketing(args.summary):
            placement = f" — {m['placement']}" if m.get("placement") else ""
            print(f"  [{m['date_iso']}] {m['feature_type']}{placement}: {m['summary']}")


if __name__ == "__main__":
    main()
