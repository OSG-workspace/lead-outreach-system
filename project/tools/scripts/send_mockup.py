#!/usr/bin/env python3
"""Send ONE mockup of the professional Automate email template via Brevo.

Renders a realistic sample outreach body through email_template.render_html()
and ships it to a single recipient so a human can eyeball the design in a real
inbox. Reuses the same Brevo endpoint + .env credentials as send_batch_brevo.py.

Usage:
    python3 tools/scripts/send_mockup.py --to replies@yourdomain.com [--dry-run]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_template import render_html  # noqa: E402
from send_batch_brevo import load_env, decode_brevo_key, BREVO_ENDPOINT  # noqa: E402

SAMPLE_BODY = (
    "Hello Mr. Freeman,\n\n"
    "Your contact form on fusonlaw.com says \"We will get back to you as soon "
    "as possible,\" which means someone on your team is reading, sorting, and "
    "replying to each incoming inquiry by hand before a consult is ever booked. "
    "For a criminal and family law practice that runs 24/7, that is a lot of "
    "quiet hours spent on follow-up.\n\n"
    "We set up an assistant that reads those intake emails as they come in, "
    "sorts real cases from the noise, books the consult straight into your "
    "calendar, then sends reminders. Not a chatbot. A quiet assistant that "
    "handles the repetitive part so your staff does not.\n\n"
    "Would Tuesday or Thursday afternoon work for a quick 15-minute call?\n\n"
    "David Geha\nAutomate, automatelb.com"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    project_root = Path(__file__).resolve().parents[2]
    env = load_env(project_root / ".env")
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    # Cold 1:1 mail: a person in the From line outperforms a brand. The prod
    # BREVO_SENDER_NAME is lowercase "automate"; the mockup shows the better choice.
    sender_name = "David Geha"
    reply_to = env.get("BREVO_REPLY_TO", "") or sender_email

    if not api_key or not sender_email:
        sys.exit("ABORT: missing BREVO_MCP_TOKEN or BREVO_SENDER_EMAIL in .env")

    html = render_html(SAMPLE_BODY)
    payload = {
        "sender": {"email": sender_email, "name": sender_name},
        "replyTo": {"email": reply_to, "name": sender_name},
        "to": [{"email": args.to}],
        "subject": "[MOCKUP] the intake forms piling up",
        "textContent": SAMPLE_BODY,
        "htmlContent": html,
        "headers": {
            "List-Unsubscribe": f"<mailto:{sender_email}?subject=unsubscribe>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }

    print(f"Sender : {sender_name} <{sender_email}>")
    print(f"To     : {args.to}")
    print(f"HTML   : {len(html)} bytes")
    if args.dry_run:
        print("DRY-RUN: not sending.")
        return

    req = request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json",
                 "api-key": api_key},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            print("SENT OK:", body or "(empty 2xx)")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        sys.exit(f"SEND FAILED HTTP {exc.code}: {detail[:600]}")
    except Exception as exc:
        sys.exit(f"SEND FAILED: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
