#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config, load_jsonl, score_lead, summarize_status, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Balanced canonical outreach lead scorer.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    input_path = Path(args.input) if args.input else run_dir / "leads-email-resolved.json"
    output_path = Path(args.output) if args.output else run_dir / "leads-scored-balanced.json"
    report_path = Path(args.report) if args.report else run_dir / "score-balanced-report.json"

    scored = [score_lead(row, cfg) for row in load_jsonl(input_path)]
    scored.sort(key=lambda row: -(row.get("score") or 0))
    write_jsonl(output_path, scored)
    report = {**summarize_status(scored), "input": str(input_path), "output": str(output_path)}
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
