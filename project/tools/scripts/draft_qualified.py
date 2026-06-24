#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config, build_outreach_draft, load_jsonl, validate_send_draft, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Draft gated outreach emails from signal-qualified leads.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--cap", type=int, default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    input_path = Path(args.input) if args.input else run_dir / "leads-signal-qualified.json"
    output_path = Path(args.output) if args.output else run_dir / "emails-drafted.json"
    report_path = Path(args.report) if args.report else run_dir / "draft-report.json"
    cap = args.cap or cfg["target_sends"]

    drafts = []
    drops: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    sent_emails: set[str] = set()
    for lead in sorted(load_jsonl(input_path), key=lambda row: -(row.get("score") or 0)):
        draft = build_outreach_draft(lead, run_slug=run_dir.name)
        ok, reason = validate_send_draft(draft, sent_emails=sent_emails, domain_counts=domain_counts, role_threshold=cfg["role_inbox_threshold"])
        if not ok:
            drops[reason] = drops.get(reason, 0) + 1
            continue
        drafts.append(draft)
        email = draft["to_email"].lower()
        sent_emails.add(email)
        domain = email.split("@", 1)[1]
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(drafts) >= cap:
            break

    write_jsonl(output_path, drafts)
    report = {
        "input": str(input_path),
        "output": str(output_path),
        "drafted": len(drafts),
        "dropped": drops,
        "cap": cap,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
