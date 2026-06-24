#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config, load_jsonl, merge_signals, qualify_ranked_signals, write_jsonl


def main() -> None:
    parser = argparse.ArgumentParser(description="Iterate ranked scored leads until concrete-signal target or exhaustion.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--input", default=None)
    parser.add_argument("--signals", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--target", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    input_path = Path(args.input) if args.input else run_dir / "leads-scored-balanced.json"
    signals_path = Path(args.signals) if args.signals else run_dir / "lead-signals.json"
    output_path = Path(args.output) if args.output else run_dir / "leads-signal-qualified.json"
    report_path = Path(args.report) if args.report else run_dir / "signal-qualification-report.json"

    scored = merge_signals(load_jsonl(input_path), load_jsonl(signals_path))
    result = qualify_ranked_signals(scored, target=args.target or cfg["target_sends"], batch_size=args.batch_size)
    write_jsonl(output_path, result["qualified"])
    report = {k: v for k, v in result.items() if k != "qualified"}
    report.update({"qualified": len(result["qualified"]), "input": str(input_path), "signals": str(signals_path), "output": str(output_path)})
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
