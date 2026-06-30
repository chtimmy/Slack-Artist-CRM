#!/usr/bin/env python3
"""
Umbra — Slack Broadcast (one-off DM announcement)

Sends a direct message from the Artist Repository bot to every human member of the
workspace. Useful for announcing access (the bot is invisible to a user until it has
DMed them at least once) or onboarding new members later.

Safe by default: runs a DRY RUN unless --send is passed.

    python3 tools/slack_broadcast.py                 # dry run — list eligible recipients
    python3 tools/slack_broadcast.py --send          # actually DM everyone
    python3 tools/slack_broadcast.py --send --exclude U123,U456
    python3 tools/slack_broadcast.py --message-file note.txt --send

Skips: bots / apps, deactivated accounts, Slackbot, and the bot itself.
Requires the bot token to have users:read, chat:write, im:write scopes.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
ENV_PATH = os.path.join(PROJECT_ROOT, "server", ".env")
if os.path.exists(ENV_PATH):
    load_dotenv(ENV_PATH, override=False)

DEFAULT_MESSAGE = (
    "👋 Hey! You now have access to the *Artist Repository* agent here on Slack.\n\n"
    "Just message me in plain English — a few things you can try:\n"
    "• `where are we at with <artist>?`\n"
    "• `what's my role?` / `what can I do?`\n"
    "• log an update, e.g. `logged a call with <artist> about cover art`\n\n"
    "What you can change depends on your team role (marketing / rise / onboarding). "
    "Ask an admin if you need a role — and `what can I do?` will always tell you your current access. 🎶"
)


def _eligible_members(client, bot_user_id: str) -> list[dict]:
    """Return human, active members (excludes bots/apps, deleted, Slackbot, the bot itself)."""
    members = []
    cursor = None
    while True:
        resp = client.users_list(limit=200, cursor=cursor)
        for u in resp.get("members", []):
            if u.get("is_bot") or u.get("deleted"):
                continue
            if u.get("id") in (bot_user_id, "USLACKBOT"):
                continue
            if u.get("is_app_user"):
                continue
            members.append(u)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return members


def _display(u: dict) -> str:
    p = u.get("profile", {}) or {}
    return p.get("display_name") or p.get("real_name") or u.get("real_name") or u.get("name") or u.get("id")


def main() -> None:
    ap = argparse.ArgumentParser(description="Broadcast a DM to all workspace members")
    ap.add_argument("--send", action="store_true", help="Actually send (default is a dry run)")
    ap.add_argument("--message-file", help="Path to a file with the message body (defaults to built-in)")
    ap.add_argument("--exclude", default="", help="Comma-separated user IDs to skip")
    ap.add_argument("--only", default="", help="Comma-separated user IDs to send to (restrict to exactly these)")
    args = ap.parse_args()

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set in server/.env", file=sys.stderr)
        sys.exit(1)

    message = DEFAULT_MESSAGE
    if args.message_file:
        with open(args.message_file, "r", encoding="utf-8") as f:
            message = f.read().strip()

    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    client = WebClient(token=token)
    bot_user_id = client.auth_test().get("user_id")
    exclude = {e.strip() for e in args.exclude.split(",") if e.strip()}
    only = {o.strip() for o in args.only.split(",") if o.strip()}

    members = [m for m in _eligible_members(client, bot_user_id) if m.get("id") not in exclude]
    if only:
        members = [m for m in members if m.get("id") in only]
        missing = only - {m.get("id") for m in members}
        if missing:
            print(f"WARNING: {len(missing)} requested ID(s) not found / not eligible: {', '.join(sorted(missing))}\n")

    print(f"Eligible recipients: {len(members)}")
    for m in members:
        print(f"  {m.get('id'):12s}  {_display(m)}")

    print("\n--- message preview ---")
    print(message)
    print("-----------------------")

    if not args.send:
        print(f"\nDRY RUN — no messages sent. Re-run with --send to DM these {len(members)} member(s).")
        return

    print(f"\nSending to {len(members)} member(s)...")
    sent, failed = 0, []
    for m in members:
        uid = m.get("id")
        try:
            opened = client.conversations_open(users=uid)
            channel = opened["channel"]["id"]
            client.chat_postMessage(channel=channel, text=message)
            sent += 1
            time.sleep(1.0)  # stay well under Slack rate limits
        except SlackApiError as e:
            failed.append(f"{_display(m)} ({uid}): {e.response.get('error')}")

    print(f"\nDone. Sent {sent}; failed {len(failed)}.")
    for f in failed:
        print(f"  FAILED {f}")


if __name__ == "__main__":
    main()
