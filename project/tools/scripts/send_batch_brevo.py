#!/usr/bin/env python3
"""Brevo BATCH sender — hits POST /v3/smtp/email with messageVersions.

One HTTP call ships up to 1000 personalized email versions. We chunk drafts
into groups of 1000 and fire each chunk as a single request. No per-email
pacing, no approval gate — this is the auto-fire path.

Input:  <run-dir>/emails-drafted.json   (one JSON per line)
Output: <run-dir>/emails-sent.jsonl     (one JSON per line, per recipient)
        <run-dir>/send-log.txt          (human-readable)

Each draft must contain:
    lead_id, lead_slug, to_email, to_name, subject, body_text, body_html
Optional: tags, vertical, country_code, score, signal_used.

Brevo docs: https://developers.brevo.com/reference/sendtransacemail
The messageVersions field lets each recipient have its own subject/body —
that's what enables 1000 personalized emails per request.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

BREVO_BATCH_MAX = 1000  # hard ceiling from Brevo for messageVersions per request
BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_suppression(project_root: Path) -> tuple[set[str], set[str]]:
    """Send-time suppression net (the LAST line of defense; upstream dedup can
    miss when the enriched contact email differs from anything previously seen).

    Returns (suppressed_emails, suppressed_domains):
      * every address ever logged in sent-log.md  -> never email twice
      * every address/domain in bounce-list.md    -> hard bounces, Brevo blocks,
        spam complaints, unsubscribes are dead-letter forever
    """
    emails: set[str] = set()
    domains: set[str] = set()
    sent_log = project_root / "vault" / "lead-outreach" / "sent-log.md"
    if sent_log.exists():
        for line in sent_log.read_text().splitlines():
            for m in EMAIL_RE.finditer(line):
                e = m.group(0).lower()
                if "smtp-relay" in e or "mailin.fr" in e:
                    continue
                emails.add(e)
    bounce_list = project_root / "vault" / "lead-outreach" / "bounce-list.md"
    if bounce_list.exists():
        for line in bounce_list.read_text().splitlines():
            s = line.strip()
            if not s or s.startswith(("#", ">", "-", "`")):
                continue
            found = False
            for m in EMAIL_RE.finditer(s):
                emails.add(m.group(0).lower())
                found = True
            if not found:
                # bare-domain entry: `<date> | example.com | reason | run`
                parts = [p.strip().lower() for p in s.split("|")]
                if len(parts) >= 2 and "." in parts[1] and " " not in parts[1]:
                    domains.add(parts[1])
    return emails, domains


def load_env(path: Path) -> dict[str, str]:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
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


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def build_version(draft: dict, sender_address: str = "") -> dict:
    # Every email renders through the shared professional shell (signature +
    # footer component). Falls back to the drafter's bare body_html only if the
    # template module is unavailable, so a send is never blocked on it.
    try:
        from email_template import render_html
        html = render_html(draft["body_text"], address=sender_address)
    except Exception:
        html = draft["body_html"]
    return {
        "to": [{"email": draft["to_email"].strip().lower(), "name": draft.get("to_name") or draft["to_email"]}],
        "subject": draft["subject"][:200],
        "textContent": draft["body_text"],
        "htmlContent": html,
        "headers": {
            "X-Mailin-Custom": f"lead_id={draft['lead_id']}",
        },
    }


def post_batch(api_key: str, sender: dict, reply_to: dict, versions: list[dict], tags: list[str], dry_run: bool) -> tuple[bool, str, dict]:
    if dry_run:
        return True, "", {
            "messageIds": [f"DRY-{int(time.time()*1000)}-{i}" for i in range(len(versions))]
        }
    payload = {
        "sender": sender,
        "replyTo": reply_to,
        # The top-level subject/content acts as the default; messageVersions overrides per recipient.
        "subject": versions[0]["subject"],
        "textContent": versions[0]["textContent"],
        "htmlContent": versions[0]["htmlContent"],
        "tags": tags,
        "headers": {
            "List-Unsubscribe": f"<mailto:{sender['email']}?subject=remove>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
        "messageVersions": versions,
    }
    req = request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return True, "", json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        return False, f"HTTP {exc.code}: {body[:500]}", {}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--send", action="store_true", help="actually ship (default is dry-run)")
    p.add_argument("--cap", type=int, default=int(os.environ.get("MAX_EMAILS_PER_RUN", "1000")))
    p.add_argument("--batch-size", type=int, default=BREVO_BATCH_MAX)
    args = p.parse_args()

    if args.batch_size > BREVO_BATCH_MAX:
        print(f"WARN: batch-size {args.batch_size} > Brevo max {BREVO_BATCH_MAX}; clamping", file=sys.stderr)
        args.batch_size = BREVO_BATCH_MAX

    run_dir = Path(args.run_dir)
    project_root = run_dir.parent.parent
    drafts_path = run_dir / "emails-drafted.json"
    out_path = run_dir / "emails-sent.jsonl"
    log_path = run_dir / "send-log.txt"

    env = {**load_env(project_root / ".env"), **os.environ}
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    sender_name = env.get("BREVO_SENDER_NAME", "OSG")
    reply_to_email = env.get("BREVO_REPLY_TO", "") or sender_email
    reply_to_name = sender_name

    if args.send and (not api_key or not sender_email):
        print("ABORT: missing BREVO_MCP_TOKEN or BREVO_SENDER_EMAIL", file=sys.stderr)
        sys.exit(1)

    sender = {"email": sender_email, "name": sender_name}
    reply_to = {"email": reply_to_email, "name": reply_to_name}

    drafts = [json.loads(l) for l in drafts_path.read_text().splitlines() if l.strip()]

    sup_emails, sup_domains = load_suppression(project_root)
    seen, unique = set(), []
    suppressed = 0
    for d in drafts:
        e = d["to_email"].strip().lower()
        dom = e.split("@", 1)[1] if "@" in e else ""
        if e in sup_emails or dom in sup_domains:
            suppressed += 1
            continue
        if e in seen:
            continue
        seen.add(e)
        unique.append(d)
    if suppressed:
        print(f"SUPPRESSED {suppressed} draft(s): already in sent-log or bounce-list "
              f"(never re-contact / dead-letter).")
    drafts = unique[: args.cap]

    if not drafts:
        print("Nothing to send.")
        return

    mode = "LIVE" if args.send else "DRY-RUN"
    print(f"[{mode}] Sending {len(drafts)} emails via Brevo batch endpoint "
          f"({(len(drafts) + args.batch_size - 1) // args.batch_size} request(s) of up to {args.batch_size}).")

    rows: list[dict] = []
    with out_path.open("w") as out_f, log_path.open("w") as log_f:
        sender_address = env.get("BREVO_SENDER_ADDRESS", "")  # CAN-SPAM physical address (set in .env)
        for chunk_idx, chunk in enumerate(chunked(drafts, args.batch_size), start=1):
            versions = [build_version(d, sender_address) for d in chunk]
            run_tag = run_dir.name
            common_tags = list({t for d in chunk for t in d.get("tags", [])} | {"cold-outreach", run_tag})

            ok, err, resp = post_batch(api_key, sender, reply_to, versions, common_tags, dry_run=not args.send)
            sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ok else ""

            msg_ids = resp.get("messageIds") or []
            single_id = resp.get("messageId") or ""

            line_summary = (f"[{sent_at or 'NOW'}] batch={chunk_idx} size={len(chunk)} "
                            f"result={'sent' if ok else 'failed'} {err or ''}").rstrip()
            print(line_summary)
            log_f.write(line_summary + "\n"); log_f.flush()

            for i, draft in enumerate(chunk):
                row = {
                    "lead_id": draft["lead_id"],
                    "lead_slug": draft.get("lead_slug", ""),
                    "to_email": draft["to_email"],
                    "to_name": draft.get("to_name", ""),
                    "subject": draft["subject"],
                    "result": "sent" if ok else "failed",
                    "message_id": (msg_ids[i] if i < len(msg_ids) else single_id),
                    "sent_at": sent_at,
                    "dry_run": not args.send,
                    "batch_index": chunk_idx,
                    "vertical": draft.get("vertical"),
                    "country_code": draft.get("country_code"),
                    "signal_used": draft.get("signal_used"),
                    "score": draft.get("score"),
                }
                if err:
                    row["error"] = err
                out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows.append(row)

            out_f.flush()

    sent = sum(1 for r in rows if r["result"] == "sent")
    failed = sum(1 for r in rows if r["result"] == "failed")
    print(f"\nDONE: attempted={len(rows)} sent={sent} failed={failed} mode={mode}")


if __name__ == "__main__":
    main()
