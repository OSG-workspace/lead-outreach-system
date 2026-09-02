#!/usr/bin/env python3
"""Stage 2 (deterministic source adapter): enumerate Google Maps directly.

WHY THIS EXISTS
`source_places.py` calls the Google Places API (New) on the Pro tier, which is a
deliberate ToS choice: the Places ToS treats only the place ID as storage-eligible
indefinitely, so that adapter never requests `websiteUri` and every row it
produces is NAME-ONLY. Those names then go to Stage 2.5 (resolve_domains.py),
which is serial and resolves ~27% of them. Measured on 2026-08-17-au-trades-1:
569 name-only businesses -> 73 minutes -> 154 domains.

This adapter uses the `gosom/google-maps-scraper` binary already shipped by
tools/install.sh (and already listed in CLAUDE.md's free sourcing stack, though
until now nothing invoked it). It reads the same Maps data the API is a view
over, and returns the business's own website AND its Maps category in one pass.
Measured 2026-08-18, one `plumber in Parramatta NSW` query at depth 1:

    20 results · 19 carried a website (95%) · 20/20 genuinely plumbers

against the Places API's 0% (Pro tier requests no website at all) and its much
weaker precision — an `includedTypes:["plumber"]` searchNearby over Sydney
returns `manufacturer` and `building_materials_store` rows, which the au-trades
fit_criteria explicitly excludes.

So this source is strictly better on yield, precision and time. It is NOT free
of tradeoffs: scraping Maps is contrary to Google's Terms of Service, whereas
the Places API path is a paid-but-sanctioned route. That is a business-risk
decision the fixture author makes by declaring `{"type": "gmaps"}`; this file
does not make it silently, and `source_places.py` is left in place unchanged so
a fixture can choose either.

OUTPUT (identical contract to every other Stage 2 adapter)
  <run-dir>/candidates-batch-<prefix>-NNN.txt   `domain|Name|ISO2|vertical|0`
  <run-dir>/unresolved-<prefix>-NNN.txt         `Name|ISO2|City|vertical`

EXIT CODES
  0  produced yield
  8  no fresh ground (every city ledgered, or the binary is not installed) —
     an expected end state, so a run's OTHER sources still carry it

CONFIG (one entry in sourcing.json "sources")
  {
    "type": "gmaps",
    "depth": 10,                 # Maps scroll depth per query (default 10)
    "concurrency": 4,            # scraper's own parallelism (default 4)
    "extract_emails": false,     # -email: visits each site for addresses. SLOW.
    "require_category_match": true,   # drop rows whose Maps category is off-ICP
    "exclude_categories": ["wholesaler", "manufacturer"],   # optional denylist
    "targets": [
      {"vertical": "trades", "terms": ["plumber", "electrician", "roofing"]},
      {"vertical": "salon"}      # no terms -> taken from overture_presets.json
    ]
  }

The city ledger is keyed `gmaps:<vertical>` (NOT per term). Every term for a
vertical is swept in ONE invocation per city, so the city really is exhausted
for that vertical when it completes — unlike source_places.py, where each
included_type sweeps separately and therefore needs a per-type key.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SENT_LOG = PROJECT / "vault" / "lead-outreach" / "sent-log.md"
SCRAPER = PROJECT / "tools" / "google-maps-scraper"
PRESETS = Path(__file__).resolve().parent / "overture_presets.json"
GMAPS_PRESETS = Path(__file__).resolve().parent / "gmaps_presets.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_candidates as _mc                     # shared dedup nets
from source_overpass import (load_places_file, _domain_from_url,
                             ledger_city, load_city_ledger)

# A term list long enough to cover a vertical but short enough that one city is
# one bounded invocation. Overture presets are ordered most-central-first.
MAX_TERMS_DEFAULT = 6


def _gmaps_presets() -> dict:
    try:
        return json.loads(GMAPS_PRESETS.read_text())
    except Exception:
        return {}


def _preset(vertical: str) -> dict:
    """Preset for a vertical, falling back to its BASE name.

    Fixtures suffix a vertical per campaign (`clinic-gcc`, `clinic-us`,
    `clinic-lb`) so each keeps its own record of exhausted ground — the same
    reason source_places.py does it. Precision, though, is a property of the
    TRADE, not the campaign: a clinic is a clinic in Dubai and in New York. So
    the ledger key stays campaign-specific while the preset lookup strips the
    suffix, and one tuning improves every campaign firing that vertical."""
    pres = _gmaps_presets()
    key = vertical
    while key:
        if key in pres:
            return pres[key] or {}
        if "-" not in key:
            break
        key = key.rsplit("-", 1)[0]
    return {}


def terms_for(target: dict) -> list[str]:
    """Search phrases for one target, most specific source first:

      1. the target's own `terms` (a fixture overriding for its market)
      2. gmaps_presets.json — SHARED across every campaign, so tuning the
         precision of one vertical improves every fixture that fires it rather
         than one branch. This is the same reason overture_presets.json exists.
      3. the overture preset for the vertical, underscores turned into spaces
         (`hvac_services` -> `hvac services`), as a last resort.

    Maps is a text search, not a tag filter, so a term is a PHRASE a customer
    would type ("emergency plumber"), not an ontology label ("plumbing")."""
    explicit = [t.strip() for t in (target.get("terms") or []) if t.strip()]
    if explicit:
        return explicit
    vertical = target.get("vertical", "")
    pre = _preset(vertical).get("terms")
    if pre:
        return list(pre)[:MAX_TERMS_DEFAULT]
    presets = {}
    try:
        presets = json.loads(PRESETS.read_text())
    except Exception:
        pass
    cats = presets.get(vertical)
    if isinstance(cats, dict):          # e.g. restaurant uses {"category_match": ...}
        cats = None
    if not cats:
        known = sorted(set(list(_gmaps_presets().keys())
                           + [k for k, v in presets.items() if isinstance(v, list)]))
        known = [k for k in known if not k.startswith("_")]
        sys.exit(f'ABORT: gmaps target vertical "{vertical}" has no "terms", no '
                 f'gmaps preset and no overture preset to borrow. Add '
                 f'"terms": ["..."] to the target, or use a known vertical: '
                 f'{", ".join(known)}')
    return [c.replace("_", " ") for c in cats[:MAX_TERMS_DEFAULT]]


def keep_for(target: dict) -> list[str]:
    """Extra category STEMS to accept, from the shared preset. Never searched."""
    return list(_preset(target.get("vertical", "")).get("keep") or [])


def exclude_for(target: dict, source_level: list[str]) -> list[str]:
    """Category denylist: the shared per-vertical preset PLUS anything the
    fixture adds. Union, not override — a fixture should be able to add its own
    noise without having to restate the vertical's known noise."""
    pre = _preset(target.get("vertical", "")).get("exclude") or []
    return sorted({*(x.lower() for x in pre), *(x.lower() for x in source_level)})


def categories_of(row: dict) -> set[str]:
    out = {(row.get("category") or "").strip().lower()}
    for c in (row.get("categories") or []):
        out.add((c or "").strip().lower())
    return {c for c in out if c}


def category_ok(row: dict, terms: list[str], exclude: list[str],
                require_match: bool, keep: list[str] | None = None) -> bool:
    """Precision gate on the Maps category — the field source_places.py pays for
    (it is in the Pro field mask) and then discards, which is how trade-supply
    RETAIL enters a callout-tradie campaign and dies 4 stages later at qualify.

    Two independent dials, and keeping them separate matters:
      `terms` is the TIME dial — every term is one Maps query per city.
      `keep`  is the PRECISION dial — pure STEMS, matched but never searched,
              so widening what you accept costs nothing.

    Without `keep`, precision could only be bought with queries, and the token
    gate is morphology-naive: measured in Muscat it dropped `Dentist` (7),
    `Dermatologist` (3) and `Physical therapist` (2) because the term tokens
    were 'dental', 'dermatology' and 'physiotherapy' — none of which is a
    substring of the word Maps actually returned. Stems ('dentist', 'dermatolog',
    'therap') fix that for free."""
    cats = categories_of(row)
    if not cats:
        return not require_match          # unknown category: keep unless strict
    for bad in exclude:
        b = bad.strip().lower()
        if b and any(b in c for c in cats):
            return False
    if not require_match:
        return True
    for stem in (keep or []):
        st = stem.strip().lower()
        if st and any(st in c for c in cats):
            return True
    # A term matches if any of its significant tokens appears in any category.
    for term in terms:
        for tok in re.split(r"\W+", term.lower()):
            if len(tok) > 3 and any(tok in c for c in cats):
                return True
    return False


def run_scraper(queries: list[str], depth: int, concurrency: int,
                emails: bool, timeout_s: int) -> list[dict] | None:
    """One invocation over a batch of queries. -> rows, or None if it failed
    (caller must NOT ledger the city on None — that would retire unswept ground)."""
    if not SCRAPER.exists():
        return None
    with tempfile.TemporaryDirectory() as td:
        qf = Path(td) / "queries.txt"
        of = Path(td) / "out.json"
        qf.write_text("\n".join(queries) + "\n")
        cmd = [str(SCRAPER), "-input", str(qf), "-results", str(of), "-json",
               "-depth", str(depth), "-c", str(concurrency),
               "-exit-on-inactivity", "90s"]
        if emails:
            cmd.append("-email")
        try:
            subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, cwd=td)
        except subprocess.TimeoutExpired:
            print(f"    WARN: scraper timed out after {timeout_s}s — city stays open",
                  file=sys.stderr)
            return None
        if not of.exists():
            return None
        rows = []
        for line in of.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
        return rows


def sweep_outcome(total: int, total_unresolved: int, cities_swept: int,
                  skipped: int) -> tuple[int, str]:
    """Mirrors source_places.sweep_outcome. NO dry sweep exits 1: a source with
    nothing left must not pre-empt the sources queued behind it (2026-08-11)."""
    if total or total_unresolved:
        return 0, ""
    if cities_swept == 0:
        return 8, ("NO FRESH GROUND: every requested city is already swept for this "
                   "vertical. Add places.txt rows, or pass --ignore-ledger to "
                   "re-sweep (only useful if Maps has changed since).")
    if skipped:
        return 8, (f"NO FRESH GROUND: swept {cities_swept} city/cities; every result "
                   f"was already sourced or contacted ({skipped} skipped).")
    return 8, (f"NO FRESH GROUND: swept {cities_swept} city/cities and Maps returned "
               f"nothing usable. Not ledgered, so they are retried next fire. If this "
               f"repeats, check the terms are real Maps searches and the binary works: "
               f"`tools/google-maps-scraper -input q.txt -results out.json -json`.")


def _sourced_block(run_slug: str) -> set:
    """The sourced-log net, applied ONLY when merge_candidates would apply it.

    merge_candidates.merge() gates this net behind SOURCED_BLOCK (default OFF):
    being merely sourced proves nothing, so a domain no run ever contacted must
    stay reachable. A source adapter that blocks it unconditionally is stricter
    than the chain it feeds — it silently suppresses candidates the merge stage
    would have kept, and the operator sees only a smaller number."""
    import os
    if os.environ.get("SOURCED_BLOCK", "").lower() not in {"on", "1", "yes"}:
        return set()
    return _mc.load_sourced_domains(_mc.sourced_log_path(SENT_LOG), run_slug)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--source", default="", help="the source object as JSON")
    ap.add_argument("--batch-prefix", default="gmaps")
    ap.add_argument("--max-candidates", type=int, default=350)
    ap.add_argument("--cities", default="")
    ap.add_argument("--ignore-ledger", action="store_true")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-city scraper timeout in seconds")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan the sweep (cities, terms, queries) without scraping")
    a = ap.parse_args()

    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    run_slug = run.resolve().name
    src = json.loads(a.source) if a.source else {}

    depth = int(src.get("depth", 10))
    concurrency = int(src.get("concurrency", 4))
    emails = bool(src.get("extract_emails", False))
    require_match = bool(src.get("require_category_match", True))
    exclude = list(src.get("exclude_categories") or [])

    targets = []
    for t in (src.get("targets") or []):
        if not t.get("vertical"):
            sys.exit('ABORT: each gmaps target needs a "vertical", e.g. '
                     '{"vertical": "trades", "terms": ["plumber"]}')
        targets.append({"vertical": t["vertical"], "terms": terms_for(t),
                        "exclude": exclude_for(t, exclude),
                        "keep": keep_for(t)})
    if not targets:
        sys.exit('ABORT: gmaps source declares no "targets". Example:\n'
                 '  {"type": "gmaps", "targets": [{"vertical": "trades", '
                 '"terms": ["plumber", "electrician"]}]}')

    places_file = run / "places.txt"
    if not places_file.exists():
        sys.exit("ABORT: gmaps source needs a places.txt in the run/fixture "
                 "(`City|ISO2|lat|lon|half_width_deg`, one per line).")
    places = load_places_file(places_file)

    if not SCRAPER.exists() and not a.dry_run:
        # Same contract as a missing Places key: an unconfigured source opens no
        # ground, it does not degrade the campaign. Nothing is swapped or guessed.
        print(f"NO GMAPS SCRAPER: {SCRAPER} is missing, so this source opened no "
              f"ground.\n  Install it:  bash tools/install.sh\n"
              f"Nothing was sourced and no ground was consumed — re-fire once it exists.",
              file=sys.stderr)
        sys.exit(8)

    print(f"gmaps config: targets={[(t['vertical'], t['terms']) for t in targets]} "
          f"places={len(places)} depth={depth} c={concurrency} emails={emails} "
          f"require_category_match={require_match}")

    blocked_domains = (_mc.load_sent_domains(SENT_LOG)
                       | _mc.load_disqualified_domains(SENT_LOG)
                       | _sourced_block(run_slug))
    blocked_slugs = _mc.load_sent_slugs(SENT_LOG)
    print(f"Fresh-only cap: {len(blocked_domains)} previously-seen domains excluded at source.")

    wanted = ([c.strip() for c in a.cities.split(",") if c.strip()]
              if a.cities else list(places.keys()))
    unknown = [c for c in wanted if c not in places]
    if unknown:
        sys.exit(f"ABORT: unknown cities (not in places.txt): {unknown}")

    total = total_unresolved = batch_n = skipped = cities_swept = 0
    off_icp = 0
    dropped_cats: dict[str, int] = {}   # what the precision gate removed, for tuning
    seen: set[str] = set()

    # Per-target share, not one shared pot. A single counter meant target 1
    # spent the whole --max-candidates budget and every later vertical broke out
    # immediately with zero — a gcc-receptionist run would have returned clinics
    # and nothing else. Each vertical gets its own slice, so a multi-vertical
    # fixture comes back balanced.
    per_target = max(1, a.max_candidates // max(1, len(targets)))
    print(f"  budget: {a.max_candidates} candidates over {len(targets)} target(s) "
          f"-> {per_target} each")

    for t in targets:
        got = 0
        vkey = f"gmaps:{t['vertical']}"
        fired = set() if a.ignore_ledger else load_city_ledger(vkey)
        todo = [c for c in wanted if c not in fired]
        if not todo:
            print(f"  {t['vertical']}: every city already swept for '{vkey}' — "
                  f"add places.txt rows or pass --ignore-ledger.")
            continue
        for city in todo:
            # PER-TARGET only. A global `total >= max_candidates` check here
            # starved every later vertical: measured on a 3-target gcc-receptionist
            # sweep, clinic-gcc alone returned 218 rows from ONE city, the global
            # cap then tripped, and salon-gcc and realestate-gcc were skipped
            # without a single query. One city's yield is not divisible, so the
            # budget is a target and not a ceiling — but every target gets its turn.
            if got >= per_target:
                break
            country = places[city][0]
            queries = [f"{term} in {city}, {country}" for term in t["terms"]]
            if a.dry_run:
                print(f"  [plan] {city:22} {t['vertical']:14} {len(queries)} queries "
                      f"(depth {depth}) e.g. {queries[0]!r}")
                continue
            print(f"  sweeping {city} ({country}) for {t['vertical']} "
                  f"({len(queries)} terms)…")
            rows = run_scraper(queries, depth, concurrency, emails, a.timeout)
            if rows is None:
                print(f"    {city}: scraper failed — NOT ledgered, retried next fire")
                continue
            cities_swept += 1
            lines, unres = [], []
            for r in rows:
                name = (r.get("title") or "").strip()
                if not name:
                    continue
                if not category_ok(r, t["terms"], t["exclude"], require_match,
                                   t["keep"]):
                    off_icp += 1
                    cat = (r.get("category") or "?").strip()
                    dropped_cats[cat] = dropped_cats.get(cat, 0) + 1
                    continue
                domain = _domain_from_url(r.get("web_site") or "")
                if not domain:
                    unres.append(f"{name.replace('|', ' ')}|{country}|{city}|{t['vertical']}")
                    continue
                norm = _mc._norm_domain(domain)
                if norm in seen:
                    continue
                seen.add(norm)
                if norm in blocked_domains or _mc._slugify(norm) in blocked_slugs:
                    skipped += 1
                    continue
                lines.append(f"{domain}|{name.replace('|', ' ')}|{country}|{t['vertical']}|0")
            batch_n += 1
            if lines:
                (run / f"candidates-batch-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(lines) + "\n")
            if unres:
                (run / f"unresolved-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(unres) + "\n")
            total += len(lines)
            total_unresolved += len(unres)
            got += len(lines) + len(unres)
            print(f"    {city}: {len(lines)} fresh with a domain, {len(unres)} name-only, "
                  f"+{skipped} already-seen — running total {total + total_unresolved}")
            ledger_city(vkey, city, run_slug)

    if a.dry_run:
        print("\n[plan] no scraping performed.")
        return

    print(f"\ngmaps sourcing: {total} with a domain + {total_unresolved} name-only -> {run}")
    if off_icp:
        print(f"  {off_icp} rows dropped on the Maps category (off-ICP) before costing "
              f"a fetch:")
        for cat, n in sorted(dropped_cats.items(), key=lambda kv: -kv[1])[:12]:
            print(f"    {n:5d}  {cat}")
        # The gate is only as good as the terms behind it, and the terms are a
        # shared preset — so surfacing WHAT was dropped is what makes it tunable.
        # A category listed here that you actually want is fixed by adding a term
        # to gmaps_presets.json (its tokens are the allowlist), not by widening
        # the gate: `require_category_match: false` keeps everything, noise included.
        print(f"    -> want one of these? add a matching term for this vertical in "
              f"tools/scripts/gmaps_presets.json")
    if skipped:
        print(f"  {skipped} already-seen domains skipped at source.")
    code, msg = sweep_outcome(total, total_unresolved, cities_swept, skipped)
    if code:
        print(msg, file=sys.stderr)
        sys.exit(code)


if __name__ == "__main__":
    main()
