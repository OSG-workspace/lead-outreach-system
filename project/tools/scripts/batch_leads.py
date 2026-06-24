#!/usr/bin/env python3
"""
Split a leads JSONL file into fixed-size batches for scoring.

Reading 200 leads at once forces Claude to hold ~200KB of lead data in
context just to score them. This script chops the input into N-line batches
(default 20) so the orchestrator can score one batch at a time, drop
disqualified leads, and only carry the qualified subset forward.

Usage:
    python3 batch_leads.py \\
        --input runs/<slug>/leads-deduped.json \\
        --out-dir runs/<slug>/batches/ \\
        [--size 20]

Writes:
    runs/<slug>/batches/batch-001.jsonl
    runs/<slug>/batches/batch-002.jsonl
    ...
    runs/<slug>/batches/manifest.json   # list of batches + counts
"""

import argparse
import json
import sys
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--size", type=int, default=20)
    args = p.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    if not in_path.exists():
        print(f"error: {in_path} does not exist", file=sys.stderr)
        sys.exit(1)
    out_dir.mkdir(parents=True, exist_ok=True)

    batch_idx = 0
    in_batch = 0
    total = 0
    current = None
    files = []

    def open_batch(idx: int):
        path = out_dir / f"batch-{idx:03d}.jsonl"
        files.append(str(path))
        return path.open("w")

    with in_path.open() as f_in:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            if current is None or in_batch >= args.size:
                if current is not None:
                    current.close()
                batch_idx += 1
                current = open_batch(batch_idx)
                in_batch = 0
            current.write(line + "\n")
            in_batch += 1
            total += 1

    if current is not None:
        current.close()

    manifest = {
        "input": str(in_path),
        "batch_size": args.size,
        "total_leads": total,
        "batch_count": batch_idx,
        "batches": files,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
