#!/usr/bin/env python3
"""Minimal Brevo sender — sends drafted emails directly with pacing, logs results.

Bypasses funnel_lib's gate (drafts already filtered/deduped during extraction).
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists(): return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
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


def send_one(api_key: str, sender: dict, draft: dict, run_dir_name: str, dry_run: bool) -> tuple[bool, str, dict]:
    if dry_run:
        return True, "", {"messageId": f"DRY-{int(time.time()*1000)}"}
    payload = {
        "sender": sender,
        "to": [{"email": draft["to_email"].strip().lower(), "name": draft["to_name"]}],
        "replyTo": sender,
        "subject": draft["subject"][:200],
        "textContent": draft["body_text"],
        "htmlContent": draft["body_html"],
        "tags": draft.get("tags", []),
        "headers": {
            "X-Mailin-Custom": f"lead_id={draft['lead_id']};run={run_dir_name}",
            "List-Unsubscribe": f"<mailto:{sender['email']}?subject=remove>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    req = request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return True, "", json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        return False, f"HTTP {exc.code}: {body[:300]}", {}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--send", action="store_true")
    p.add_argument("--pace", type=int, default=30)
    p.add_argument("--cap", type=int, default=100)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    project_root = run_dir.parent.parent
    drafts_path = run_dir / "emails-drafted.json"
    out_path = run_dir / "emails-sent.jsonl"
    log_path = run_dir / "send-log.txt"

    env = {**load_env(project_root / ".env"), **os.environ}
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    sender_name = env.get("BREVO_SENDER_NAME", "Automate")
    if not args.send:
        print("DRY-RUN mode (pass --send to actually ship)")
    elif not api_key or not sender_email:
        print("ABORT: missing BREVO_MCP_TOKEN or BREVO_SENDER_EMAIL", file=sys.stderr)
        sys.exit(1)
    sender = {"email": sender_email, "name": sender_name}

    drafts = [json.loads(l) for l in drafts_path.read_text().splitlines() if l.strip()]
    # Drop duplicates by email (some leads may share inboxes)
    seen, unique = set(), []
    for d in drafts:
        e = d["to_email"].lower()
        if e in seen: continue
        seen.add(e)
        unique.append(d)
    drafts = unique[: args.cap]
    print(f"Sending {len(drafts)} emails, pace={args.pace}s, dry_run={not args.send}")

    results = []
    with out_path.open("w") as out_f, log_path.open("w") as log_f:
        for i, draft in enumerate(drafts):
            ok, err, resp = send_one(api_key, sender, draft, run_dir.name, dry_run=not args.send)
            sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ok else ""
            row = {
                "lead_id": draft["lead_id"],
                "lead_slug": draft["lead_slug"],
                "to_email": draft["to_email"],
                "to_name": draft["to_name"],
                "subject": draft["subject"],
                "result": "sent" if ok else "failed",
                "message_id": resp.get("messageId", ""),
                "sent_at": sent_at,
                "dry_run": not args.send,
                "vertical": draft.get("vertical"),
                "country_code": draft.get("country_code"),
            }
            if err: row["error"] = err
            out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            out_f.flush()
            line = f"[{sent_at or 'NOW'}] {row['result']:6} {i+1:3}/{len(drafts)} {draft['to_email']:45} {err or resp.get('messageId','')}"
            print(line)
            log_f.write(line + "\n"); log_f.flush()
            results.append(row)
            if i + 1 < len(drafts) and args.send:
                time.sleep(args.pace)

    sent = sum(1 for r in results if r["result"] == "sent")
    failed = sum(1 for r in results if r["result"] == "failed")
    print(f"\nDONE: attempted={len(results)} sent={sent} failed={failed} dry_run={not args.send}")


if __name__ == "__main__":
    main()
