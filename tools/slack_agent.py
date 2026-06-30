#!/usr/bin/env python3
"""
Umbra — Slack Artist CRM Agent

Long-running Slack listener that acts as the team's middle-man for everything
per-artist. Reads from and writes to a Google Sheet (Artists + Conversations tabs)
and uploads files to Google Drive.

Interactions:
  - @mention in any channel the bot is invited to
  - DM to the bot
  - File attachment (with caption) → uploaded to Drive, URL stored on the artist row

Architecture:
  Slack event → auth check → slack_intent.classify(text) → handler dispatcher
                                                            ↓
                                                       artist_crm.* (Sheets)
                                                       slack_files.* (Drive)

All replies are threaded. Every step is written to the audit log.

Run:
    caffeinate -i python3 tools/slack_agent.py
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

# macOS Python.framework ships without root certs; point SSL at certifi's bundle.
# Must happen before any HTTP libraries load their SSL contexts.
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

import anthropic
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
_env_path = os.path.join(PROJECT_ROOT, "server", ".env")
if os.path.exists(_env_path):
    load_dotenv(_env_path, override=False)

sys.path.insert(0, SCRIPT_DIR)
from audit_log import log_event  # noqa: E402
import artist_crm  # noqa: E402
from slack_intent import classify as classify_intent  # noqa: E402
from slack_files import CATEGORY_LABELS, ensure_category_folder, ingest_slack_file  # noqa: E402

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("slack_agent")

SUMMARY_MODEL = "claude-sonnet-4-6"

# Human-readable labels for marketing feature_type values (artist_crm.MARKETING_FEATURE_TYPES).
FEATURE_TYPE_LABELS = {
    "paid_campaign": "paid campaign",
    "daily_press": "daily press",
    "social": "social",
    "other": "other",
}

# Process-lifetime cache for Slack user_id → display name (resolved via users.info).
_user_name_cache: dict[str, str] = {}


def _resolve_user_name(client, user_id: str) -> str:
    """Resolve a Slack user_id to a human-readable display name (cached)."""
    if not user_id:
        return ""
    if user_id in _user_name_cache:
        return _user_name_cache[user_id]
    if client is None:
        return user_id
    try:
        result = client.users_info(user=user_id)
        if result.get("ok"):
            user = result.get("user", {}) or {}
            profile = user.get("profile", {}) or {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            if name:
                _user_name_cache[user_id] = name
                return name
    except Exception as e:
        logger.warning(f"users.info failed for {user_id}: {e}")
    return user_id


def _to_slack_mrkdwn(text: str) -> str:
    """Convert GitHub-flavored markdown emphasis to Slack mrkdwn (`**bold**` → `*bold*`)."""
    import re
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


# ---------------------------------------------------------------------------
# Conversation memory (in-memory, per channel)
# ---------------------------------------------------------------------------
# Gives the intent classifier short-term context so it can resolve references
# like "that", "the update I gave you", "yes do that", and corrections like
# "no, I meant log it as a conversation". Lost on restart — fine for v1.

_conv_lock = threading.Lock()
_conv_history: dict[str, list[dict]] = {}  # channel -> [{"role", "text"}, ...]
# How many recent turns (user + bot) to feed the classifier as context. Higher = better
# recall of the recent exchange for follow-ups/corrections. Per-process; cleared on restart.
MAX_HISTORY_TURNS = 14


def _remember(channel: str, role: str, text: str) -> None:
    if not text:
        return
    with _conv_lock:
        hist = _conv_history.setdefault(channel, [])
        hist.append({"role": role, "text": text})
        if len(hist) > MAX_HISTORY_TURNS:
            del hist[:-MAX_HISTORY_TURNS]


def _recent_context(channel: str) -> str:
    with _conv_lock:
        hist = list(_conv_history.get(channel, []))
    if not hist:
        return ""
    lines = []
    for h in hist:
        who = "User" if h["role"] == "user" else "Bot"
        lines.append(f"{who}: {h['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Event deduplication
# ---------------------------------------------------------------------------
# Slack redelivers an event if it isn't acked within ~3s. Our handler does a
# Claude classification + a Sheets write before replying, which can cross that
# threshold — so the same message can arrive 2-3 times. We track each message's
# stable ID and process it exactly once. Bounded + thread-safe.

_seen_lock = threading.Lock()
_seen_events: "OrderedDict[str, bool]" = OrderedDict()
SEEN_MAX = 1000


def _already_handled(event_key: str) -> bool:
    """Atomically check-and-mark. Returns True if this event was already seen."""
    if not event_key:
        return False
    with _seen_lock:
        if event_key in _seen_events:
            return True
        _seen_events[event_key] = True
        if len(_seen_events) > SEEN_MAX:
            _seen_events.popitem(last=False)
    return False


def _event_key(event: dict) -> str:
    """Stable identifier for a Slack message event, constant across retries."""
    return event.get("client_msg_id") or f"{event.get('channel', '')}:{event.get('ts', '')}"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _allow_all_users() -> bool:
    """True if the allowlist is the wildcard "*" — anyone in the workspace may use the bot.

    Safe because Socket Mode only delivers events from the workspace the app is installed
    in, and the optional SLACK_WORKSPACE_ID guard pins that workspace at startup.
    """
    return os.getenv("SLACK_AUTHORIZED_USER_IDS", "").strip() == "*"


def _authorized_user_ids() -> set[str]:
    raw = os.getenv("SLACK_AUTHORIZED_USER_IDS", "").strip()
    if not raw or raw == "*":
        return set()
    return {u.strip() for u in raw.split(",") if u.strip()}


def _is_authorized(user_id: str) -> bool:
    if _allow_all_users():
        return True
    allowed = _authorized_user_ids()
    if not allowed:
        # If the allowlist is empty, fail closed.
        return False
    return user_id in allowed


# ---------------------------------------------------------------------------
# Roles & permissions (RBAC)
# ---------------------------------------------------------------------------
# Roles live in the sheet's Team tab (managed by admins via the bot). Admins can
# also be seeded in .env (SLACK_ADMIN_USER_IDS) so there's always a recovery path.
# Reads are open to any authorized user; writes are gated by the matrix below.

# Write action -> roles permitted. "admin" is allowed everywhere (handled in code).
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "add_artist": {"onboarding"},
    "update_field": {"marketing", "rise", "onboarding"},
    "log_conversation": {"rise", "onboarding"},
    "log_marketing": {"marketing"},
    "assign_role": set(),  # admin-only (admin passes via the global check)
}

# File uploads gated per Drive category (target_field) -> roles permitted.
FILE_CATEGORY_ROLES: dict[str, set[str]] = {
    "marketing_material_drive_url": {"marketing"},
    "press_pics_drive_url": {"onboarding", "rise"},
    "artist_info_drive_url": {"onboarding", "rise"},
    "rise_material_drive_url": {"rise"},
}

# Per-field overrides for update_field. Fields not listed fall back to
# ROLE_PERMISSIONS["update_field"] (any of marketing/rise/onboarding). admin always passes.
FIELD_PERMISSIONS: dict[str, set[str]] = {
    "tier": {"rise"},
    "rise_associate": {"rise", "onboarding"},
}

# Any role that can upload to at least one category (used for the coarse pre-check).
ANY_UPLOAD_ROLES = {r for roles in FILE_CATEGORY_ROLES.values() for r in roles}

# Intents that anyone authorized may run (no role needed).
READ_INTENTS = {
    "query_artist", "query_roster", "query_marketing",
    "get_asset_link", "get_sheet_link", "query_team",
    "clarify", "unknown",
}

# Human labels for roles in messages.
ROLE_LABELS = {
    "admin": "admin", "marketing": "marketing", "rise": "rise", "onboarding": "onboarding",
}

# Process-lifetime cache of user_id -> roles set. Invalidated when roles change.
_roles_lock = threading.Lock()
_roles_cache: dict[str, set[str]] = {}


def _admin_bootstrap_ids() -> set[str]:
    raw = os.getenv("SLACK_ADMIN_USER_IDS", "").strip()
    return {u.strip() for u in raw.split(",") if u.strip()}


def _user_roles(user_id: str) -> set[str]:
    """Effective roles for a user = Team-tab roles ∪ ({admin} if seeded in .env). Cached."""
    if not user_id:
        return set()
    with _roles_lock:
        if user_id in _roles_cache:
            return set(_roles_cache[user_id])
    roles: set[str] = set()
    try:
        roles = set(artist_crm.get_user_roles(user_id))
    except Exception as e:
        logger.warning(f"get_user_roles failed for {user_id}: {e}")
    if user_id in _admin_bootstrap_ids():
        roles.add("admin")
    with _roles_lock:
        _roles_cache[user_id] = set(roles)
    return roles


def _invalidate_roles_cache(user_id: str | None = None) -> None:
    with _roles_lock:
        if user_id is None:
            _roles_cache.clear()
        else:
            _roles_cache.pop(user_id, None)


def _roles_phrase(roles: set[str]) -> str:
    """e.g. {'marketing','rise'} -> 'marketing or rise'; empty -> 'an admin'."""
    rs = [ROLE_LABELS[r] for r in ("onboarding", "rise", "marketing", "admin") if r in roles]
    if not rs:
        return "an admin"
    if len(rs) == 1:
        return rs[0]
    return ", ".join(rs[:-1]) + " or " + rs[-1]


def _check_permission(roles: set[str], action: str, target_field: str | None = None) -> tuple[bool, str]:
    """Return (allowed, reason). `reason` is a user-facing guidance string when denied."""
    if action in READ_INTENTS:
        return True, ""
    if "admin" in roles:
        return True, ""

    your = f"Your role: {', '.join(sorted(roles)) if roles else 'none yet'}."

    if action == "file_upload":
        if target_field is None:
            # Category not known yet — allow only if the user can upload *something*.
            if roles & ANY_UPLOAD_ROLES:
                return True, ""
            return False, f"Uploading files needs a team role. {your} Ask an admin to add you."
        needed = FILE_CATEGORY_ROLES.get(target_field, set())
        if roles & needed:
            return True, ""
        label = CATEGORY_LABELS.get(target_field, target_field)
        return False, (
            f"Uploading to *{label}* is limited to the {_roles_phrase(needed)} team (or an admin). {your}"
        )

    needed = ROLE_PERMISSIONS.get(action)
    if needed is None:
        # Unknown/ungated action — allow (reads already handled above).
        return True, ""
    if roles & needed:
        return True, ""

    action_phrase = {
        "add_artist": "Adding new artists",
        "update_field": "Updating artist info",
        "log_conversation": "Logging conversations",
        "log_marketing": "Logging marketing features",
        "assign_role": "Managing team roles",
    }.get(action, "That action")
    who = _roles_phrase(needed) if needed else "an admin"
    return False, f"{action_phrase} is for the {who} team (or an admin). {your} Ask an admin to grant you the role."


def _can_edit_field(roles: set[str], field: str) -> bool:
    """Per-field edit check for update_field. admin always allowed."""
    if "admin" in roles:
        return True
    needed = FIELD_PERMISSIONS.get(field, ROLE_PERMISSIONS["update_field"])
    return bool(roles & needed)


def _field_denial(field: str, roles: set[str]) -> str:
    needed = FIELD_PERMISSIONS.get(field, ROLE_PERMISSIONS["update_field"])
    your = f"Your role: {', '.join(sorted(roles)) if roles else 'none yet'}."
    return f"`{field}` can only be edited by the {_roles_phrase(needed)} team (or an admin). {your}"


# ---------------------------------------------------------------------------
# Pending-file state (in-memory, keyed by channel)
# ---------------------------------------------------------------------------
# When a file is dropped but the artist or folder is still unknown, we stash it
# here and wait for the next message in the same conversation to disambiguate.
# Keyed by `channel` (not thread_ts) so it works in DMs, where every message has
# its own ts and no thread — matching how _conv_history is keyed. We also remember
# the resolved artist_id so a category-only reply ("press pics") can complete.
# Lost on restart.

_pending_lock = threading.Lock()
_pending_files: dict[str, dict[str, Any]] = {}  # channel -> {files, target_field, artist_id}


def _stash_pending_file(channel: str, files: list[dict], target_field: str | None, artist_id: str | None = None) -> None:
    with _pending_lock:
        _pending_files[channel] = {"files": files, "target_field": target_field, "artist_id": artist_id}


def _pop_pending_file(channel: str) -> dict | None:
    with _pending_lock:
        return _pending_files.pop(channel, None)


# ---------------------------------------------------------------------------
# Artist resolution
# ---------------------------------------------------------------------------

def _resolve_artist(text: str | None) -> dict:
    """Wrap artist_crm.find_artist for routing. Always returns a dict with status."""
    if not text:
        return {"status": "missing", "matches": []}
    result = artist_crm.find_artist(text)
    if result["confidence"] == "exact" and len(result["matches"]) == 1:
        return {"status": "ok", "artist": result["matches"][0]}
    if result["confidence"] == "fuzzy" and len(result["matches"]) == 1:
        return {"status": "ok", "artist": result["matches"][0]}
    if len(result["matches"]) > 1:
        return {"status": "ambiguous", "matches": result["matches"]}
    return {"status": "none", "matches": []}


def _fmt_artist_brief(a: dict) -> str:
    bits = [a.get("name") or a.get("artist_id"), f"(@{a.get('artist_id')})"]
    if a.get("tier"):
        bits.append(f"— {a['tier']}")
    return " ".join(bits)


# Keyword → file target, for resolving a bare category reply to "which folder?".
_TARGET_KEYWORDS = {
    "press_pics_drive_url": ["press shot", "press pic", "headshot", "photo", "photoshoot"],
    "marketing_material_drive_url": ["marketing", "promo", "advert", "graphic", "asset", "flyer", "poster"],
    "artist_info_drive_url": ["artist info", "track data", "master", "stem", "audio", "song file", "wav", "mp3", "track"],
    "rise_material_drive_url": ["rise material", "rise", "content idea", "reference", "mockup", "inspo", "moodboard"],
}


def _match_target_field(text: str) -> str | None:
    """Map a free-text category reference to a file target field, or None."""
    t = (text or "").lower()
    for field, keywords in _TARGET_KEYWORDS.items():
        if any(k in t for k in keywords):
            return field
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_log_conversation(intent: dict, user_id: str, channel: str, reply, client=None) -> None:
    artist_ref = intent.get("artist_reference")
    summary = intent.get("summary") or ""
    if not summary:
        reply("I couldn't extract a clean summary from that. Try: \"logged a call with Sam about cover art.\"")
        return

    resolved = _resolve_artist(artist_ref)
    if resolved["status"] == "missing":
        reply("Which artist is this about?")
        return
    if resolved["status"] == "none":
        reply(
            f"I don't have an artist matching \"{artist_ref}\" yet. "
            f"If they're new, say: `add new artist <name>, instagram <handle>, email <addr>`"
        )
        return
    if resolved["status"] == "ambiguous":
        names = ", ".join(_fmt_artist_brief(m) for m in resolved["matches"][:5])
        reply(f"More than one match for \"{artist_ref}\": {names}. Which one?")
        return

    artist = resolved["artist"]
    author_name = _resolve_user_name(client, user_id)
    artist_crm.append_conversation(
        artist_id=artist["artist_id"],
        author_slack_id=user_id,
        channel=channel,
        summary=summary,
        created_by=author_name,
    )
    log_event("slack_action", {
        "action": "append_conversation",
        "artist_id": artist["artist_id"],
        "user_id": user_id,
        "created_by": author_name,
        "summary": summary,
    })
    reply(f"Logged for *{_fmt_artist_brief(artist)}*: {summary}")


def handle_log_marketing(intent: dict, user_id: str, channel: str, reply, client=None) -> None:
    summary = intent.get("summary") or ""
    feature_type = intent.get("feature_type") or "other"
    placement = intent.get("placement") or ""
    if not summary:
        reply("I couldn't tell what the feature was. Try: \"Dru was featured on a paid campaign.\"")
        return

    # One feature can name multiple artists ("the Daily Rush this week had Dru, Yas, Sam").
    refs = intent.get("artist_references") or []
    if not refs and intent.get("artist_reference"):
        refs = [intent["artist_reference"]]
    if not refs:
        reply("Which artist(s) is this marketing feature for?")
        return

    author_name = _resolve_user_name(client, user_id)
    logged: list[dict] = []
    not_found: list[str] = []
    ambiguous: list[tuple[str, list[dict]]] = []  # (ref, candidate matches)
    for ref in refs:
        resolved = _resolve_artist(ref)
        if resolved["status"] == "ok":
            artist = resolved["artist"]
            artist_crm.append_marketing(
                artist_id=artist["artist_id"],
                author_slack_id=user_id,
                channel=channel,
                feature_type=feature_type,
                placement=placement,
                summary=summary,
                created_by=author_name,
            )
            logged.append(artist)
        elif resolved["status"] == "ambiguous":
            ambiguous.append((ref, resolved["matches"]))
        else:  # none / missing
            not_found.append(ref)

    label = FEATURE_TYPE_LABELS.get(feature_type, feature_type)
    tag = f"{label} — {placement}" if placement else label

    if logged:
        log_event("slack_action", {
            "action": "log_marketing",
            "artist_ids": [a["artist_id"] for a in logged],
            "user_id": user_id,
            "created_by": author_name,
            "feature_type": feature_type,
            "placement": placement,
            "summary": summary,
            "skipped": {"not_found": not_found, "ambiguous": [r for r, _ in ambiguous]},
        })

    # Build the reply: confirm what was logged, then ASK to clarify ambiguous names
    # and flag any with no match. The user's next reply (with thread context) re-runs
    # log_marketing for the clarified artist using the same feature.
    parts = []
    if logged:
        names = ", ".join(f"*{_fmt_artist_brief(a)}*" for a in logged)
        parts.append(f"Logged marketing ({tag}) for {names}: {summary}")
    for ref, matches in ambiguous:
        cands = ", ".join(_fmt_artist_brief(m) for m in matches[:5])
        parts.append(f'"{ref}" matches more than one artist: {cands}. Which one should I log the {tag} feature for?')
    if not_found:
        nf = ", ".join(f'"{r}"' for r in not_found)
        parts.append(f"No match for {nf} — add them first (`add new artist ...`) or use the exact @handle.")
    reply("\n".join(parts))


def handle_query_artist(intent: dict, user_id: str, channel: str, reply, client=None) -> None:
    artist_ref = intent.get("artist_reference")
    question = intent.get("question") or "Where are we at with this artist?"

    resolved = _resolve_artist(artist_ref)
    if resolved["status"] == "missing":
        reply("Which artist do you want to know about?")
        return
    if resolved["status"] == "none":
        reply(f"I don't have an artist matching \"{artist_ref}\".")
        return
    if resolved["status"] == "ambiguous":
        names = ", ".join(_fmt_artist_brief(m) for m in resolved["matches"][:5])
        reply(f"More than one match for \"{artist_ref}\": {names}. Which one?")
        return

    artist = resolved["artist"]
    convs = artist_crm.read_conversations(artist["artist_id"])
    marketing = artist_crm.read_marketing(artist["artist_id"])

    if not convs and not marketing:
        reply(f"*{_fmt_artist_brief(artist)}* — no logged conversations or marketing yet.")
        return

    summary = _summarize_artist(artist, convs, question, marketing=marketing, client=client)
    log_event("slack_action", {
        "action": "summarize_artist",
        "artist_id": artist["artist_id"],
        "user_id": user_id,
        "question": question,
        "n_conversations": len(convs),
    })
    reply(f"*{_fmt_artist_brief(artist)}*\n{summary}")


def handle_query_roster(intent: dict, user_id: str, channel: str, reply) -> None:
    question = intent.get("question") or "List every artist in the roster."
    artists = artist_crm.read_artists()
    if not artists:
        reply("There are no artists in the repository yet.")
        return
    conversations = artist_crm.read_conversations()  # all rows, sorted oldest->newest
    answer = _answer_roster_query(artists, conversations, question)
    log_event("slack_action", {
        "action": "query_roster",
        "user_id": user_id,
        "question": question,
        "n_artists": len(artists),
        "n_conversations": len(conversations),
    })
    reply(answer)


def handle_query_marketing(intent: dict, user_id: str, channel: str, reply) -> None:
    question = intent.get("question") or "Summarize marketing features across the roster."
    artists = artist_crm.read_artists()
    if not artists:
        reply("There are no artists in the repository yet.")
        return
    marketing = artist_crm.read_marketing()  # all rows, sorted oldest->newest
    answer = _answer_marketing_query(artists, marketing, question)
    log_event("slack_action", {
        "action": "query_marketing",
        "user_id": user_id,
        "question": question,
        "n_artists": len(artists),
        "n_marketing": len(marketing),
    })
    reply(answer)


def handle_get_asset_link(intent: dict, user_id: str, channel: str, reply) -> None:
    resolved = _resolve_artist(intent.get("artist_reference"))
    if resolved["status"] == "missing":
        reply("Which artist's folder do you want the link to?")
        return
    if resolved["status"] == "none":
        reply(f"I don't have an artist matching \"{intent.get('artist_reference')}\".")
        return
    if resolved["status"] == "ambiguous":
        names = ", ".join(_fmt_artist_brief(m) for m in resolved["matches"][:5])
        reply(f"More than one match: {names}. Which one?")
        return

    artist = resolved["artist"]
    target = intent.get("target_field")
    fields = [target] if target else list(CATEGORY_LABELS.keys())

    lines = []
    for f in fields:
        label = CATEGORY_LABELS.get(f, f)
        url = (artist.get(f) or "").strip()
        if url:
            lines.append(f"*{label}:* {url}")
        else:
            lines.append(f"*{label}:* no folder yet — drop a file to create it")

    log_event("slack_action", {
        "action": "get_asset_link",
        "artist_id": artist["artist_id"],
        "user_id": user_id,
        "fields": fields,
    })
    reply(f"*{_fmt_artist_brief(artist)}*\n" + "\n".join(lines))


def handle_get_sheet_link(intent: dict, user_id: str, channel: str, reply) -> None:
    url = os.getenv("ARTIST_CRM_SHEET_URL", "").strip()
    if not url:
        reply("The sheet isn't configured yet — ask an admin to run the provisioning step.")
        return
    log_event("slack_action", {"action": "get_sheet_link", "user_id": user_id})
    reply(f"Here's the artist repository (view-only):\n{url}")


def _capabilities_phrase(roles: set[str]) -> str:
    """One-line summary of what a role set can do, derived from the permission matrix."""
    if "admin" in roles:
        return "You're an *admin* — you can do everything."
    if not roles:
        return ("You don't have a team role yet, so you can look things up but can't make "
                "changes. Ask an admin to add you to a team.")
    caps = []
    if "onboarding" in roles:
        caps.append("add new artists")
    if roles & {"rise", "onboarding"}:
        caps.append("log conversations")
    if "marketing" in roles:
        caps.append("log marketing features")
    if roles & {"marketing", "rise", "onboarding"}:
        caps.append("update artist info")
    upload_cats = sorted({CATEGORY_LABELS[tf] for tf, allowed in FILE_CATEGORY_ROLES.items() if roles & allowed})
    if upload_cats:
        caps.append("upload files to " + ", ".join(upload_cats))
    caps.append("look up artists, conversations and marketing")
    return "You can " + "; ".join(caps) + "."


def handle_assign_role(intent: dict, user_id: str, channel: str, text: str, reply, client=None) -> None:
    import re
    roles = [r for r in (intent.get("roles") or []) if r in artist_crm.VALID_ROLES]
    action = intent.get("role_action") or "add"
    if not roles:
        reply("Which role? Say e.g. `make @Jane marketing` (roles: admin, marketing, rise, onboarding).")
        return

    # Extract target user IDs from the raw message — Slack mentions first, then bare U-IDs.
    targets = re.findall(r"<@([A-Z0-9]+)>", text or "")
    if not targets:
        targets = [t for t in re.findall(r"\b(U[A-Z0-9]{6,})\b", text or "") if t != user_id]
    # Don't act on the bot's own mention.
    targets = [t for t in dict.fromkeys(targets)]  # de-dup, preserve order
    if not targets:
        reply("Who should I update? @mention them, e.g. `make @Jane marketing`.")
        return

    admin_name = _resolve_user_name(client, user_id)
    results = []
    for uid in targets:
        name = _resolve_user_name(client, uid)
        for role in roles:
            if action == "remove":
                artist_crm.remove_user_role(uid, role, added_by=admin_name)
            else:
                artist_crm.add_user_role(uid, role, name=name, added_by=admin_name)
        _invalidate_roles_cache(uid)
        now_roles = _user_roles(uid)
        now = ", ".join(sorted(now_roles)) if now_roles else "no roles"
        results.append(f"*{name}* → {now}")

    verb = "Removed" if action == "remove" else "Added"
    role_list = ", ".join(roles)
    log_event("slack_action", {
        "action": "assign_role",
        "by": user_id, "targets": targets, "roles": roles, "role_action": action,
    })
    reply(f"{verb} {role_list}.\n" + "\n".join(results))


def handle_query_team(intent: dict, user_id: str, channel: str, reply, client=None) -> None:
    question = (intent.get("question") or "").lower()
    personal = (not question) or any(k in question for k in ("my role", "what can i", "my permission", "am i", "do i have"))

    if personal:
        roles = _user_roles(user_id)
        role_str = ", ".join(sorted(roles)) if roles else "none"
        reply(f"*Your role:* {role_str}\n{_capabilities_phrase(roles)}")
        return

    # Team listing, optionally filtered to a role named in the question.
    try:
        team = artist_crm.read_team()
    except Exception as e:
        reply(f"Couldn't read the team list: {e}")
        return

    named_role = next((r for r in artist_crm.VALID_ROLES if r in question), None)
    by_role: dict[str, list[str]] = {r: [] for r in ("admin", "marketing", "rise", "onboarding")}
    for row in team:
        name = row.get("name") or row.get("user_id")
        for r in artist_crm._parse_roles(row.get("roles", "")):
            by_role.setdefault(r, []).append(name)
    # Include env-seeded admins who may not have a Team row.
    for uid in _admin_bootstrap_ids():
        nm = _resolve_user_name(client, uid)
        if nm not in by_role["admin"]:
            by_role["admin"].append(nm)

    if named_role:
        members = by_role.get(named_role, [])
        if members:
            reply(f"*{named_role.capitalize()} team:* " + ", ".join(sorted(members)))
        else:
            reply(f"No one is on the *{named_role}* team yet.")
        return

    lines = ["*Team roles:*"]
    for r in ("admin", "marketing", "rise", "onboarding"):
        members = sorted(set(by_role.get(r, [])))
        lines.append(f"• *{r.capitalize()}:* " + (", ".join(members) if members else "—"))
    reply("\n".join(lines))


def handle_add_artist(intent: dict, user_id: str, channel: str, reply) -> None:
    fields = dict(intent.get("fields") or {})
    artist_id = fields.get("artist_id") or intent.get("artist_reference")
    if not artist_id:
        reply("To add an artist I need at least an Instagram handle. Try: `add new artist Sam Harper, instagram samharper`.")
        return
    fields["artist_id"] = artist_id.lstrip("@").strip()

    try:
        a = artist_crm.add_artist(fields)
    except ValueError as e:
        reply(f"Couldn't add artist: {e}")
        return

    log_event("slack_action", {
        "action": "add_artist",
        "artist_id": a["artist_id"],
        "user_id": user_id,
        "fields": fields,
    })
    set_fields = ", ".join(f"{k}={v}" for k, v in fields.items() if v and k != "artist_id")
    reply(f"Added *{_fmt_artist_brief(a)}*" + (f" — {set_fields}" if set_fields else ""))


def handle_update_field(intent: dict, user_id: str, channel: str, reply) -> None:
    fields = intent.get("fields") or {}
    if not fields:
        reply("Tell me which field to update, e.g. `update Sam support_level to Tier 1`.")
        return

    resolved = _resolve_artist(intent.get("artist_reference"))
    if resolved["status"] != "ok":
        if resolved["status"] == "ambiguous":
            names = ", ".join(_fmt_artist_brief(m) for m in resolved["matches"][:5])
            reply(f"Multiple matches: {names}. Which one?")
        else:
            reply(f"I don't have an artist matching \"{intent.get('artist_reference')}\".")
        return

    artist = resolved["artist"]
    roles = _user_roles(user_id)
    updated_fields = []
    denied = []
    for field, value in fields.items():
        if field == "artist_id":
            continue
        if not _can_edit_field(roles, field):
            denied.append(field)
            continue
        try:
            artist_crm.update_artist_field(artist["artist_id"], field, value)
            updated_fields.append(f"{field}={value}")
        except ValueError as e:
            reply(f"Couldn't update `{field}`: {e}")
            return

    if updated_fields:
        log_event("slack_action", {
            "action": "update_artist_field",
            "artist_id": artist["artist_id"],
            "user_id": user_id,
            "fields": {k: fields[k] for k in fields if any(uf.startswith(f"{k}=") for uf in updated_fields)},
        })

    parts = []
    if updated_fields:
        parts.append(f"Updated *{_fmt_artist_brief(artist)}* — {', '.join(updated_fields)}")
    for field in denied:
        log_event("slack_rejected", {
            "user_id": user_id, "action": "update_field", "field": field, "roles": sorted(roles),
        })
        parts.append(_field_denial(field, roles))
    reply("\n".join(parts) if parts else "Nothing to update.")


def handle_file_upload(
    intent: dict,
    user_id: str,
    channel: str,
    files: list[dict] | None,
    thread_ts: str,
    reply,
) -> None:
    if not files:
        reply("I didn't see an attached file on that message.")
        return

    artist_ref = intent.get("artist_reference")
    target_field = intent.get("target_field")

    # If we can't tell which artist this is for, stash (remembering any known
    # category) and ask. Keyed by channel so the next DM message completes it.
    resolved = _resolve_artist(artist_ref)
    if resolved["status"] != "ok":
        _stash_pending_file(channel, files, target_field, artist_id=None)
        n = len(files)
        got = f"Got the {'file' if n == 1 else f'{n} files'}."
        if resolved["status"] == "ambiguous":
            names = ", ".join(_fmt_artist_brief(m) for m in resolved["matches"][:5])
            reply(f"{got} Multiple matches for \"{artist_ref}\": {names}. Which one?")
        else:
            reply(f"{got} Which artist is this for?")
        return

    artist = resolved["artist"]

    # Default target_field by mime only for audio (→ Artist Info). Everything else
    # needs an explicit category, since images could be press shots OR marketing
    # material OR rise material. Remember the artist so the follow-up completes.
    if not target_field:
        mimes = [(f.get("mimetype") or "").lower() for f in files]
        if all(m.startswith("audio/") for m in mimes):
            target_field = "artist_info_drive_url"
        else:
            _stash_pending_file(channel, files, None, artist_id=artist["artist_id"])
            reply(
                f"Got it — for *{_fmt_artist_brief(artist)}*. Which folder — "
                "*Press Shots*, *Marketing Material*, *Artist Info*, or *Rise Material*?"
            )
            return

    _do_folder_upload(artist, target_field, files, user_id, reply)


def _do_folder_upload(artist: dict, target_field: str, files: list[dict], user_id: str, reply) -> None:
    # Precise per-category permission check at the point of writing — covers the
    # audio→Artist Info default and the deferred "which folder?" path, where the
    # category isn't known when _route's coarse gate runs.
    allowed, reason = _check_permission(_user_roles(user_id), "file_upload", target_field)
    if not allowed:
        log_event("slack_rejected", {
            "user_id": user_id, "action": "file_upload",
            "target_field": target_field, "reason": reason,
        })
        reply(reason)
        return

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        reply("Internal error: SLACK_BOT_TOKEN not configured.")
        return

    _, drive_svc = artist_crm._get_clients()

    # Ensure the artist's category folder exists (root → artist → category).
    try:
        folder = ensure_category_folder(drive_svc, artist, target_field)
    except Exception as e:
        log_event("slack_action", {
            "action": "folder_create_failed",
            "artist_id": artist["artist_id"], "target_field": target_field, "error": str(e),
        })
        reply(f"Couldn't create the Drive folder: {e}")
        return

    # Upload each file into the folder.
    uploaded, failed = [], []
    for f in files:
        try:
            res = ingest_slack_file(f, bot_token, drive_svc, folder_id=folder["folder_id"])
            uploaded.append(res["name"])
        except Exception as e:
            failed.append(f"{f.get('name', 'file')} ({e})")

    if not uploaded:
        log_event("slack_action", {
            "action": "folder_upload_failed",
            "artist_id": artist["artist_id"], "target_field": target_field, "failed": failed,
        })
        reply(f"Upload failed: {'; '.join(failed)}")
        return

    # Point the sheet field at the folder (idempotent — same link each time).
    artist_crm.set_artist_file_url(artist["artist_id"], target_field, folder["folder_url"])
    log_event("slack_action", {
        "action": "folder_upload",
        "artist_id": artist["artist_id"], "user_id": user_id,
        "target_field": target_field, "uploaded": uploaded, "failed": failed,
        "folder_url": folder["folder_url"],
    })

    label = CATEGORY_LABELS.get(target_field, target_field)
    n = len(uploaded)
    msg = (
        f"Added {n} file{'s' if n != 1 else ''} to *{_fmt_artist_brief(artist)}*'s "
        f"*{label}* folder:\n{folder['folder_url']}"
    )
    if failed:
        msg += f"\n(Couldn't upload: {'; '.join(failed)})"
    reply(msg)


def handle_clarify(intent: dict, user_id: str, reply) -> None:
    # Prefer a specific follow-up question from the classifier over the generic menu.
    question = intent.get("question")
    if question:
        reply(question)
        return
    reply(
        "I didn't quite catch that. I can log a conversation, tell you where things "
        "are with an artist, add a new artist, update a field, or store a press pic / track. "
        "What would you like?"
    )


# ---------------------------------------------------------------------------
# Smart summary
# ---------------------------------------------------------------------------

def _summarize_artist(artist: dict, convs: list[dict], question: str, marketing: list[dict] | None = None, client=None) -> str:
    """Synthesize a status summary using Claude Sonnet. `client` is the Slack web
    client used to resolve author user IDs to display names. `marketing` is the
    artist's marketing-feature rows; when present, the summary gains a *Marketing:* line."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "(no ANTHROPIC_API_KEY — cannot synthesize summary)"

    artist_record_lines = [f"  {k}: {v}" for k, v in artist.items() if v]
    conv_lines = []
    for c in convs:
        author = _resolve_user_name(client, c.get("author_slack_id", ""))
        date_short = (c.get("date_iso") or "")[:10] or "?"
        conv_lines.append(f"[{date_short}] {author}: {c.get('summary', '')}")
    convs_block = "\n".join(conv_lines) or "(none logged)"
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")

    # Marketing block: counts by type + dated feature lines (newest first).
    marketing = marketing or []
    if marketing:
        counts = _marketing_count_phrase(marketing)
        mkt_lines = []
        for m in sorted(marketing, key=lambda r: r.get("date_iso", ""), reverse=True):
            date_short = (m.get("date_iso") or "")[:10] or "?"
            label = FEATURE_TYPE_LABELS.get(m.get("feature_type", ""), m.get("feature_type", ""))
            placement = f" — {m['placement']}" if m.get("placement") else ""
            mkt_lines.append(f"[{date_short}] {label}{placement}: {m.get('summary', '')}")
        marketing_block = f"Totals: {counts}\n" + "\n".join(mkt_lines)
        marketing_instruction = (
            '- After the conversation summary, on a NEW LINE write "*Marketing:*" followed by a one-line '
            "recap of marketing features by type with notable placements (e.g. \"2 paid campaigns, 1 Daily Rush feature\"). "
            "Use the Totals provided; name standout placements. Keep it to one line."
        )
    else:
        marketing_block = "(none logged)"
        marketing_instruction = '- Do NOT write a "*Marketing:*" line — there are no marketing features on file.'

    prompt = f"""You summarize the team's relationship with one music artist. Output goes directly into Slack DM.

ARTIST RECORD:
{chr(10).join(artist_record_lines)}

CONVERSATION HISTORY (oldest -> newest), each prefixed with the author who logged it:
{convs_block}

MARKETING FEATURES (the marketing team's logged placements for this artist):
{marketing_block}

USER ASKED: "{question}"

Today: {today_iso}

OUTPUT FORMAT — follow exactly:
- A single prose paragraph (1-4 sentences) integrating the conversation log chronologically. Reference authors by name when their action matters (e.g. "Timmy onboarded the artist last week and promised playlist placements").
- COLLAPSE overlapping log entries that describe the same multi-step thread. Only the latest state matters. Examples: "promised to connect with marketing" → "connected with marketing" → "marketing met with artist" should be summarized as just the latest ("marketing team met with the artist X days ago"). Do not retell each step.
- Items that were committed/promised once and have NO follow-up entry are still outstanding — mention them and credit the author who promised them.
{marketing_instruction}
- Then on a NEW LINE write "*Next steps:*" followed by what's still outstanding, written as a short comma-separated list or 1-2 short clauses. Infer next steps from unresolved promises and the latest pending state. NEVER write filler like "none logged" or "no explicit next steps" — if everything is resolved, just omit the "*Next steps:*" line entirely.

STRICT FORMATTING:
- Slack mrkdwn only. Use *single asterisks* for bold. Do NOT use **double asterisks**.
- Use relative dates ("today", "yesterday", "2 days ago", "last week") computed from the "Today" above. Never ISO dates.
- No section headers like "Current Status", "Recent Context", or "Open / Next Steps". Just the prose paragraph, the optional "*Marketing:*" line, and the optional "*Next steps:*" line.
- Under 160 words total.

If the user's question is narrower than "where are we at", answer that question directly in 1-2 sentences first, then the prose summary.
"""

    try:
        anthropic_client = anthropic.Anthropic()
        msg = anthropic_client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(summary failed: {e})"


def _format_roster(artists: list[dict]) -> str:
    """One numbered labeled block per artist; blank cells shown as '—'."""
    lines = []
    for i, a in enumerate(artists, 1):
        parts = [f"{col}={a.get(col) or '—'}" for col in artist_crm.ARTIST_COLUMNS]
        lines.append(f"{i}. " + " | ".join(parts))
    return "\n".join(lines)


def _format_conversation_activity(artists: list[dict], conversations: list[dict]) -> str:
    """Per-artist conversation digest: last contact, total count, and recent entries.

    Shown for every artist (including those with zero conversations, so "no updates
    yet" queries work). Recent entries are capped at the newest 5 per artist.
    """
    by_artist: dict[str, list[dict]] = {}
    for c in conversations:
        by_artist.setdefault(c.get("artist_id", ""), []).append(c)

    lines = []
    for a in artists:
        aid = a["artist_id"]
        convs = sorted(by_artist.get(aid, []), key=lambda c: c.get("date_iso", ""))
        label = f"{a.get('name') or aid} [{aid}]"
        if not convs:
            lines.append(f"{label}: last_contact=never, total=0")
            continue
        last_contact = (convs[-1].get("date_iso") or "")[:10] or "?"
        recent = list(reversed(convs))[:5]  # newest first, up to 5
        lines.append(f"{label}: last_contact={last_contact}, total={len(convs)}")
        for c in recent:
            date = (c.get("date_iso") or "")[:10] or "?"
            who = c.get("created_by") or c.get("author_slack_id") or "?"
            summary = (c.get("summary") or "").strip()
            if len(summary) > 120:
                summary = summary[:117] + "..."
            lines.append(f"  - [{date}] {who}: {summary}")
    return "\n".join(lines)


def _answer_roster_query(artists: list[dict], conversations: list[dict], question: str) -> str:
    """Answer an aggregate / filter question across the whole roster via Claude.

    Reasons over both artist fields and per-artist conversation activity.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "(no ANTHROPIC_API_KEY — cannot answer roster queries)"

    roster_block = _format_roster(artists)
    activity_block = _format_conversation_activity(artists, conversations)
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    prompt = f"""You answer questions about a label's roster of music artists. You have two data
sources: the artist ROSTER (profile fields) and CONVERSATION ACTIVITY (a per-artist log digest).

In the ROSTER, a value of "—" means that field is BLANK (no data on file); "n/a" means the
field was explicitly marked not-applicable (e.g. not in the Rise program).

In CONVERSATION ACTIVITY, each artist shows last_contact (date of most recent logged touchpoint,
or "never"), total (number of logged conversations), and up to the 5 most recent entries
([date] author: summary). "total=0" / "last_contact=never" means no conversations are logged yet.

Today: {today_iso}

ROSTER ({len(artists)} artist(s)):
{roster_block}

CONVERSATION ACTIVITY:
{activity_block}

USER ASKED: "{question}"

You can answer questions about EITHER source or both, including:
- profile fields / assignments / missing data ("which artists have no rise associate", "who's missing a spotify link"),
- conversation recency ("which artists haven't we contacted in a while" → compare last_contact to Today; with no stated threshold, treat roughly 2+ weeks, and include never-contacted artists),
- activity windows ("who did we contact this week / this month / since <date>" → use the dated entries vs Today),
- no activity yet ("which artists have no conversation updates" → total=0),
- who logged updates ("what has Cameron logged" → filter by the author in the entries).

Rules:
- Answer directly and concisely. Output goes into Slack.
- For filter questions, list EVERY matching artist by *name* with their @handle. If none match, say so plainly.
- Treat "—" (blank) as missing / unassigned; "n/a" as a deliberate not-applicable (don't count as missing unless asked).
- Use relative dates ("3 days ago", "last week") computed from Today — not ISO strings.
- Use Slack mrkdwn: *single asterisks* for bold, "• " for bullet points. Never use tables or **double asterisks**.
- Do NOT invent artists, fields, or conversations that aren't in the data.
- Keep it tight: a short lead sentence, then a bullet list when naming multiple artists.
"""
    try:
        anthropic_client = anthropic.Anthropic()
        msg = anthropic_client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(roster query failed: {e})"


# ---------------------------------------------------------------------------
# Marketing aggregation
# ---------------------------------------------------------------------------

def _marketing_count_phrase(marketing: list[dict]) -> str:
    """e.g. '2 paid campaigns, 1 daily press, 1 social' — only non-zero types."""
    order = ["paid_campaign", "daily_press", "social", "other"]
    counts: dict[str, int] = {}
    for m in marketing:
        ft = m.get("feature_type") or "other"
        counts[ft] = counts.get(ft, 0) + 1
    parts = []
    for ft in order:
        n = counts.get(ft, 0)
        if n:
            parts.append(f"{n} {FEATURE_TYPE_LABELS.get(ft, ft)}")
    return ", ".join(parts) if parts else "none"


def _format_marketing_activity(artists: list[dict], marketing: list[dict]) -> str:
    """Per-artist marketing digest: counts by feature_type + recent feature lines.

    Shown for every artist (including those with zero features, so "never featured"
    queries work). Recent entries are capped at the newest 6 per artist.
    """
    by_artist: dict[str, list[dict]] = {}
    for m in marketing:
        by_artist.setdefault(m.get("artist_id", ""), []).append(m)

    lines = []
    for a in artists:
        aid = a["artist_id"]
        feats = sorted(by_artist.get(aid, []), key=lambda m: m.get("date_iso", ""))
        label = f"{a.get('name') or aid} [{aid}]"
        if not feats:
            lines.append(f"{label}: total=0 (never featured)")
            continue
        lines.append(f"{label}: total={len(feats)} ({_marketing_count_phrase(feats)})")
        for m in list(reversed(feats))[:6]:  # newest first, up to 6
            date = (m.get("date_iso") or "")[:10] or "?"
            ft = FEATURE_TYPE_LABELS.get(m.get("feature_type", ""), m.get("feature_type", ""))
            placement = f" — {m['placement']}" if m.get("placement") else ""
            summary = (m.get("summary") or "").strip()
            if len(summary) > 100:
                summary = summary[:97] + "..."
            lines.append(f"  - [{date}] {ft}{placement}: {summary}")
    return "\n".join(lines)


def _answer_marketing_query(artists: list[dict], marketing: list[dict], question: str) -> str:
    """Answer a marketing question (counts / history / gaps) across the roster via Claude."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "(no ANTHROPIC_API_KEY — cannot answer marketing queries)"

    activity_block = _format_marketing_activity(artists, marketing)
    today_iso = datetime.now(PT).strftime("%Y-%m-%d")
    prompt = f"""You answer questions about a music label's MARKETING activity. Each artist has a digest of
their logged marketing FEATURES (placements the marketing team secured).

Feature types are a fixed set:
- paid_campaign — paid ads / promoted posts / ad campaigns
- daily_press — press / blog / editorial features (e.g. the "Daily Rush")
- social — organic IG / TikTok / social features
- other — anything else

Each artist line shows total= and a count per type, then up to 6 recent features as
[date] type — placement: summary. "total=0 (never featured)" means no marketing logged.

Today: {today_iso}

MARKETING ACTIVITY ({len(artists)} artist(s)):
{activity_block}

USER ASKED: "{question}"

You can answer:
- counts for one artist ("how many campaigns has Dru been on" → count that artist's paid_campaign features),
- counts by specific outlet ("how many Daily Rush features" → count features whose placement is "Daily Rush", across the named artist or the whole roster),
- roster totals ("how many paid campaigns total"),
- gaps ("which artists haven't been on a paid campaign" → artists with 0 of that type, including never-featured ones),
- leaders ("who's gotten the most marketing").

Rules:
- Answer directly and concisely. Output goes into Slack.
- A feature type with no entries for an artist counts as ZERO — never invent features.
- For filter/list questions, name EVERY matching artist by *name* with their @handle. If none match, say so plainly.
- Use relative dates ("3 days ago", "last week") computed from Today — not ISO strings.
- Use Slack mrkdwn: *single asterisks* for bold, "• " for bullet points. Never use tables or **double asterisks**.
- Keep it tight: a short lead sentence, then a bullet list when naming multiple artists.
"""
    try:
        anthropic_client = anthropic.Anthropic()
        msg = anthropic_client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        return f"(marketing query failed: {e})"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def _route(text: str, user_id: str, channel: str, thread_ts: str, files: list[dict] | None, reply, client=None) -> None:
    if not _is_authorized(user_id):
        log_event("slack_rejected", {"user_id": user_id, "channel": channel, "text": text[:200]})
        # Stay silent — don't tip off unauthorized users that the bot is here.
        return

    log_event("slack_in", {
        "user_id": user_id, "channel": channel, "text": text, "has_files": bool(files),
    })

    # Record this user turn, and wrap `reply` so every bot turn is recorded too.
    # This gives the classifier short-term memory of the dialogue.
    _remember(channel, "user", text)
    _base_reply = reply

    def reply(msg):  # noqa: F811 — intentional wrap to log the bot turn
        _remember(channel, "bot", msg)
        _base_reply(msg)

    # If this conversation had a pending file waiting for clarification, prefer file
    # flow. Keyed by channel so it survives the DM back-and-forth (no threads in DMs).
    pending = _pop_pending_file(channel)

    intent = classify_intent(text, recent_context=_recent_context(channel))
    log_event("slack_decision", {"user_id": user_id, "intent": intent})

    # ---- Permission gate (RBAC) -------------------------------------------------
    # Reads are open; writes are gated by the user's roles. File flows are gated on
    # the best-known category here, and re-checked precisely at write time.
    roles = _user_roles(user_id)
    is_file_flow = bool(files) or bool(pending) or intent.get("intent") == "file_upload"
    if is_file_flow:
        gate_action = "file_upload"
        gate_target = (
            intent.get("target_field")
            or (pending.get("target_field") if pending else None)
            or _match_target_field(text)
        )
    else:
        gate_action, gate_target = intent["intent"], None

    allowed, reason = _check_permission(roles, gate_action, gate_target)
    if not allowed:
        log_event("slack_rejected", {
            "user_id": user_id, "channel": channel,
            "action": gate_action, "target_field": gate_target,
            "roles": sorted(roles), "reason": reason, "text": text[:200],
        })
        reply(reason)
        return

    try:
        if pending:
            # Re-route as file_upload with the stashed file(s). The follow-up may
            # supply the missing artist, the missing category, or both — merge the
            # new message with what we already remembered (artist_id + category).
            target_field = (
                intent.get("target_field")
                or pending.get("target_field")
                or _match_target_field(text)
            )
            artist_ref = intent.get("artist_reference") or pending.get("artist_id")
            # If the message is just a category word, don't mistake it for an artist.
            if not artist_ref and _match_target_field(text) is None:
                artist_ref = text.strip()
            intent["intent"] = "file_upload"
            intent["target_field"] = target_field
            intent["artist_reference"] = artist_ref
            handle_file_upload(intent, user_id, channel, pending["files"], thread_ts, reply)
            return

        if files:
            intent["intent"] = "file_upload"
            handle_file_upload(intent, user_id, channel, files, thread_ts, reply)
            return

        # Trust the classifier's intent — it returns `clarify` itself when genuinely
        # unsure (and now has conversation context to lean on). Each handler asks a
        # specific follow-up when it's missing an artist or a field, so there's no
        # need for a blunt confidence gate that bails to a generic menu.
        intent_name = intent["intent"]

        if intent_name == "log_conversation":
            handle_log_conversation(intent, user_id, channel, reply, client=client)
        elif intent_name == "query_artist":
            handle_query_artist(intent, user_id, channel, reply, client=client)
        elif intent_name == "query_roster":
            handle_query_roster(intent, user_id, channel, reply)
        elif intent_name == "log_marketing":
            handle_log_marketing(intent, user_id, channel, reply, client=client)
        elif intent_name == "query_marketing":
            handle_query_marketing(intent, user_id, channel, reply)
        elif intent_name == "get_asset_link":
            handle_get_asset_link(intent, user_id, channel, reply)
        elif intent_name == "get_sheet_link":
            handle_get_sheet_link(intent, user_id, channel, reply)
        elif intent_name == "add_artist":
            handle_add_artist(intent, user_id, channel, reply)
        elif intent_name == "update_field":
            handle_update_field(intent, user_id, channel, reply)
        elif intent_name == "assign_role":
            handle_assign_role(intent, user_id, channel, text, reply, client=client)
        elif intent_name == "query_team":
            handle_query_team(intent, user_id, channel, reply, client=client)
        elif intent_name == "file_upload":
            # File-upload intent but no file present — likely the user is captioning a
            # forthcoming upload. Ask them to attach, and offer the retrieve path.
            reply("Attach the file and I'll add it. (If you wanted the existing folder link instead, say e.g. \"get the link to their press shots\".)")
        elif intent_name == "unknown":
            reply(
                "I help track artist info and conversations. Try:\n"
                "• `logged a call with Sam about cover art`\n"
                "• `where are we at with Sam?`\n"
                "• `add new artist Sam Harper, instagram samharper`\n"
                "• drop a file with a caption like `press pic for Sam`"
            )
        else:
            handle_clarify(intent, user_id, reply)
    except Exception as e:
        log_event("slack_error", {
            "user_id": user_id, "channel": channel,
            "error": str(e), "trace": traceback.format_exc()[:2000],
        })
        reply(f"Hit an error handling that — {e}")


# ---------------------------------------------------------------------------
# Slack Bolt wiring
# ---------------------------------------------------------------------------

def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the leading <@UBOT> mention from text."""
    if not bot_user_id:
        return text
    import re
    return re.sub(rf"<@{bot_user_id}>\s*", "", text).strip()


def main() -> None:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not bot_token or not app_token:
        print("ERROR: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in server/.env", file=sys.stderr)
        sys.exit(1)

    if _allow_all_users():
        logger.info("SLACK_AUTHORIZED_USER_IDS='*' — any user in the workspace may use the bot.")
        if not os.getenv("SLACK_WORKSPACE_ID", "").strip():
            print("WARNING: allow-all ('*') is set but SLACK_WORKSPACE_ID is empty — set it to pin the workspace.", file=sys.stderr)
    elif not _authorized_user_ids():
        print("WARNING: SLACK_AUTHORIZED_USER_IDS is empty — agent will respond to no one (fail closed).", file=sys.stderr)

    try:
        # Validate the sheet is provisioned. This will raise if not.
        artist_crm._get_sheet_id()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    app = App(token=bot_token)

    # Identify the bot's own user_id so we can ignore its own messages
    auth = app.client.auth_test()
    bot_user_id = auth.get("user_id")
    workspace = auth.get("team_id")
    logger.info(f"Bot user_id={bot_user_id} workspace={workspace}")

    expected_workspace = os.getenv("SLACK_WORKSPACE_ID", "").strip()
    if expected_workspace and workspace != expected_workspace:
        print(f"ERROR: bot is in workspace {workspace}, expected {expected_workspace}", file=sys.stderr)
        sys.exit(1)

    @app.event("app_mention")
    def on_mention(event, say, client):
        # Channel @mentions: reply in-thread to keep channels clean.
        user_id = event.get("user", "")
        if user_id == bot_user_id:
            return
        if _already_handled(_event_key(event)):
            logger.info(f"Skipping duplicate app_mention {_event_key(event)}")
            return
        text = _strip_bot_mention(event.get("text", ""), bot_user_id)
        channel = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        files = event.get("files") or None

        def reply(msg):
            say(text=_to_slack_mrkdwn(msg), thread_ts=thread_ts)

        _route(text, user_id, channel, thread_ts, files, reply, client=client)

    @app.event("message")
    def on_message(event, say, client):
        # Only handle DMs here; channel messages flow through app_mention.
        if event.get("channel_type") != "im":
            return
        # Ignore the bot's own messages. Reject system subtypes (edits, deletes,
        # joins, etc.) but ALLOW "file_share" — that's how a file upload with an
        # optional caption arrives in a DM.
        if event.get("bot_id"):
            return
        subtype = event.get("subtype")
        if subtype and subtype != "file_share":
            return
        user_id = event.get("user", "")
        if not user_id or user_id == bot_user_id:
            return
        if _already_handled(_event_key(event)):
            logger.info(f"Skipping duplicate message {_event_key(event)}")
            return
        text = event.get("text", "") or ""
        channel = event.get("channel", "")
        # `thread_ts` is kept for the pending-file stash key, but DMs reply as
        # top-level messages (no threading) for a natural back-and-forth.
        thread_ts = event.get("thread_ts") or event.get("ts")
        files = event.get("files") or None

        def reply(msg):
            say(text=_to_slack_mrkdwn(msg))

        _route(text, user_id, channel, thread_ts, files, reply, client=client)

    @app.event("file_shared")
    def on_file_shared(event, client):
        # file_shared fires for every file. The actual handling happens via
        # the parent message event (app_mention / message.im) which carries
        # the file in event["files"]. We ack here purely to keep Bolt happy.
        log_event("slack_file_event", {
            "file_id": event.get("file_id"),
            "user_id": event.get("user_id"),
        })

    logger.info("Starting Slack agent (Socket Mode)…")
    handler = SocketModeHandler(app, app_token)
    handler.start()


if __name__ == "__main__":
    main()
