#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import build_funnel_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Build reusable funnel config from icp.yaml.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    config = build_funnel_config(run_dir)
    output = Path(args.output) if args.output else run_dir / "funnel-config.json"
    output.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    print(json.dumps({"output": str(output), **config}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
