#!/usr/bin/env python3
"""
Artist CRM — shared helpers.

Small, self-contained utilities used across the CRM tools:
  - _load_env():       load server/.env relative to the repo root
  - get_credentials(): Google OAuth (Sheets + Drive), with token caching/refresh
  - extract_file_id(): pull a Drive/Docs/Sheets file or folder ID out of a URL

SCOPES is a module-level global on purpose: get_credentials() reads it at call
time, so a caller that needs different scopes can temporarily swap it (see
artist_crm._get_clients), then restore it.
"""

import os
import re

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Full Drive access — required to work inside a user-created folder in a Shared
# Drive (the narrow drive.file scope can't see folders the app didn't create,
# nor enumerate Shared Drives) — plus spreadsheets read/write.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _load_env():
    """Load environment variables from <repo-root>/server/.env."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    env_path = os.path.join(project_root, "server", ".env")
    load_dotenv(env_path)


def get_credentials(token_path: str, client_id: str, client_secret: str) -> Credentials:
    """Return valid Google OAuth creds, refreshing or running the flow as needed.

    Uses the module-level SCOPES at call time. Tokens are cached to token_path
    and refreshed transparently; if the refresh token has been revoked or aged
    out (Google testing-mode tokens die after 7 days) a fresh browser flow runs.
    """
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                # Token revoked or expired beyond refresh — fall through to a
                # fresh OAuth flow.
                creds = None

        if not refreshed and (not creds or not creds.valid):
            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"],
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def extract_file_id(id_or_url: str) -> str:
    """Accept a raw file ID or any Google Drive/Docs/Sheets/Slides URL."""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", id_or_url)
    if match:
        return match.group(1)
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", id_or_url)
    if match:
        return match.group(1)
    # Assume it's already a bare file ID
    return id_or_url.strip()
