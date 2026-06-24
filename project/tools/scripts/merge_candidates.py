#!/usr/bin/env python3
"""Stage 3: Merge candidates-batch-*.txt → candidates-all.txt.

Deduplicates by domain, drops already-sent domains, enforces the country
filter declared in `<run-dir>/countries.txt` if present, else defaults to
the GCC set. countries.txt is a newline-separated list of ISO-2 codes
(e.g. `LB` for a Lebanon run; `AE\\nSA\\nQA\\nBH\\nKW` for GCC).
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

DEFAULT_COUNTRIES = {"AE", "SA", "QA", "BH", "KW"}


WORLDWIDE_GEOGRAPHIES = {"worldwide", "global", "international", "any", "all"}


def _icp_geography(run_dir: Path) -> str | None:
    """Read `geography:` from the run's icp.yaml without a yaml dependency."""
    f = run_dir / "icp.yaml"
    if not f.exists():
        return None
    for line in f.read_text().splitlines():
        s = line.strip()
        if s.lower().startswith("geography:"):
            return s.split(":", 1)[1].strip().strip("\"'").lower()
    return None


def load_allowed_countries(run_dir: Path) -> set[str] | None:
    """Return a set of allowed ISO-2 codes, or None for worldwide (accept any).

    Precedence:
      1. countries.txt — explicit override (`ALL`/`*` -> worldwide).
      2. icp.yaml `geography: worldwide` -> worldwide (no country filter).
         This stops a run that the ICP declares worldwide from being
         silently narrowed to the GCC default just because nobody dropped
         a countries.txt in the folder (the bug that capped the
         2026-05-30 / 2026-05-29-3 worldwide runs at ~51-71 candidates).
      3. Fallback: GCC default {AE,SA,QA,BH,KW}.
    """
    f = run_dir / "countries.txt"
    if f.exists():
        codes = {line.strip().upper() for line in f.read_text().splitlines() if line.strip() and not line.startswith("#")}
        if "*" in codes or "ALL" in codes:
            return None
        return codes or set(DEFAULT_COUNTRIES)
    if _icp_geography(run_dir) in WORLDWIDE_GEOGRAPHIES:
        return None
    return set(DEFAULT_COUNTRIES)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_sent_domains(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    domains: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if "@" in e and "smtp-relay" not in e and "mailin.fr" not in e:
                domains.add(e.split("@")[1])
    return domains


def merge(run_dir: Path, sent_log: Path) -> list[str]:
    sent_domains = load_sent_domains(sent_log)
    allowed = load_allowed_countries(run_dir)
    seen: set[str] = set()
    rows: list[str] = []
    for batch_file in sorted(run_dir.glob("candidates-batch-*.txt")):
        for raw in batch_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            domain = parts[0].lower().strip()
            country = parts[2].upper().strip()
            if domain in seen:
                continue
            if allowed is not None and country not in allowed:
                continue
            if domain in sent_domains:
                continue
            seen.add(domain)
            rows.append(line)
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--sent-log", required=True)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    rows = merge(run_dir, Path(args.sent_log))

    out = run_dir / "candidates-all.txt"
    out.write_text("\n".join(rows) + ("\n" if rows else ""))

    batch_count = len(list(run_dir.glob("candidates-batch-*.txt")))
    print(f"Merged {len(rows)} candidates from {batch_count} batch files → {out}")


if __name__ == "__main__":
    main()
