#!/usr/bin/env python3
"""Sync Brevo delivery-failure events into vault/lead-outreach/bounce-list.md.

The bounce-list is the dead-letter suppression file (checked at send time by
send_batch_brevo.load_suppression). Before this script existed nothing ever
wrote to it — 1,193 hard bounces accumulated in Brevo with no local record,
and the pipeline kept drafting to addresses Brevo already knew were dead.

Pulls these transactional event types (Brevo GET /v3/smtp/statistics/events):
    hardBounces, blocked, spam, invalid, unsubscribed

Appends one line per NEW address:  `<date> | <email> | <event> | <subject-60>`
Idempotent: an address already anywhere in bounce-list.md is never re-added.

Run standalone or via run_fire.py (which runs it before every live send).
Exit codes: 0 ok · 1 config error · 2 Brevo API unreachable (kill-on-fallback:
a run must not send while the suppression list is stale).
"""
from __future__ import annotations
import argparse
import base64
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib import error, parse, request

PROJECT = Path(__file__).resolve().parents[2]
BOUNCE_LIST = PROJECT / "vault" / "lead-outreach" / "bounce-list.md"
EVENTS_URL = "https://api.brevo.com/v3/smtp/statistics/events"
EVENT_TYPES = ["hardBounces", "blocked", "spam", "invalid", "unsubscribed"]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PAGE = 2500
MAX_PAGES = 8  # 20k events per type is far beyond Brevo's retention window


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def decode_brevo_key(token: str) -> str:
    if token.startswith("xkeysib-") and not token.endswith("=="):
        return token
    try:
        body = token[len("xkeysib-"):] if token.startswith("xkeysib-") else token
        return json.loads(base64.b64decode(body + "==").decode())["api_key"]
    except Exception:
        return token


def fetch_events(api_key: str, event: str) -> list[dict]:
    out: list[dict] = []
    for page in range(MAX_PAGES):
        qs = parse.urlencode({"limit": PAGE, "offset": page * PAGE,
                              "event": event, "sort": "desc"})
        req = request.Request(f"{EVENTS_URL}?{qs}",
                              headers={"accept": "application/json", "api-key": api_key})
        with request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode() or "{}")
        events = data.get("events") or []
        out.extend(events)
        if len(events) < PAGE:
            break
    return out


def existing_addresses(bounce_list: Path) -> set[str]:
    if not bounce_list.exists():
        return set()
    return {m.group(0).lower()
            for line in bounce_list.read_text().splitlines()
            for m in EMAIL_RE.finditer(line)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bounce-list", default=str(BOUNCE_LIST))
    args = ap.parse_args()
    bounce_list = Path(args.bounce_list)

    env = load_env(PROJECT / ".env")
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    if not api_key:
        print("ABORT: BREVO_MCP_TOKEN missing from project/.env", file=sys.stderr)
        sys.exit(1)

    known = existing_addresses(bounce_list)
    new_rows: list[str] = []
    per_type: dict[str, int] = {}
    for ev in EVENT_TYPES:
        try:
            events = fetch_events(api_key, ev)
        except (error.URLError, error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"ABORT: Brevo events API unreachable for {ev}: {exc}", file=sys.stderr)
            sys.exit(2)
        added = 0
        for e in events:
            addr = (e.get("email") or "").strip().lower()
            if not addr or addr in known:
                continue
            when = (e.get("date") or date.today().isoformat())[:10]
            subj = (e.get("subject") or "").replace("|", "/")[:60]
            new_rows.append(f"{when} | {addr} | {ev} | {subj}")
            known.add(addr)
            added += 1
        per_type[ev] = added

    if new_rows:
        if not bounce_list.exists():
            bounce_list.parent.mkdir(parents=True, exist_ok=True)
            bounce_list.write_text("# Bounce List\n\n## Entries\n")
        with bounce_list.open("a") as f:
            for r in sorted(new_rows):
                f.write(r + "\n")
    print(f"Bounce sync: {len(new_rows)} new suppressed addresses "
          f"({', '.join(f'{k}={v}' for k, v in per_type.items())}). "
          f"Total suppressed: {len(known)}.")


if __name__ == "__main__":
    main()
