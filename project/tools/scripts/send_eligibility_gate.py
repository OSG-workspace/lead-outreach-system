#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config, gate_drafts, load_jsonl, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run Brevo send eligibility gates.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--drafts", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    drafts_path = Path(args.drafts) if args.drafts else run_dir / "emails-drafted.json"
    output_path = Path(args.output) if args.output else run_dir / "emails-send-eligible.json"
    report_path = Path(args.report) if args.report else run_dir / "send-eligibility-report.json"

    eligible, report = gate_drafts(load_jsonl(drafts_path), role_threshold=cfg["role_inbox_threshold"])
    write_jsonl(output_path, eligible)
    report.update({"drafts": str(drafts_path), "output": str(output_path), "dry_run": True})
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
