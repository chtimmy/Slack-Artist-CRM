#!/usr/bin/env python3
"""
Umbra — Audit Log
Append-only JSONL log of every signal, prompt, decision, and order.
One file per trading day in .tmp/audit/YYYY-MM-DD.jsonl.

Usage as a module:
    from tools.audit_log import log_event, read_events

    log_event("signal", {"ticker": "NVDA", "type": "breakout", "price": 485.0})
    log_event("decision", {"ticker": "NVDA", "action": "BUY", "qty": 8, ...})

    events = read_events("2026-05-08", event_type="decision")

CLI:
    python3 tools/audit_log.py --date 2026-05-08
    python3 tools/audit_log.py --date 2026-05-08 --type decision
    python3 tools/audit_log.py --tail 20
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
AUDIT_DIR = os.path.join(PROJECT_ROOT, ".tmp", "audit")

_lock = threading.Lock()


def _ensure_dir() -> None:
    os.makedirs(AUDIT_DIR, exist_ok=True)


def _path_for(date_str: str) -> str:
    return os.path.join(AUDIT_DIR, f"{date_str}.jsonl")


def log_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append a structured event to today's audit log. Thread-safe.

    Returns the full event dict including auto-added fields (ts, type).
    """
    _ensure_dir()
    now = datetime.now(timezone.utc)
    event = {
        "ts": now.isoformat(),
        "ts_unix": time.time(),
        **data,
        "type": event_type,  # set AFTER spread — caller can never clobber the event type
    }
    path = _path_for(now.strftime("%Y-%m-%d"))
    line = json.dumps(event, default=str, ensure_ascii=False)
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return event


def read_events(
    date_str: str | None = None,
    event_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read events from a given day's log. Defaults to today.

    Returns events in chronological order.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = _path_for(date_str)
    if not os.path.exists(path):
        return []
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_type and ev.get("type") != event_type:
                continue
            events.append(ev)
    if limit:
        events = events[-limit:]
    return events


def list_dates() -> list[str]:
    """All dates that have an audit log, oldest first."""
    _ensure_dir()
    files = [f for f in os.listdir(AUDIT_DIR) if f.endswith(".jsonl")]
    return sorted(f.replace(".jsonl", "") for f in files)


def read_range(start_date: str, end_date: str, event_type: str | None = None) -> list[dict[str, Any]]:
    """Read events across a date range (inclusive). Dates as YYYY-MM-DD."""
    out = []
    for d in list_dates():
        if start_date <= d <= end_date:
            out.extend(read_events(d, event_type=event_type))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_event_line(ev: dict[str, Any]) -> str:
    ts = ev.get("ts", "")[:19].replace("T", " ")
    typ = ev.get("type", "?")
    rest = {k: v for k, v in ev.items() if k not in ("ts", "ts_unix", "type")}
    return f"{ts}  [{typ:<10}]  {json.dumps(rest, default=str, ensure_ascii=False)}"


def main():
    ap = argparse.ArgumentParser(description="Read the trading agent audit log")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--type", default=None, help="Filter by event type")
    ap.add_argument("--tail", type=int, default=None, help="Show only last N events")
    ap.add_argument("--list-dates", action="store_true", help="List all dates with logs")
    ap.add_argument("--json", action="store_true", help="Output raw JSONL instead of formatted")
    args = ap.parse_args()

    if args.list_dates:
        for d in list_dates():
            print(d)
        return

    events = read_events(args.date, event_type=args.type, limit=args.tail)
    for ev in events:
        if args.json:
            print(json.dumps(ev, default=str, ensure_ascii=False))
        else:
            print(_format_event_line(ev))

    if not args.json:
        print(f"\n{len(events)} event(s) in {args.date or 'today'}", file=sys.stderr)


if __name__ == "__main__":
    main()
