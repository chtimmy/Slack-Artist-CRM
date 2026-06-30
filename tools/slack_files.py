#!/usr/bin/env python3
"""
Umbra — Slack File Ingest

Downloads a file shared in Slack and uploads it to Google Drive, returning a
publicly accessible URL ready to drop into the Artist CRM sheet.

Mirrors the Drive-upload pattern from tools/research_organizer.py:510-525.

Usage as a module:
    from slack_files import ingest_slack_file
    result = ingest_slack_file(file_obj, bot_token, drive_svc, folder_id="...")
    # result = {"drive_url": "...", "mime": "image/png", "name": "sam_press.png"}
"""

from __future__ import annotations

import io
import os
import sys
from typing import Any

import requests
from googleapiclient.http import MediaIoBaseUpload

FOLDER_MIME = "application/vnd.google-apps.folder"
ASSETS_FOLDER_NAME = "Artist Assets"

# Maps a sheet field to the human-readable category subfolder name.
CATEGORY_LABELS = {
    "press_pics_drive_url": "Press Shots",
    "marketing_material_drive_url": "Marketing Material",
    "artist_info_drive_url": "Artist Info",
    "rise_material_drive_url": "Rise Material",
}


# ---------------------------------------------------------------------------
# Drive folder management (Shared Drive aware)
# ---------------------------------------------------------------------------
# All calls pass supportsAllDrives / includeItemsFromAllDrives so they work in a
# Shared Drive as well as My Drive. Layout:
#   Artist Repository/ (holds the sheet) → Artist Assets/ → <name> [id]/
#     → {Press Shots, Marketing Material, Artist Info, Rise Material}/
# Folders inherit Shared Drive membership for access — no per-folder public share.

def _escape_q(value: str) -> str:
    """Escape a value for use inside a Drive `q` string literal (handles apostrophes)."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_folder(drive_svc, name: str, parent_id: str | None) -> str | None:
    """Return the id of a non-trashed folder with this exact name under parent_id, or None."""
    q = (
        f"name = '{_escape_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    )
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = drive_svc.files().list(
        q=q, spaces="drive", fields="files(id, name)", pageSize=10,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(drive_svc, name: str, parent_id: str | None = None) -> str:
    """Create a Drive folder (Shared Drive aware) and return its id."""
    meta: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive_svc.files().create(
        body=meta, fields="id", supportsAllDrives=True,
    ).execute()
    return folder["id"]


def get_or_create_folder(drive_svc, name: str, parent_id: str | None = None) -> str:
    """Find a folder by name under parent_id, creating it if absent. Returns the id."""
    return _find_folder(drive_svc, name, parent_id) or _create_folder(drive_svc, name, parent_id)


def get_or_create_assets_folder(drive_svc) -> str:
    """Return the 'Artist Assets' folder id, found-or-created inside the folder that
    holds the CRM sheet (the 'Artist Repository' folder). Persisted to .env on first use.
    """
    assets_id = os.getenv("ARTIST_CRM_ASSETS_FOLDER_ID", "").strip()
    if assets_id:
        return assets_id

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from artist_crm import _get_sheet_id, _set_env_var  # noqa: E402

    sheet_id = _get_sheet_id()
    meta = drive_svc.files().get(
        fileId=sheet_id, fields="parents", supportsAllDrives=True,
    ).execute()
    parents = meta.get("parents")
    if not parents:
        raise RuntimeError(
            "Could not find the sheet's parent folder. Make sure the sheet lives "
            "inside the 'Artist Repository' folder in the shared drive."
        )
    repo_folder_id = parents[0]
    assets_id = get_or_create_folder(drive_svc, ASSETS_FOLDER_NAME, repo_folder_id)
    _set_env_var("ARTIST_CRM_ASSETS_FOLDER_ID", assets_id)
    os.environ["ARTIST_CRM_ASSETS_FOLDER_ID"] = assets_id
    return assets_id


def ensure_category_folder(drive_svc, artist: dict, target_field: str) -> dict[str, str]:
    """Ensure Artist Assets → <artist> → category subfolder exist. Returns {folder_id, folder_url}."""
    label = CATEGORY_LABELS.get(target_field)
    if not label:
        raise ValueError(f"No category folder for field '{target_field}'")

    assets_id = get_or_create_assets_folder(drive_svc)
    artist_folder_name = f"{artist.get('name') or artist['artist_id']} [{artist['artist_id']}]"
    artist_folder_id = get_or_create_folder(drive_svc, artist_folder_name, assets_id)
    category_id = get_or_create_folder(drive_svc, label, artist_folder_id)
    return {
        "folder_id": category_id,
        "folder_url": f"https://drive.google.com/drive/folders/{category_id}",
    }


def ingest_slack_file(
    file_obj: dict[str, Any],
    bot_token: str,
    drive_svc,
    folder_id: str | None = None,
) -> dict[str, str]:
    """Download a Slack file and upload it to Drive, returning a public URL.

    file_obj: the Slack API "file" object (must include url_private_download, mimetype, name).
    bot_token: SLACK_BOT_TOKEN (xoxb-...) — required for url_private_download auth.
    drive_svc: an authenticated Drive v3 service.
    folder_id: optional Drive folder ID to place the file in (defaults to root).
    """
    download_url = file_obj.get("url_private_download") or file_obj.get("url_private")
    if not download_url:
        raise ValueError("file_obj has no url_private_download / url_private")

    name = file_obj.get("name") or "slack_upload"
    mime = file_obj.get("mimetype") or "application/octet-stream"

    # 1) Download bytes from Slack (bearer auth required)
    resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=60,
        allow_redirects=True,
    )
    resp.raise_for_status()
    if not resp.content:
        raise RuntimeError(f"Empty response downloading {name} from Slack")

    # 2) Upload to Drive (in-memory, no .tmp/ write)
    file_meta: dict[str, Any] = {"name": name}
    if folder_id:
        file_meta["parents"] = [folder_id]
    media = MediaIoBaseUpload(io.BytesIO(resp.content), mimetype=mime, resumable=False)
    created = drive_svc.files().create(
        body=file_meta, media_body=media, fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    file_id = created["id"]

    # Access comes from Shared Drive membership — no per-file public share needed.
    web_view = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    return {
        "drive_url": web_view,
        "uc_url": f"https://drive.google.com/uc?id={file_id}",
        "file_id": file_id,
        "mime": mime,
        "name": name,
    }


# ---------------------------------------------------------------------------
# CLI (smoke test — needs a Slack file URL + bot token + Drive auth)
# ---------------------------------------------------------------------------

def _cli() -> None:
    import argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from artist_crm import _get_clients  # noqa: E402

    ap = argparse.ArgumentParser(description="Test Slack file ingest (download then re-upload to Drive)")
    ap.add_argument("--slack-url", required=True, help="A file's url_private_download from a Slack event payload")
    ap.add_argument("--name", default="test_upload", help="Filename")
    ap.add_argument("--mime", default="image/png", help="Mime type")
    args = ap.parse_args()

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        print("SLACK_BOT_TOKEN not set in env", file=sys.stderr)
        sys.exit(1)

    _, drive_svc = _get_clients()
    folder_id = os.getenv("ARTIST_CRM_DRIVE_FOLDER_ID") or None
    file_obj = {
        "url_private_download": args.slack_url,
        "name": args.name,
        "mimetype": args.mime,
    }
    result = ingest_slack_file(file_obj, bot_token, drive_svc, folder_id=folder_id)
    print(result)


if __name__ == "__main__":
    _cli()
