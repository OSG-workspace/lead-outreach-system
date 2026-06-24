#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config, load_jsonl, merge_signals, resolve_email_pool, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve the best clean email for source-fit leads.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input", default=None)
    parser.add_argument("--signals", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    build_funnel_config(run_dir)  # validates the run has an ICP file.
    input_path = Path(args.input) if args.input else run_dir / "leads-source-fit.json"
    signals_path = Path(args.signals) if args.signals else run_dir / "lead-signals.json"
    output_path = Path(args.output) if args.output else run_dir / "leads-email-resolved.json"
    report_path = Path(args.report) if args.report else run_dir / "email-resolution-report.json"

    rows = merge_signals(load_jsonl(input_path), load_jsonl(signals_path))
    resolved, drops = resolve_email_pool(rows)
    write_jsonl(output_path, resolved)
    report = {
        "input": len(rows),
        "resolved": len(resolved),
        "dropped": dict(drops),
        "input_file": str(input_path),
        "signals_file": str(signals_path),
        "output": str(output_path),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
