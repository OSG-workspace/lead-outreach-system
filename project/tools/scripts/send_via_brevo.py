#!/usr/bin/env python3
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

from funnel_lib import build_funnel_config, gate_drafts, load_jsonl


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def decode_brevo_key(token: str) -> str:
    if token.startswith("xkeysib-") and not token.endswith("=="):
        return token
    try:
        body = token[len("xkeysib-"):] if token.startswith("xkeysib-") else token
        return json.loads(base64.b64decode(body + "==").decode())["api_key"]
    except Exception:
        return token


def post_brevo(api_key: str, payload: dict, dry_run: bool) -> tuple[bool, str, dict]:
    if dry_run:
        return True, "", {"messageId": f"DRY-RUN-{int(time.time())}"}
    req = request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            return True, "", json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        return False, f"HTTP {exc.code}: {body[:300]}", {}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Gated Brevo transactional sender. Defaults to dry-run.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--drafts", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="Actually call Brevo after gates pass.")
    parser.add_argument("--pace-seconds", type=int, default=90)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    drafts_path = Path(args.drafts) if args.drafts else run_dir / "emails-drafted.json"
    output_path = Path(args.output) if args.output else run_dir / "emails-sent.json"
    log_path = run_dir / "send-log.txt"
    dry_run = not args.send or args.dry_run

    drafts = load_jsonl(drafts_path)
    eligible, gate_report = gate_drafts(drafts, role_threshold=cfg["role_inbox_threshold"])
    (run_dir / "send-eligibility-report.json").write_text(json.dumps(gate_report, indent=2, ensure_ascii=False))
    if gate_report["dropped"]:
        print(json.dumps({"refused": gate_report, "reason": "send_gate_failed"}, indent=2, ensure_ascii=False))
        if not eligible:
            sys.exit(1)

    env = {**load_env(Path(".env")), **os.environ}
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    sender_name = env.get("BREVO_SENDER_NAME", "Automate")
    reply_to = env.get("BREVO_REPLY_TO", sender_email)
    if not dry_run and (not api_key or not sender_email):
        print("ABORT: missing BREVO_MCP_TOKEN or BREVO_SENDER_EMAIL", file=sys.stderr)
        sys.exit(1)

    results: list[dict] = []
    for idx, draft in enumerate(eligible):
        to_email = (draft.get("to_email") or "").strip().lower()
        payload = {
            "sender": {"email": sender_email, "name": sender_name},
            "to": [{"email": to_email, "name": draft.get("to_name") or to_email.split("@")[0]}],
            "replyTo": {"email": reply_to, "name": sender_name},
            "subject": (draft.get("subject") or "")[:200],
            "textContent": draft.get("body_text") or "",
            "htmlContent": draft.get("body_html") or "",
            "tags": draft.get("tags") or ["cold-outreach", run_dir.name],
            "headers": {
                "X-Mailin-Custom": f"lead_id={draft.get('lead_id', '')};run={run_dir.name}",
                "List-Unsubscribe": f"<mailto:{sender_email}?subject=remove>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            },
        }
        ok, err, response = post_brevo(api_key, payload, dry_run=dry_run)
        sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = {
            "lead_id": draft.get("lead_id"),
            "result": "sent" if ok else "failed",
            "message_id": response.get("messageId", ""),
            "sent_at": sent_at if ok else "",
            "dry_run": dry_run,
        }
        if err:
            result["error"] = err
        results.append(result)
        with log_path.open("a") as log:
            log.write(f"[{sent_at}] {result['result']} lead_id={draft.get('lead_id')} dry_run={dry_run}\n")
        if not dry_run and idx + 1 < len(eligible):
            time.sleep(args.pace_seconds)

    output_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results))
    print(json.dumps({"attempted": len(results), "sent": sum(1 for row in results if row["result"] == "sent"), "dry_run": dry_run}, indent=2))


if __name__ == "__main__":
    main()
