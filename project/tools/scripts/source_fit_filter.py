#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import (
    apply_source_fit,
    build_funnel_config,
    load_jsonl,
    normalize_maps_record,
    normalize_web_record,
    write_jsonl,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply pre-score ICP source-fit filters.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    rows = [normalize_maps_record(row) for row in load_jsonl(run_dir / "maps-raw.json")]
    rows.extend(normalize_web_record(row) for row in load_jsonl(run_dir / "web-raw.json"))

    kept, drops = apply_source_fit(rows, cfg)
    output = Path(args.output) if args.output else run_dir / "leads-source-fit.json"
    report_path = Path(args.report) if args.report else run_dir / "source-fit-report.json"
    write_jsonl(output, kept)
    report = {
        "input": len(rows),
        "kept": len(kept),
        "dropped": dict(drops),
        "output": str(output),
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
