#!/usr/bin/env python3
"""Stage 2 source adapter: exhaustive enumeration via the Google Places API (New).

WHY THIS EXISTS — measured, not assumed (audit 2026-07-31)
`source_overpass.py` enumerates OpenStreetMap. OSM is an excellent index of
PREMISES and a near-useless index of businesses that have no premises. Probed
live against the public Overpass endpoint over the WHOLE Australian continent:

    nwr["craft"~"plumber|electrician|hvac"](-44,112,-10,154)
      -> 450 elements, 438 named, 159 with a website tag

159 usable domains. Not 159 per city — 159 for Australia. The au-trades campaign
had already consumed them: its city ledger showed 16/16 metros fired, and the
last three fires collapsed to 51 -> 4 -> 20 -> 5 fresh candidates before the
sweep ran out of ground entirely. Australia has tens of thousands of licensed
plumbing and electrical businesses; OSM maps a rounding error of them, because a
mobile trade business is not a place on a map.

Google Maps indexes those businesses, because they are the ones who create a
Google Business Profile in order to get called. Google Places API (New) exposes
`plumber`, `electrician`, `roofing_contractor`, `locksmith`, `painter` as
first-class filterable types (Table A), alongside `hotel`, `resort_hotel`,
`lodging` and the rest.

WHAT THIS DOES *NOT* CHANGE
The chain is still ONE chain, and this is still deterministic enumeration with
zero LLM tokens — the same discipline as the Overpass sweep, and it emits the
same candidate contract, so merge/fetch/extract/qualify are untouched. A fixture
opts in by naming this source in sourcing.json. What varies per run is still only
WHERE it looks (places.txt) and WHO it looks for (the target list).

THE ONE REAL CONSTRAINT: 20 RESULTS PER REQUEST, NO PAGINATION
Nearby Search (New) returns at most 20 places per call and has no page token
(Text Search paginates but caps at 60 total). So a single wide query has the
exact rank ceiling that killed the old DDG sourcing. The fix is geometry, not
ranking: cover the ground in circles small enough that each holds under 20
businesses of the type, and SUBDIVIDE any circle that comes back full. A circle
returning 20 is not "20 businesses here", it is "at least 20 and we are blind to
the rest" — so it splits into four and each quarter is asked again, down to
--min-radius. That makes the sweep exhaustive per city instead of rank-limited,
and it self-tunes: sparse rural cells cost one request, dense CBD cells spend
requests only where the businesses actually are.

COST (checked against the published pricing 2026-07-31)
The field mask decides the SKU, so this asks for exactly what the chain consumes.
  * Nearby Search Pro       — 5,000 free requests/month, then $32/1,000.
    Fields: places.id, places.displayName, places.primaryType.
  * Nearby Search Enterprise — 1,000 free requests/month, then $35/1,000.
    Adds places.websiteUri + places.nationalPhoneNumber.
At 20 results/request, Enterprise is ~$1.75 per 1,000 businesses WITH their
website and phone. Pro is free for up to 100,000 businesses/month but returns no
website, so those leads go through Stage 2.5 (resolve_domains.py) to have a
domain resolved and PROVEN — cheaper in dollars, lossy in yield.
`--tier pro` picks that trade-off; the default is enterprise.
Every run prints its request count and estimated cost, and `--max-requests`
is a hard ceiling that stops the sweep rather than surprising anyone with a bill.

SETUP
  1. Create a Google Cloud project, enable "Places API (New)", make an API key.
  2. Put it in project/.env as   GOOGLE_PLACES_API_KEY=...
  3. Restrict the key to the Places API.
Missing key = loud abort with these instructions, never a silent fallback.

LEDGER
Fired cities are recorded in the shared city ledger under the vertical
`places:<vertical>`, deliberately NOT the same key the OSM sweep uses. Ground
that OSM has already exhausted is fresh ground for this source (that is the
entire point), so the two must not retire each other's cities. Domain-level
dedup at merge still guarantees nobody is contacted twice.

OUTPUT  <run-dir>/candidates-batch-<prefix>-NNN.txt   `domain|Name|ISO2|vertical|0`
        <run-dir>/unresolved-<prefix>-NNN.txt         `Name|ISO2|City|vertical`
"""
from __future__ import annotations
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
CITY_LEDGER = PROJECT / "vault" / "lead-outreach" / "overpass-cities-fired.txt"
SENT_LOG = PROJECT / "vault" / "lead-outreach" / "sent-log.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_candidates as _mc                     # shared dedup nets
from source_overpass import (load_places_file, _domain_from_url,
                             ledger_city, load_city_ledger)

ENDPOINT = "https://places.googleapis.com/v1/places:searchNearby"

# Field masks per SKU tier. The mask is what Google bills on, so this is the
# cost dial — never request a field the chain does not consume.
MASKS = {
    "pro": "places.id,places.displayName,places.primaryType",
    "enterprise": ("places.id,places.displayName,places.primaryType,"
                   "places.websiteUri,places.nationalPhoneNumber"),
}
FREE_REQUESTS = {"pro": 5000, "enterprise": 1000}
COST_PER_1K = {"pro": 32.00, "enterprise": 35.00}


def resolve_tier(cli_tier: str, source_tier: str | None) -> str:
    """Which Places API (New) field mask this sweep requests.

    Defaults to "pro" (id + displayName + primaryType only), NOT "enterprise".
    "pro" never requests websiteUri/nationalPhoneNumber, so the field the Places
    API ToS treats most restrictively (Content storage: the ToS permits storing
    only the place ID indefinitely; website/phone are not storage-eligible) is
    simply never in the response to begin with — there is nothing to persist and
    nothing to be non-compliant about. Every "pro" result without a resolvable
    domain lands in unresolved-*.txt, which Stage 2.5 (resolve_domains.py)
    independently resolves via search + fetch + name-token PROOF against the
    business's own site — a domain this pipeline verified itself, not one taken
    from Google's response. This is also better sourcing, not just safer
    sourcing: resolve_domains.py's own measurement (2026-07-27, 15 Riyadh
    clinics) found trusting a single field's first result is right ~2/15 times,
    which is why every other weakly-mapped branch already resolves-and-proves
    rather than trusts.

    "enterprise" is an explicit per-source opt-in ({"tier": "enterprise"} in
    sourcing.json) for a campaign that genuinely needs the phone field
    (resolve_domains.py only proves web domains, not phone numbers) or wants a
    website field without the resolution step's extra search+fetch. Choosing it
    is a real ToS/legal tradeoff the fixture author is making deliberately, not
    a default nobody looked at.
    """
    return cli_tier or source_tier or "pro"

M_PER_DEG_LAT = 111_320.0
FULL_PAGE = 20          # Nearby Search (New) hard cap; 20 back == saturated cell


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Budget:
    """Hard request ceiling + running cost estimate."""

    def __init__(self, tier: str, max_requests: int):
        self.tier = tier
        self.max = max_requests
        self.n = 0

    def spend(self) -> bool:
        if self.n >= self.max:
            return False
        self.n += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.n >= self.max

    def report(self) -> str:
        billable = max(0, self.n - FREE_REQUESTS[self.tier])
        cost = billable / 1000.0 * COST_PER_1K[self.tier]
        return (f"{self.n} requests ({self.tier} SKU; "
                f"{FREE_REQUESTS[self.tier]}/mo free -> "
                f"~${cost:.2f} if the free tier is already spent)")


def search_nearby(key: str, lat: float, lon: float, radius_m: float,
                  included_type: str, tier: str,
                  budget: Budget) -> list[dict] | None:
    """One circle. Returns places, or None if the call failed after retries
    (caller must NOT treat a failure as an empty cell — that would silently
    retire ground)."""
    if not budget.spend():
        return None
    body = json.dumps({
        "includedTypes": [included_type],
        "maxResultCount": FULL_PAGE,
        "locationRestriction": {
            "circle": {"center": {"latitude": lat, "longitude": lon},
                       "radius": float(radius_m)}
        },
    }).encode()
    last = "unknown"
    for attempt in range(3):
        try:
            req = urllib.request.Request(ENDPOINT, data=body, headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": MASKS[tier],
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("places", [])
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except Exception:
                pass
            last = f"HTTP {e.code} {detail}"
            # Config errors will not fix themselves on retry — fail fast and loud.
            if e.code in (400, 401, 403):
                print(f"    ABORT-WORTHY: {last}", file=sys.stderr)
                return None
            time.sleep(2 + attempt * 3)
        except Exception as e:
            last = str(e)[:150]
            time.sleep(2 + attempt * 3)
    print(f"    WARN: circle failed after retries ({last})", file=sys.stderr)
    return None


def cover_bbox(lat: float, lon: float, half_deg: float,
               radius_m: float) -> list[tuple[float, float]]:
    """Circle centers tiling the city bbox. Spacing is radius*sqrt(2) so the
    inscribed squares tile without gaps (circles overlap slightly, which is
    correct — a gap loses businesses, an overlap only costs dedup)."""
    step_m = radius_m * math.sqrt(2)
    dlat = step_m / M_PER_DEG_LAT
    dlon = step_m / (M_PER_DEG_LAT * max(0.15, math.cos(math.radians(lat))))
    out = []
    n_lat = max(1, int(math.ceil(2 * half_deg / dlat)))
    n_lon = max(1, int(math.ceil(2 * half_deg / dlon)))
    for i in range(n_lat):
        for j in range(n_lon):
            out.append((lat - half_deg + (i + 0.5) * (2 * half_deg / n_lat),
                        lon - half_deg + (j + 0.5) * (2 * half_deg / n_lon)))
    return out


def sweep_outcome(total: int, total_unresolved: int, cities_swept: int,
                  skipped: int) -> tuple[int, str]:
    """Decide how a finished places sweep should exit. -> (exit_code, message).

    exit 0 = produced yield; exit 8 = no fresh ground (an expected end state, so
    a run's other sources still carry it); exit 1 = anomalous, halt loudly.

    The distinction that matters: yield is domain-rows PLUS name-only rows. On
    the default Pro tier `total` is ALWAYS 0 because websiteUri is never
    requested, so judging yield by `total` alone would hard-abort every
    successful Pro sweep as "Google returned 0 businesses" and kill the run.
    """
    if total or total_unresolved:
        return 0, ""
    if cities_swept == 0:
        return 8, ("NO FRESH GROUND: every requested city is already swept for every "
                   "places target. Add rows to places.txt, add targets, or pass "
                   "--ignore-ledger.")
    if skipped:
        return 8, (f"NO FRESH GROUND: swept {cities_swept} city/target unit(s); all "
                   f"{skipped} businesses found were already sourced or contacted.")
    return 1, (f"ABORT: swept {cities_swept} city/target unit(s) and Google Places "
               f"returned 0 businesses. Check the API key's restrictions and that "
               f"the included_type values are valid Places API (New) Table A types.")


def places_ledger_key(vertical: str, included_type: str) -> str:
    """City-ledger key for one places sweep unit.

    Namespaced under `places:` so a Places sweep never inherits the OSM sweep's
    exhausted cities (the two enumerate different universes of the same vertical).

    Keyed per (vertical, included_type), NOT per vertical: the sweep unit is one
    Google place type in one city, so `plumber` in Sydney is a different
    enumeration from `electrician` in Sydney and retires independently. Under a
    vertical-only key the FIRST target ledgered a city and every later target
    sharing that vertical skipped it as "already swept", so a four-target source
    really only swept its first target.
    """
    return f"places:{vertical}:{included_type}"


def sweep_city(key: str, city: str, meta: tuple, target: dict, tier: str,
               budget: Budget, seen_ids: set[str], blocked_domains: set[str],
               blocked_slugs: set[str], start_radius_m: float,
               min_radius_m: float, verbose: bool = True,
               ) -> tuple[list[str], list[str], int, bool]:
    """Adaptive quadtree sweep of one city for one place type.

    -> (candidate_lines, unresolved_lines, n_already_seen, complete)
    `complete` is False when the request budget ran out mid-city or a cell was
    still saturated at --min-radius, i.e. the city is NOT exhausted and must not
    be ledgered.
    """
    country, lat, lon, half = meta
    vertical = target["vertical"]
    itype = target["included_type"]
    lines: list[str] = []
    unresolved: list[str] = []
    already = 0
    complete = True
    # work queue of (lat, lon, radius)
    queue = [(clat, clon, start_radius_m)
             for clat, clon in cover_bbox(lat, lon, half, start_radius_m)]
    while queue:
        if budget.exhausted:
            complete = False
            break
        clat, clon, r = queue.pop()
        places = search_nearby(key, clat, clon, r, itype, tier, budget)
        if places is None:
            complete = False          # unknown ground, do not retire the city
            continue
        for p in places:
            pid = p.get("id") or ""
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            name = ((p.get("displayName") or {}).get("text") or "").strip()
            if not name:
                continue
            domain = _domain_from_url((p.get("websiteUri") or ""))
            if not domain:
                # Name-only (Pro tier, or a business with no site on its profile)
                # -> Stage 2.5 resolves + PROVES a domain, or drops it.
                unresolved.append(f"{name.replace('|', ' ')}|{country}|{city}|{vertical}")
                continue
            norm = _mc._norm_domain(domain)
            if norm in blocked_domains or _mc._slugify(norm) in blocked_slugs:
                already += 1
                continue
            if any(ln.startswith(domain + "|") for ln in lines[-50:]):
                continue
            lines.append(f"{domain}|{name.replace('|', ' ')}|{country}|{vertical}|0")
        # SATURATED CELL -> subdivide. A full page means there are more results
        # here than the API will show, so this cell has NOT been enumerated.
        if len(places) >= FULL_PAGE:
            half_r = r / 2.0
            if half_r >= min_radius_m:
                off_lat = (half_r / M_PER_DEG_LAT)
                off_lon = (half_r / (M_PER_DEG_LAT
                                     * max(0.15, math.cos(math.radians(clat)))))
                for dla in (-off_lat, off_lat):
                    for dlo in (-off_lon, off_lon):
                        queue.append((clat + dla, clon + dlo, half_r))
                if verbose:
                    print(f"      cell {clat:.3f},{clon:.3f} r={int(r)}m full "
                          f"-> split into 4 x {int(half_r)}m")
            else:
                # Still dense at the floor: real businesses remain unseen here.
                complete = False
                if verbose:
                    print(f"      cell {clat:.3f},{clon:.3f} still full at the "
                          f"{int(min_radius_m)}m floor — city stays open")
    return lines, unresolved, already, complete


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--source", default="", help="the source object as JSON (from sourcing.json)")
    ap.add_argument("--batch-prefix", default="places")
    ap.add_argument("--max-candidates", type=int, default=350)
    ap.add_argument("--max-requests", type=int, default=0,
                    help="hard ceiling on API calls this run (default: the tier's free monthly allowance)")
    ap.add_argument("--tier", default="", choices=["", "pro", "enterprise"],
                    help="field mask. Default (unset here and in sourcing.json) is "
                         "'pro': see resolve_tier() for why that's the default now.")
    ap.add_argument("--cities", default="")
    ap.add_argument("--ignore-ledger", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan the sweep (cities, cells, request estimate) without calling the API")
    a = ap.parse_args()

    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    run_slug = run.resolve().name
    src = json.loads(a.source) if a.source else {}

    tier = resolve_tier(a.tier, src.get("tier"))
    start_radius = float(src.get("start_radius_m", 12000))
    min_radius = float(src.get("min_radius_m", 700))
    max_req = a.max_requests or int(src.get("max_requests", FREE_REQUESTS[tier]))
    sleep_s = float(src.get("sleep", 0.12))

    # --- targets: which Google place types, and the vertical they map to ------
    targets = []
    for t in (src.get("targets") or []):
        it = t.get("included_type") or t.get("type")
        if not it or not t.get("vertical"):
            sys.exit('ABORT: each places target needs "included_type" and "vertical", '
                     'e.g. {"included_type": "plumber", "vertical": "trades"}')
        targets.append({"included_type": it, "vertical": t["vertical"]})
    if not targets:
        sys.exit('ABORT: places source declares no "targets". Example:\n'
                 '  {"type": "places", "targets": ['
                 '{"included_type": "plumber", "vertical": "trades"}]}\n'
                 '  Filterable types include plumber, electrician, roofing_contractor,\n'
                 '  locksmith, painter, hotel, resort_hotel, lodging.')

    places_file = run / "places.txt"
    if not places_file.exists():
        sys.exit("ABORT: places source needs a places.txt in the run/fixture "
                 "(`City|ISO2|lat|lon|half_width_deg`, one per line).")
    places = load_places_file(places_file)

    key = ""
    if not a.dry_run:
        env = {**load_env(PROJECT / ".env"), **os.environ}
        key = env.get("GOOGLE_PLACES_API_KEY", "").strip()
        if not key:
            # Exit 8 = "this source has no ground it can open", the same contract
            # source_overpass.py uses when its cities are all swept. An unconfigured
            # source is not a degraded campaign: nothing is swapped, nothing is
            # guessed, and no ground is consumed. run_fire.py records it in
            # dry_sources, so it lands in status.txt and the final report instead of
            # killing a fixture whose OTHER sources (or backlog) can still deliver.
            # Fixtures can therefore declare a places source before the key exists
            # and start producing the moment it is added, with no further edits.
            print(
                "NO PLACES KEY: GOOGLE_PLACES_API_KEY is not set, so this source "
                "opened no ground.\n"
                "  1. Google Cloud console -> enable 'Places API (New)'\n"
                "  2. create an API key, restrict it to the Places API\n"
                "  3. add to project/.env:  GOOGLE_PLACES_API_KEY=...\n"
                "Nothing was sourced and no ground was consumed — re-fire once it is set.",
                file=sys.stderr)
            sys.exit(8)

    print(f"Places config: tier={tier} targets="
          f"{[t['included_type'] for t in targets]} places={len(places)} "
          f"start_radius={int(start_radius)}m min_radius={int(min_radius)}m "
          f"max_requests={max_req}")

    blocked_domains = _mc.load_sent_domains(SENT_LOG) | \
        _mc.load_sourced_domains(_mc.sourced_log_path(SENT_LOG), run_slug)
    blocked_slugs = _mc.load_sent_slugs(SENT_LOG)
    print(f"Fresh-only cap: {len(blocked_domains)} previously-seen domains excluded at source.")

    budget = Budget(tier, max_req)
    total = 0               # rows carrying a domain already (Enterprise tier only)
    total_unresolved = 0    # name-only rows for Stage 2.5 to resolve+prove
    batch_n = 0
    skipped = 0
    cities_swept = 0        # how many (target, city) units actually hit the API
    seen_ids: set[str] = set()
    wanted = ([c.strip() for c in a.cities.split(",") if c.strip()]
              if a.cities else list(places.keys()))
    unknown = [c for c in wanted if c not in places]
    if unknown:
        sys.exit(f"ABORT: unknown cities (not in places.txt): {unknown}")

    if a.dry_run:
        for t in targets:
            for city in wanted:
                cells = len(cover_bbox(*places[city][1:3], places[city][3], start_radius))
                print(f"  [plan] {city:22} {t['included_type']:20} "
                      f"{cells:4} starting cells (before any subdivision)")
        print("\n[plan] no API calls made. Dense cells subdivide x4, so real usage "
              "is higher than the starting-cell count.")
        return

    for t in targets:
        vkey = places_ledger_key(t["vertical"], t["included_type"])
        fired = set() if a.ignore_ledger else load_city_ledger(vkey)
        todo = [c for c in wanted if c not in fired]
        if not todo:
            print(f"  {t['included_type']}: every city already swept for "
                  f"'{vkey}' — add places.txt rows or pass --ignore-ledger.")
            continue
        for city in todo:
            # The cap counts BOTH kinds of yield. On the default Pro tier `total`
            # is always 0 (no websiteUri is ever requested), so capping on `total`
            # alone meant --max-candidates never tripped and the sweep ran every
            # city for every target, ignoring the fixture's share of the budget.
            if (total + total_unresolved) >= a.max_candidates or budget.exhausted:
                break
            print(f"  sweeping {city} ({places[city][0]}) for {t['included_type']}…")
            cities_swept += 1
            lines, unres, already, complete = sweep_city(
                key, city, places[city], t, tier, budget, seen_ids,
                blocked_domains, blocked_slugs, start_radius, min_radius)
            skipped += already
            batch_n += 1
            if lines:
                (run / f"candidates-batch-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(lines) + "\n")
            if unres:
                (run / f"unresolved-{a.batch_prefix}-{batch_n:03d}.txt"
                 ).write_text("\n".join(unres) + "\n")
            total += len(lines)
            total_unresolved += len(unres)
            state = "complete" if complete else "INCOMPLETE (not ledgered)"
            print(f"    {city}: {len(lines)} fresh, {len(unres)} name-only, "
                  f"+{already} already-seen — {state}; "
                  f"running total {total + total_unresolved}")
            if complete:
                ledger_city(vkey, city, run_slug)
            time.sleep(sleep_s)

    print(f"\nPlaces sourcing: {total} with a domain + {total_unresolved} name-only "
          f"(Stage 2.5 resolves+proves those) -> {run}")
    print(f"  {budget.report()}")
    if skipped:
        print(f"  {skipped} already-seen domains skipped at source.")
    if total_unresolved and not total:
        print(f"  Pro tier: no websiteUri is requested, so every row is name-only "
              f"by design. resolve_domains.py converts these into candidates.")
    if budget.exhausted:
        print(f"  NOTE: stopped at the {max_req}-request ceiling. Cities left open "
              f"are not ledgered and the next fire resumes them.")
    code, msg = sweep_outcome(total, total_unresolved, cities_swept, skipped)
    if code:
        print(msg, file=sys.stderr)
        sys.exit(code)


if __name__ == "__main__":
    main()
