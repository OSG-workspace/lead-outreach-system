#!/usr/bin/env python3
"""Stage 3: Merge candidates-batch-*.txt → candidates-all.txt.

Deduplicates by domain, drops already-sent domains, drops PREVIOUSLY-SOURCED
domains (every fired run must surface only fresh leads — never a domain any
earlier run already sourced), and enforces the country filter declared in
`<run-dir>/countries.txt` if present, else defaults to the GCC set.
countries.txt is a newline-separated list of ISO-2 codes
(e.g. `LB` for a Lebanon run; `AE\\nSA\\nQA\\nBH\\nKW` for GCC).

Dedup nets applied here (in order):
  1. within-run domain dedup (same domain from two queries)
  2. sent-log email domains + `dom@<domain>` website tokens
  3. sent-log `[[lead-slug]]` tokens matched against the slugified candidate
     domain (catches sends where the contact email was on a DIFFERENT domain
     than the business website — the bug that re-sent San Pietro Taormina 4x)
  4. the sourced-log ledger (vault/lead-outreach/sourced-log.txt): any domain
     a previous run already sourced is skipped FOREVER, contacted or not
     (searched-and-rejected businesses never resurface in a later fire).
     Override: SOURCED_SKIP=off disables net 4 for one run;
     SOURCED_SKIP_DAYS=<n> restores an n-day retry window.
"""
from __future__ import annotations
import argparse
import os
import re
from datetime import date
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


SLUG_RE = re.compile(r"\[\[([a-z0-9\-]+)\]\]")


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


def load_sent_slugs(sent_log: Path) -> set[str]:
    """[[lead-slug]] tokens from the sent-log. The slug is the business WEBSITE
    domain with dots flattened to hyphens, so slugify(candidate_domain) matching
    one of these means this exact business was already contacted — even when
    the contact email lived on a different (chain/parent/personal) domain."""
    if not sent_log.exists():
        return set()
    slugs: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in SLUG_RE.finditer(line):
            slugs.add(m.group(1))
    return slugs


def _norm_domain(domain: str) -> str:
    d = domain.lower().strip().rstrip("/")
    d = d.split("//")[-1].split("/")[0]
    if d.startswith("www."):
        d = d[4:]
    return d


def _slugify(domain: str) -> str:
    return _norm_domain(domain).replace(".", "-")


def sourced_log_path(sent_log: Path) -> Path:
    """The sourced ledger lives next to the sent-log in the vault."""
    return sent_log.parent / "sourced-log.txt"


def load_sourced_domains(sourced_log: Path, current_run: str) -> set[str]:
    """Domains a PREVIOUS run sourced within the freshness window (rows for the
    current run are ignored so re-running the merge stage never blocks itself).

    Freshness policy (user directive 2026-07-17: every fire touches ONLY
    never-seen businesses):
      * CONTACTED domains are blocked forever — but by the sent-log nets, not here.
      * Sourced-but-never-contacted domains (incl. searched-and-rejected) are
        ALSO blocked forever by default.
    Line format: `domain|YYYY-MM-DD|run-slug`.
    SOURCED_SKIP=off disables for one run; SOURCED_SKIP_DAYS=<n> re-enables the
    old n-day retry window if a deliberate re-sweep is ever wanted."""
    if os.environ.get("SOURCED_SKIP", "").lower() in {"off", "0", "no"}:
        return set()
    if not sourced_log.exists():
        return set()
    window_raw = os.environ.get("SOURCED_SKIP_DAYS", "forever").lower()
    forever = window_raw in {"forever", "all", "inf"}
    try:
        window_days = 0 if forever else int(window_raw)
    except ValueError:
        window_days = 90
    today = date.today()
    domains: set[str] = set()
    for line in sourced_log.read_text().splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3 or parts[2] == current_run:
            continue
        if not forever:
            try:
                age = (today - date.fromisoformat(parts[1])).days
            except ValueError:
                age = 0
            if age > window_days:
                continue
        domains.add(_norm_domain(parts[0]))
    return domains


def load_disqualified_domains(sent_log: Path) -> set[str]:
    """Domains PROVEN not to qualify, which are the only ones blocked forever.

    User directive 2026-07-27: "the condition for dropping leads for all future
    runs is if they have already been proven to NOT be qualified; if any lead has
    been proven to be qualified he should always be contacted."

    So permanence now follows evidence, not mere exposure:
      * contacted            -> blocked forever (sent-log, unchanged)
      * proven unqualified   -> blocked forever (THIS ledger, written by
                                qualify_leads.py when its gate rejects a lead)
      * qualified but not yet contacted -> NOT blocked; it must stay reachable
                                until it is actually contacted
      * merely sourced, never judged    -> NOT blocked; being seen proves nothing

    Line format: `domain|YYYY-MM-DD|run-slug|reason`.
    """
    ledger = sent_log.parent / "disqualified-log.txt"
    if not ledger.exists():
        return set()
    out: set[str] = set()
    for line in ledger.read_text().splitlines():
        parts = line.strip().split("|")
        if parts and parts[0].strip():
            out.add(_norm_domain(parts[0]))
    return out


def resolve_ok(domain: str) -> bool:
    """True if the domain actually exists in DNS.

    Source agents sometimes emit plausible-looking domains that do not exist
    (`aubmc.edu.lb`, `tcl.com.lb` were NXDOMAIN in the 2026-07-26 run, where the
    real host is `aubmc.org.lb`). Those cost a fetch, produce no pages, and die at
    extract — silently shrinking the run. Checking DNS here kills them up front.
    """
    import socket
    try:
        socket.getaddrinfo(domain, None)
        return True
    except OSError:
        # No A/AAAA record. Some hosts are mail-only, so accept a valid MX too.
        try:
            import subprocess as _sp
            mx = _sp.run(["dig", "+short", "MX", domain], capture_output=True,
                         text=True, timeout=8).stdout.strip()
            return bool(mx)
        except Exception:
            return False


def drop_nonexistent(rows: list[str]) -> tuple[list[str], int]:
    """Filter out candidate rows whose domain does not resolve (parallel DNS)."""
    if not rows:
        return rows, 0
    from concurrent.futures import ThreadPoolExecutor
    domains = [_norm_domain(r.split("|")[0]) for r in rows]
    with ThreadPoolExecutor(max_workers=32) as ex:
        ok = list(ex.map(resolve_ok, domains))
    kept = [r for r, good in zip(rows, ok) if good]
    return kept, len(rows) - len(kept)


def append_sourced(sourced_log: Path, domains: list[str], run_slug: str) -> int:
    """Record this run's kept domains in the ledger (idempotent per run)."""
    existing: set[tuple[str, str]] = set()
    if sourced_log.exists():
        for line in sourced_log.read_text().splitlines():
            parts = line.strip().split("|")
            if len(parts) >= 3:
                existing.add((parts[0], parts[2]))
    today = date.today().isoformat()
    new_rows = [f"{d}|{today}|{run_slug}" for d in domains if (d, run_slug) not in existing]
    if new_rows:
        sourced_log.parent.mkdir(parents=True, exist_ok=True)
        with sourced_log.open("a") as f:
            for r in new_rows:
                f.write(r + "\n")
    return len(new_rows)


def merge(run_dir: Path, sent_log: Path) -> list[str]:
    sent_domains = load_sent_domains(sent_log)
    sent_slugs = load_sent_slugs(sent_log)
    # Permanence follows EVIDENCE (user directive 2026-07-27): only a domain we
    # proved unqualified is blocked forever. Being merely sourced proves nothing,
    # and a qualified-but-uncontacted domain must stay reachable. Set
    # SOURCED_BLOCK=on to restore the old "everything ever sourced is dead" rule.
    disqualified = load_disqualified_domains(sent_log)
    sourced = (load_sourced_domains(sourced_log_path(sent_log), run_dir.resolve().name)
               if os.environ.get("SOURCED_BLOCK", "").lower() in {"on", "1", "yes"}
               else set())
    allowed = load_allowed_countries(run_dir)
    seen: set[str] = set()
    rows: list[str] = []
    dropped_sent = dropped_sourced = dropped_country = dropped_disqualified = 0
    dropped_country_codes: dict[str, int] = {}
    for batch_file in sorted(run_dir.glob("candidates-batch-*.txt")):
        for raw in batch_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            domain = _norm_domain(parts[0])
            country = parts[2].upper().strip()
            if domain in seen:
                continue
            if allowed is not None and country not in allowed:
                dropped_country += 1
                dropped_country_codes[country] = dropped_country_codes.get(country, 0) + 1
                continue
            if domain in sent_domains or _slugify(domain) in sent_slugs:
                dropped_sent += 1
                continue
            if domain in disqualified:
                dropped_disqualified += 1
                continue
            if domain in sourced:
                dropped_sourced += 1
                continue
            seen.add(domain)
            rows.append(line)
    # Fake/typo domains die here, before they cost a fetch and vanish at extract.
    rows, dropped_dns = drop_nonexistent(rows)
    if dropped_sent or dropped_sourced or dropped_disqualified:
        print(f"Dedup: dropped {dropped_sent} previously-contacted, "
              f"{dropped_disqualified} proven-unqualified, "
              f"{dropped_sourced} previously-sourced domains.")
    if dropped_dns:
        print(f"Domain check: dropped {dropped_dns} candidates whose domain does not "
              f"resolve (agent-invented or typo hostnames).")
    # Country-filter drops must NEVER be silent: a countries.txt that lags the
    # campaign's actual sourcing scope throws away good candidates invisibly
    # (bug class of commit 393f37a — hit eu-hotels twice). Report always; warn
    # loudly when the filter eats a large share.
    if dropped_country:
        top = ", ".join(f"{c}:{n}" for c, n in
                        sorted(dropped_country_codes.items(), key=lambda kv: -kv[1])[:8])
        print(f"Country filter: dropped {dropped_country} candidates outside "
              f"countries.txt ({top}).")
        kept_plus = len(rows) + dropped_country
        if kept_plus and dropped_country / kept_plus > 0.3:
            print(f"WARN: the country filter discarded "
                  f"{dropped_country}/{kept_plus} ({dropped_country * 100 // kept_plus}%) of "
                  f"fresh candidates. countries.txt likely lags the campaign's sourcing "
                  f"scope — align it with where sourcing actually looks (fixture: "
                  f"templates/<base>/countries.txt).")
    total_unique = len(rows) + dropped_sent + dropped_sourced + dropped_disqualified
    stale = dropped_sourced + dropped_disqualified + dropped_sent
    if total_unique and stale / total_unique > 0.5:
        print(f"WARN: {stale}/{total_unique} unique candidates were already "
              f"contacted, rejected, or sourced by earlier runs — the campaign's queries "
              f"are going STALE. "
              f"Expand places.txt in the template fixture (new cities, "
              f"sub-verticals, phrasings) to keep surfacing fresh leads.")
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--sent-log", required=True)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    sent_log = Path(args.sent_log)
    rows = merge(run_dir, sent_log)

    out = run_dir / "candidates-all.txt"
    out.write_text("\n".join(rows) + ("\n" if rows else ""))

    # Ledger every kept domain so NO future run ever re-sources it (fresh
    # leads only, every fire). Skipped in tests (ledger lives next to sent-log).
    kept_domains = [_norm_domain(r.split("|")[0]) for r in rows]
    added = append_sourced(sourced_log_path(sent_log), kept_domains, run_dir.resolve().name)

    batch_count = len(list(run_dir.glob("candidates-batch-*.txt")))
    print(f"Merged {len(rows)} candidates from {batch_count} batch files → {out}")
    print(f"Sourced-log: recorded {added} new domains for future-run freshness dedup.")


if __name__ == "__main__":
    main()
