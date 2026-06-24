#!/usr/bin/env python3
"""Stage 8: Append sent emails to vault/lead-outreach/sent-log.md.

Idempotent: skips message_ids already present in the log.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

MSG_ID_RE = re.compile(r"<[^>]+@[^>]+>")
# WhatsApp phone tokens are written as `wa:<digits>` so they never collide with
# the email regex used for email-channel dedup.
WA_PHONE_RE = re.compile(r"wa:(\d{6,15})")


def load_existing_message_ids(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    ids: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in MSG_ID_RE.finditer(line):
            ids.add(m.group(0))
    return ids


def load_existing_wa_phones(sent_log: Path) -> set[str]:
    """Phones (digits-only) already recorded for the WhatsApp channel."""
    if not sent_log.exists():
        return set()
    phones: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in WA_PHONE_RE.finditer(line):
            phones.add(m.group(1))
    return phones


def format_row(row: dict, run_slug: str) -> str:
    date = (row.get("sent_at") or "")[:10]
    return (
        f"{date} | {row['to_email']} | [[{row['lead_slug']}]] "
        f"| step 1 | {run_slug} | {row['message_id']}"
    )


def append_rows(sent_jsonl: Path, sent_log: Path, run_slug: str) -> int:
    if not sent_jsonl.exists():
        return 0
    existing_ids = load_existing_message_ids(sent_log)
    new_rows: list[str] = []
    for line in sent_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("result") != "sent" or row.get("dry_run"):
            continue
        msg_id = row.get("message_id", "")
        if not msg_id or msg_id in existing_ids:
            continue
        new_rows.append(format_row(row, run_slug))
        existing_ids.add(msg_id)

    if not new_rows:
        return 0

    sent_log.parent.mkdir(parents=True, exist_ok=True)
    if not sent_log.exists():
        sent_log.write_text(
            "---\nname: sent-log\ndescription: Permanent dedup log of all outreach emails sent\n"
            "metadata:\n  type: project\n---\n\n# Sent Log\n\n"
            "Master dedup record. Format: `date | email | lead_slug | step | run_slug | brevo_message_id`\n\n"
        )
    with sent_log.open("a") as f:
        for row in new_rows:
            f.write(row + "\n")
    return len(new_rows)


def load_contact_index(run_dir: Path) -> dict:
    """lead_id -> {email, domain, slug} from leads-with-contact.json so a
    WhatsApp send can be logged with the business email/domain. That lets the
    SAME email/domain dedup (extract_leads.py, merge_candidates.py) block this
    business on EITHER channel in future runs, not just WhatsApp."""
    idx: dict = {}
    src = run_dir / "leads-with-contact.json"
    if not src.exists():
        return idx
    for line in src.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        lead_id = row.get("lead_id")
        if not lead_id:
            continue
        email = (row.get("to_email") or row.get("contact_email") or "").strip().lower()
        website = (row.get("website") or "").strip().lower()
        domain = website.split("//", 1)[-1].split("/", 1)[0] if website else ""
        if domain.startswith("www."):
            domain = domain[4:]
        idx[lead_id] = {
            "email": email,
            "domain": domain,
            "slug": row.get("lead_slug", ""),
        }
    return idx


def format_wa_row(send: dict, contact: dict, run_slug: str) -> str:
    date = (send.get("ts") or "")[:10]
    phone = (send.get("to_jid") or "").split("@", 1)[0] or (send.get("to_phone") or "")
    phone = re.sub(r"\D", "", phone)
    # Real business email when known so exact-email dedup catches re-sourcing;
    # "-" placeholder (no @) when the lead was phone-only.
    email = contact.get("email") or "-"
    slug = contact.get("slug") or send.get("lead_slug") or send.get("lead_id", "")
    # Always carry the business website domain as a `dom@<domain>` token so
    # domain-level dedup (merge_candidates.py) blocks this business on a future
    # EMAIL run even when the contact email is freemail (gmail/yahoo/etc).
    domain = contact.get("domain") or ""
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    dom_tok = f" dom@{domain}" if domain and domain != email_domain else ""
    return (
        f"{date} | {email} | [[{slug}]] "
        f"| step 1 (whatsapp) | {run_slug} | wa:{phone}{dom_tok}"
    )


def append_whatsapp_rows(wa_jsonl: Path, sent_log: Path, run_slug: str, contact_index: dict) -> int:
    if not wa_jsonl.exists():
        return 0
    existing_phones = load_existing_wa_phones(sent_log)
    new_rows: list[str] = []
    for line in wa_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        send = json.loads(line)
        if send.get("result") != "sent":
            continue
        phone = re.sub(r"\D", "", (send.get("to_jid") or "").split("@", 1)[0] or (send.get("to_phone") or ""))
        if not phone or phone in existing_phones:
            continue
        contact = contact_index.get(send.get("lead_id"), {})
        new_rows.append(format_wa_row(send, contact, run_slug))
        existing_phones.add(phone)

    if not new_rows:
        return 0

    sent_log.parent.mkdir(parents=True, exist_ok=True)
    if not sent_log.exists():
        sent_log.write_text(
            "---\nname: sent-log\ndescription: Permanent dedup log of all outreach emails sent\n"
            "metadata:\n  type: project\n---\n\n# Sent Log\n\n"
            "Master dedup record. Format: `date | email | lead_slug | step | run_slug | brevo_message_id`\n\n"
        )
    with sent_log.open("a") as f:
        for row in new_rows:
            f.write(row + "\n")
    return len(new_rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--sent-log", required=True)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    sent_jsonl = run_dir / "emails-sent.jsonl"
    run_slug = run_dir.name
    sent_log = Path(args.sent_log)

    added = append_rows(sent_jsonl, sent_log, run_slug)
    print(f"Persisted {added} new email entries to {args.sent_log}")

    # WhatsApp channel: record every successful WhatsApp send too, so a lead
    # contacted on WhatsApp is never re-contacted on either channel.
    wa_added = append_whatsapp_rows(
        run_dir / "whatsapp-sent.jsonl", sent_log, run_slug, load_contact_index(run_dir)
    )
    if wa_added:
        print(f"Persisted {wa_added} new WhatsApp entries to {args.sent_log}")


if __name__ == "__main__":
    main()
