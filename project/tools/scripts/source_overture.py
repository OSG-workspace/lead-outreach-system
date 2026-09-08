#!/usr/bin/env python3
"""Stage 2 (deterministic source adapter): BULK open POI data via Overture Maps.

WHY THIS EXISTS
The other two geographic adapters both meter or starve:

  * `source_overpass.py` (OSM) indexes PREMISES, and only what volunteers mapped.
    Measured 2026-08-11 across 13 GCC cities it returned 61 usable candidates in
    total, because Gulf OSM carries names but almost no `website` tag.
  * `source_places.py` (Google Places API) is per-request metered and caps at 60
    results per search, so a city is swept by quadtree subdivision. On
    2026-08-11-gcc-receptionist that budget (150 requests) was fully spent inside
    ONE city, Riyadh, and still reported `INCOMPLETE`: 0 rows with a domain,
    763 name-only.

Overture Places is the third shape: a bulk, license-clean, zero-cost dataset you
query in place. One `read_parquet` over an S3 release with a bbox predicate
returns a whole city at once — no request ceiling to run out of, no per-call
cost, no key. Measured on the 2026-07-22.0 release (this adapter's own probe):

    Riyadh bbox alone -> 41,396 POIs in 32s, 21,823 (52.7%) carrying a website
    Riyadh + Dubai, the six gcc-receptionist verticals -> ~44k POIs, ~26k with a
    website (restaurant 24,729/13,839 · salon 7,676/4,254 · carservice
    4,687/2,924 · realestate 4,806/3,455 · homeservices 1,757/1,434)

That website field is the real prize. Every other weakly-mapped source hands
name-only rows to Stage 2.5 to resolve and prove; roughly half of Overture's
rows already carry the business's own domain, so they enter the chain as
candidates directly and Stage 2.5 only has to work on the remainder.

This is NOT a GCC adapter. It takes its geography from the fixture's own
places.txt exactly like every other source, so the same code sweeps Sydney,
Beirut, Madrid or Ohio; the only per-campaign config is which Overture
categories map to which vertical.

LICENSE  Overture Places is CDLA Permissive 2.0 / Apache 2.0 — commercial use
permitted, and (unlike the Places API Content rules that force `source_places.py`
onto Pro tier) the website/name fields ARE storage-eligible, so persisting them
into sourced-log.txt / sent-log.md is fine.

OUTPUT  <run-dir>/candidates-batch-<prefix>-NNN.txt   `domain|Name|ISO2|vertical|0`
        <run-dir>/unresolved-<prefix>-NNN.txt         `Name|ISO2|City|vertical`

CONFIG (one entry in sourcing.json "sources"):
  {"type": "overture",
   "targets": [
     {"vertical": "salon",      "category_match": "salon|barber|spa|beauty|nail"},
     {"vertical": "realestate", "categories": ["real_estate", "real_estate_agent"]}
   ],
   "release": "2026-07-22.0",   # optional; default = newest release discovered
   "min_confidence": 0.5,       # optional; drop low-confidence rows
   "require_website": true,     # optional (default true) — see below
   "include_closed": false}     # optional; default drops operating_status='closed'

  `category_match` is a case-insensitive regex tested against BOTH
  `categories.primary` and `categories.alternate`; `categories` is an exact-match
  list. Give either or both. Overture's taxonomy is wide (131 distinct restaurant
  categories in Riyadh+Dubai alone), so the regex form is usually what you want.

  `require_website` defaults TRUE: emit only rows whose own domain Overture
  already carries. That is the point of this source — it turns straight into
  candidates. Set it false to ALSO hand the name-only rows to Stage 2.5, at the
  cost of a much larger resolve queue.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
SENT_LOG = PROJECT / "vault" / "lead-outreach" / "sent-log.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_candidates as _mc                      # shared dedup nets
from resolve_domains import DIRECTORY as _AGGREGATORS   # shared never-a-business list
from source_overpass import (load_places_file, _domain_from_url,
                             ledger_city, load_city_ledger)

# Hosts that are a PLATFORM, never the business itself. Distinct from the shared
# aggregator list (`DIRECTORY`, which is social/portal/press): these are booking
# SaaS, delivery marketplaces, storefront builders and reseller subdomains that
# Overture legitimately records in `websites` because it IS where the business
# sends customers. Measured on the first Riyadh salon scan, they arrived as
# book.glamera.com, app.shedul.com, wj1.daftara.com and partners.naeem.cg.sa.
#
# Two independent reasons a row like that must not become a candidate:
#   1. the host is SHARED by thousands of businesses, so contacting it writes one
#      domain into sent-log.md on behalf of all of them and permanently blocks
#      the rest at the dedup net;
#   2. a business already living on a booking platform has solved the very gap
#      every receptionist campaign pitches — the chain's own `modern_booking`
#      rule says skip it.
# Filtered rows fall to the name-only path, so with `require_website: false`
# Stage 2.5 can still recover the business's real domain.
PLATFORM = re.compile(
    r"(^|\.)(glamera|shedul|fresha|booksy|treatwell|setmore|calendly|acuityscheduling|"
    r"simplybook|daftara|vagaro|mindbodyonline|squareup|square\.site|book\.app|"
    r"opentable|resy|thefork|quandoo|talabat|hungerstation|deliveroo|ubereats|"
    r"doordash|zomato|jahez|toyou|careem|noon\.com|instashop|"
    r"salla|zid\.store|shopify|myshopify|bigcartel|ecwid|"
    r"wixsite|weebly|godaddysites|business\.site|sites\.google|webnode|"
    r"jimdosite|strikingly|carrd\.co|hostinger\.site|blogspot)\.", re.I)

BUCKET = "overturemaps-us-west-2"
LIST_URL = (f"https://{BUCKET}.s3.us-west-2.amazonaws.com/"
            "?list-type=2&prefix=release/&delimiter=/")
THEME = "theme=places/type=place/*"
ATTEMPTS = 4          # remote parquet reads hit transient DNS/IO; see scan_city()


def latest_release(pinned: str = "") -> str:
    """Newest Overture release id, e.g. `2026-07-22.0`.

    Discovered from the bucket's own listing rather than hardcoded, so the
    adapter keeps working as Overture ships monthly releases. A fixture can pin
    `"release"` for reproducibility. NOTE: DuckDB's `glob()` over this bucket
    returns 0 rows (anonymous LIST is not exposed through the S3 filesystem
    layer), which is why this goes through the REST listing endpoint.
    """
    if pinned:
        return pinned
    try:
        req = urllib.request.Request(LIST_URL, headers={"User-Agent": "lead-outreach/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        sys.exit(f"ABORT: could not list Overture releases ({type(e).__name__}: {e}). "
                 f'Pin one in sourcing.json, e.g. "release": "2026-07-22.0".')
    rels = sorted(set(re.findall(r"<Prefix>release/([^<]+?)/</Prefix>", xml)))
    if not rels:
        sys.exit("ABORT: Overture release listing returned no releases. "
                 'Pin one in sourcing.json, e.g. "release": "2026-07-22.0".')
    return rels[-1]


def _sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def load_presets() -> dict:
    p = Path(__file__).resolve().parent / "overture_presets.json"
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def resolve_target(target: dict, presets: dict) -> dict:
    """Fill in a target's categories from the shared preset map when it names none.

    Precision here is a CHAIN-level asset, not a per-campaign chore. Overture
    ships 1,862 distinct primary categories and hand-written regexes get them
    wrong in ways that are invisible until you read the leads: a `salon` regex
    pulls in Sephora / MAC / H&M (cosmetics RETAIL, not salons), `ngo` matches
    bingo_hall and mongolian_restaurant, `homeservices` matches
    movie_television_studio. So the curated lists live once in
    overture_presets.json and every fixture inherits them.

    Lookup order, first hit wins:
      1. the target's own `categories` / `category_match`  (explicit override)
      2. `preset`: "<name>"
      3. the vertical verbatim                     (`clinic`  -> clinic preset)
      4. the vertical's first segment              (`clinic-gcc`, `hotel-eu`,
         `trades-emergency` -> clinic / hotel / trades), which is this repo's
         existing convention for naming a campaign's copy of a vertical.
    """
    if target.get("categories") or target.get("category_match"):
        return target
    vertical = str(target.get("vertical") or "")
    for key in (target.get("preset"), vertical, vertical.split("-")[0]):
        if not key:
            continue
        hit = presets.get(key)
        if hit is None:
            continue
        out = dict(target)
        if isinstance(hit, dict):          # preset carries a regex
            out["category_match"] = hit["category_match"]
        else:                              # preset carries an exact list
            out["categories"] = hit
        out["_preset_used"] = key
        return out
    return target


def category_predicate(target: dict) -> str:
    """SQL matching one target's Overture categories (primary OR alternate).

    Matching alternate as well as primary matters for recall: a dental practice
    is frequently `categories.primary='hospital'` with `dentist` sitting in
    `alternate`, and a primary-only filter silently loses it.
    """
    parts = []
    cats = target.get("categories") or []
    if cats:
        lst = ", ".join(_sql_str(c) for c in cats)
        parts.append(f"(categories.primary IN ({lst}) OR "
                     f"list_has_any(coalesce(categories.alternate, []::VARCHAR[]), [{lst}]))")
    pat = (target.get("category_match") or "").strip()
    if pat:
        p = _sql_str(pat)
        parts.append(
            f"(regexp_matches(coalesce(categories.primary, ''), {p}, 'i') OR "
            f"regexp_matches(array_to_string("
            f"coalesce(categories.alternate, []::VARCHAR[]), ','), {p}, 'i'))")
    if not parts:
        sys.exit(f'ABORT: overture target {target.get("vertical")!r} needs '
                 f'"categories" (exact list) and/or "category_match" (regex).')
    return "(" + " OR ".join(parts) + ")"


def scan_city(con, src_glob: str, meta: tuple, target: dict, cfg: dict) -> list[tuple]:
    """One (city, target) unit -> raw rows. Retries transient remote-read errors.

    A 13-bbox single-scan attempt died mid-read with `Could not resolve hostname`
    on 2026-08-11; per-city scans are both smaller and independently retryable,
    and they line up with the per-city ledger the other adapters already use.
    """
    _iso, lat, lon, hw = meta
    xmin, xmax, ymin, ymax = lon - hw, lon + hw, lat - hw, lat + hw
    where = [f"bbox.xmin BETWEEN {xmin} AND {xmax}",
             f"bbox.ymin BETWEEN {ymin} AND {ymax}",
             category_predicate(target)]
    if not cfg.get("include_closed"):
        where.append("coalesce(operating_status, 'open') <> 'closed'")
    minc = cfg.get("min_confidence")
    if minc is not None:
        where.append(f"coalesce(confidence, 0) >= {float(minc)}")
    q = f"""
    SELECT names.primary,
           websites[1],
           CASE WHEN len(coalesce(addresses, [])) > 0 THEN addresses[1].country END
    FROM read_parquet({_sql_str(src_glob)}, filename=false, hive_partitioning=1)
    WHERE {' AND '.join(where)}
    """
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return con.execute(q).fetchall()
        except Exception as e:                       # transient S3/DNS/IO
            last = e
            if attempt < ATTEMPTS:
                import time
                print(f"      read failed ({type(e).__name__}), retry {attempt}/{ATTEMPTS - 1}")
                time.sleep(3 * attempt)
    raise RuntimeError(f"Overture read failed after {ATTEMPTS} attempts: {last}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--source", default="", help="the source object as JSON (from sourcing.json)")
    ap.add_argument("--batch-prefix", default="overture")
    ap.add_argument("--max-candidates", type=int, default=350)
    ap.add_argument("--cities", default="")
    ap.add_argument("--ignore-ledger", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan the sweep (release, cities, targets) without reading Overture")
    a = ap.parse_args()

    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    run_slug = run.resolve().name
    cfg = json.loads(a.source) if a.source else {}

    presets = load_presets()
    targets = []
    for t in (cfg.get("targets") or []):
        if not t.get("vertical"):
            sys.exit('ABORT: each overture target needs a "vertical".')
        t = resolve_target(t, presets)
        if not (t.get("categories") or t.get("category_match")):
            sys.exit(
                f'ABORT: overture target {t["vertical"]!r} names no categories and no '
                f'preset matches it.\n  Either add "categories"/"category_match" to the '
                f'target, or add a "{t["vertical"].split("-")[0]}" entry to '
                f'tools/scripts/overture_presets.json.\n  Known presets: '
                f'{", ".join(sorted(k for k in presets if not k.startswith("_")))}')
        category_predicate(t)                        # validate now, not mid-sweep
        targets.append(t)
    if not targets:
        sys.exit('ABORT: overture source declares no "targets". Example:\n'
                 '  {"type": "overture", "targets": [{"vertical": "salon"}]}\n'
                 '  (categories come from tools/scripts/overture_presets.json unless '
                 'the target overrides them.)')

    places_file = run / "places.txt"
    if not places_file.exists():
        sys.exit("ABORT: overture source needs a places.txt in the run/fixture "
                 "(`City|ISO2|lat|lon|half_width_deg`, one per line).")
    places = load_places_file(places_file)
    wanted = ([c.strip() for c in a.cities.split(",") if c.strip()]
              if a.cities else list(places.keys()))
    unknown = [c for c in wanted if c not in places]
    if unknown:
        sys.exit(f"ABORT: unknown cities (not in places.txt): {unknown}")

    release = latest_release(str(cfg.get("release") or ""))
    src_glob = f"s3://{BUCKET}/release/{release}/{THEME}"
    require_site = cfg.get("require_website", True)
    print(f"Overture config: release={release} targets="
          f"{[t['vertical'] for t in targets]} places={len(places)} "
          f"require_website={bool(require_site)} "
          f"min_confidence={cfg.get('min_confidence')}")
    for t in targets:
        how = (f"preset '{t['_preset_used']}'" if t.get("_preset_used")
               else "fixture-defined categories")
        n = len(t.get("categories") or []) or "regex"
        print(f"    {t['vertical']:22} <- {how} ({n} categories)")

    if a.dry_run:
        for t in targets:
            vkey = f"overture:{t['vertical']}"
            fired = set() if a.ignore_ledger else load_city_ledger(vkey)
            todo = [c for c in wanted if c not in fired]
            print(f"  [plan] {t['vertical']:14} {len(todo)} city/cities to scan "
                  f"({len(wanted) - len(todo)} already ledgered)")
        print("\n[plan] no Overture reads made.")
        return

    try:
        import duckdb
    except ImportError:
        # Exit 8, not 1: an unconfigured source opened no ground, exactly like a
        # places source with no API key. Other sources in the fire still run.
        print("NO DUCKDB: the overture source needs duckdb.\n"
              "  fix: ./tools/venv/bin/python3 -m pip install duckdb\n"
              "  (or re-run `bash tools/install.sh`)\n"
              "Nothing was sourced and no ground was consumed.", file=sys.stderr)
        sys.exit(8)

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'; "
                "SET enable_progress_bar=false;")

    blocked_domains = _mc.load_sent_domains(SENT_LOG) | \
        _mc.load_sourced_domains(_mc.sourced_log_path(SENT_LOG), run_slug)
    blocked_slugs = _mc.load_sent_slugs(SENT_LOG)
    print(f"Fresh-only cap: {len(blocked_domains)} previously-seen domains excluded at source.")

    total = total_unres = batch_n = skipped = cities_scanned = 0
    seen_dom: set[str] = set()
    seen_name: set[str] = set()

    for t in targets:
        vertical = t["vertical"]
        # Namespaced `overture:` so this sweep never inherits the OSM or Places
        # sweep's exhausted cities — they enumerate different universes of the
        # same vertical, and ground spent in one is untouched in the others.
        vkey = f"overture:{vertical}"
        fired = set() if a.ignore_ledger else load_city_ledger(vkey)
        todo = [c for c in wanted if c not in fired]
        if not todo:
            print(f"  {vertical}: every city already swept for '{vkey}' — "
                  f"add places.txt rows or pass --ignore-ledger.")
            continue
        for city in todo:
            if (total + total_unres) >= a.max_candidates:
                break
            iso = places[city][0]
            print(f"  scanning {city} ({iso}) for {vertical}…")
            cities_scanned += 1
            try:
                rows = scan_city(con, src_glob, places[city], t, cfg)
            except RuntimeError as e:
                # A single city's S3 read timing out is not a reason to lose
                # every candidate already sourced this run (2026-09-04: one
                # exhausted-retry timeout on one Dubai part-file killed a fire
                # that had already banked 67 gmaps + 615 places leads). Skip
                # it unledgered so the next fire retries this city fresh.
                print(f"    {city}: SKIPPED — {e}")
                continue

            lines: list[str] = []
            unres: list[str] = []
            already = 0
            for name, website, country in rows:
                if (total + total_unres + len(lines) + len(unres)) >= a.max_candidates:
                    break
                name = (name or "").strip()
                if not name:
                    continue
                iso2 = (country or iso or "").strip().upper()[:2]
                domain = _domain_from_url(website or "")
                if domain and (_AGGREGATORS.search(domain) or PLATFORM.search(domain)):
                    domain = None   # a portal/social/booking-platform host is not the business
                if domain:
                    norm = _mc._norm_domain(domain)
                    if norm in blocked_domains or _mc._slugify(norm) in blocked_slugs:
                        already += 1
                        continue
                    if norm in seen_dom:
                        continue
                    seen_dom.add(norm)
                    lines.append(f"{norm}|{name.replace('|', ' ')}|{iso2}|{vertical}|0")
                elif not require_site:
                    k = f"{name.lower()}|{city}"
                    if k in seen_name:
                        continue
                    seen_name.add(k)
                    unres.append(f"{name.replace('|', ' ')}|{iso2}|{city}|{vertical}")

            batch_n += 1
            if lines:
                (run / f"candidates-batch-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(lines) + "\n")
            if unres:
                (run / f"unresolved-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(unres) + "\n")
            total += len(lines)
            total_unres += len(unres)
            skipped += already
            print(f"    {city}: {len(rows)} POIs -> {len(lines)} fresh candidates, "
                  f"{len(unres)} name-only, +{already} already-seen; "
                  f"running total {total + total_unres}")
            # A city is ledgered once scanned: unlike a metered API sweep there is
            # no partial-coverage state to resume — the bbox read is complete by
            # construction. The cap can still cut a city short, so only ledger
            # when this unit actually ran to the end of its rows.
            if (total + total_unres) < a.max_candidates:
                ledger_city(vkey, city, run_slug)

    print(f"\nOverture sourcing: {total} with a domain + {total_unres} name-only "
          f"-> {run}")
    if skipped:
        print(f"  {skipped} already-seen domains skipped at source "
              f"(never counted against the cap).")
    if total + total_unres >= a.max_candidates:
        print(f"  NOTE: stopped at the {a.max_candidates}-candidate cap. Cities left "
              f"open are not ledgered and the next fire resumes them.")

    if total or total_unres:
        return
    # rc=8 = "no fresh ground", an expected end state. Never exit 1: run_fire.py
    # tolerates only 8, so exiting 1 here would kill the whole fire including the
    # sources queued behind this one that still had ground.
    if cities_scanned == 0:
        print("NO FRESH GROUND: every requested city is already swept for every "
              "overture target. Add places.txt rows, add targets, or pass "
              "--ignore-ledger.", file=sys.stderr)
    elif skipped:
        print(f"NO FRESH GROUND: scanned {cities_scanned} city/target unit(s); all "
              f"{skipped} businesses found were already sourced or contacted.",
              file=sys.stderr)
    else:
        print(f"NO FRESH GROUND: scanned {cities_scanned} city/target unit(s) and "
              f"Overture matched 0 businesses. Check the target's category_match / "
              f"categories against the release's taxonomy"
              + (" (require_website=true drops rows with no domain — try false)"
                 if require_site else ""), file=sys.stderr)
    sys.exit(8)


if __name__ == "__main__":
    main()
