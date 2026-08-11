# Campaign templates — the FIXED source of truth

Each folder here is the **canonical config for one campaign**. A fire trigger
clones `templates/<base>/` into a new dated `runs/<date>-<base>/` and launches it.

**The orchestrator reads ONLY from here — never from previous runs.** This is
deliberate: firing is direct and reproducible, with no "look at the latest run to
figure out the config" step. To change a campaign (queries, ICP, voice, channels,
qualify gate), **edit the fixture in this folder** — not a past run under `runs/`.

| Fixture | Campaign | Draft mode | Channels |
|---|---|---|---|
| `us-law-firms/` | US law firms | custom | email |
| `us-staffing/` | US staffing/recruiting | custom | email |
| `us-clinics/` | US med spas / dental / clinics | custom | email |
| `us-property/` | US property management | custom | email |
| `gcc-auto/` | GCC consumer chains | template | email |
| `eu-hotels/` | Big hotels, 31-country EU+ footprint (AI receptionist) | template | email |
| `lb-enterprise/` | Biggest Lebanese companies | custom | whatsapp |
| `lb-receptionist/` | Lebanon phone-heavy SMBs | template | email+whatsapp |
| `worldwide-receptionist/` | Worldwide AI receptionist | template | email |
| `au-trades/` | AU emergency trades (plumbing/electrical/HVAC/hot water) — AI receptionist | template | email |
| `lb-fintech-compliance/` | Lebanon money transfer / fintech / bank compliance depts (AML/KYC angle) | template | email |
| `lb-supermarkets/` | Lebanon supermarket & food-retail chains (ordering/forecasting angle) | template | email |
| `lb-insurance-tpa/` | Lebanon insurers, TPAs & hospital finance offices (claims-processing angle) | template | email |
| `lb-fmcg-distributors/` | Lebanon FMCG distributors & industrial groups (rep/collections angle) | template | email |
| `lb-construction/` | Lebanon construction/engineering/design consultancies (tendering angle) | template | email |
| `lb-restaurants-hotels/` | Lebanon restaurants, hotels & beach clubs (inbox/DM angle) | template | email |
| `lb-ngos/` | Lebanon NGOs & international organizations (donor-reporting angle) | template | email |

## THE general fixture structure (one chain — a new campaign is ONLY a new folder)

Every fixture has the SAME file set with the same meaning; the pipeline never
grows a campaign-specific path. Adding a new campaign type = adding one small
branch to this tree (a `templates/<base>/` folder), zero code changes:

```
templates/<base>/
  icp.yaml         REQUIRED  target definition (the human-readable WHAT)
  sourcing.json    REQUIRED  the Stage-2 sourcing contract (the machine HOW):
                             {"method":"map","selector":"\"office\"=\"lawyer\"",
                              "vertical":"law","max_per_run":350}
  countries.txt              ISO-2 country filter for merge (absent → GCC default)
  draft_mode.txt             template | custom            (absent → template)
  channels.json              ["email"] and/or "whatsapp"  (absent → email)
  pitch.json                 REQUIRED for template mode — the user-approved copy
  vertical.txt               REQUIRED for custom mode
  qualify.json               optional per-run qualify gate (e.g. hotel size/volume)
  target_roles.txt           optional: comma-separated priority job titles for
                             Stage 5.5 (e.g. "Head of Compliance, MLRO, COO")
                             when the decision-maker isn't the generic
                             founder/CEO/owner/GM — appended to every
                             name-finder dispatch as a `TargetRoles:` line
  places.txt                 map method, optional: City|ISO2|lat|lon|half_width
                             (absent → built-in 195-city EU table)
  enrich_cap.txt             optional top-N override for Stage 5.3
```

The routing phrase → fixture map lives in `../CLAUDE.md` (plus a generic rule:
naming any existing fixture base fires it — no table edit needed). The whole
fixture is cloned into a fresh dated `runs/` folder on every fire.

## Creating a new campaign (one command)

```bash
python3 tools/scripts/new_campaign.py --base us-hvac --vertical hvac \
    --countries US --selector '"shop"="hvac"'
```

This writes the complete structure above and prints the exact remaining
blockers (approved `pitch.json` copy, real places, ICP criteria).
Firing a half-finished fixture always aborts loudly naming the missing file —
the chain treats every campaign identically, whatever its channels, draft mode,
or geography.

## Query rotation (keeping lead volume up)

Every fire drops previously-contacted domains (forever) and recently-sourced
domains (90-day window, `SOURCED_SKIP_DAYS`). DuckDuckGo returns roughly the
same top results for the same query, so re-firing a query an earlier run
already fired searches ground where every domain is already blocked.

The pipeline handles this by SCOPING, not by asking: each run claims only the
queries this campaign has never fired (ledger:
`vault/lead-outreach/queries-fired-log.txt`) and searches exactly those. You
never need to think about it — until the pool is spent, at which point the fire
ABORTS (exit 6) with `query pool exhausted`. That is the signal to **extend the
fixture's queries**: new companies/cities/regions, new sub-vertical phrasings,
different qualifiers. Append them; never edit existing lines, since a changed
line reads as a brand-new query.

Entity-enumeration fixtures (e.g. `lb-insurance-tpa`, one query per named
company) enumerate a FINITE universe, so they exhaust after roughly one full
fire. Broad-phrasing fixtures (`au-trades`, `eu-hotels`) last much longer.
Evidence this works: the 2026-06-24 eu-hotels retries overlapped 42-46% on the
same queries, and dropped to 5% overlap once queries were diversified.

## Choosing the sourcing method in sourcing.json

Sourcing is deterministic enumeration for EVERY campaign — no agents, no tokens.
The agent-per-query `"search"` method was retired 2026-08-05 along with its
`source-agent-*` definitions; a poorly-mapped vertical now reaches for a
`places` source (below) rather than a fan-out of WebSearch agents.

- **`"map"`** — deterministic OSM enumeration via `source_overpass.py`:
  exhaustive per city, region by region, zero LLM tokens, a per-vertical
  fired-city ledger (`vault/lead-outreach/overpass-cities-fired.txt`) so
  every fire covers fresh ground. Right choice when the vertical is well
  mapped (hotels everywhere; US law firms probed at 44/45 with websites).
  Probe coverage with one Overpass query before committing a new vertical.

## `sources`: the source ladder (pick the index that actually holds your ICP)

`selector`/`targets` alone is shorthand for a single `map` source. A fixture that
needs more declares them explicitly, and they run in order:

```json
{"sources": [
   {"type": "places",
    "tier": "enterprise",
    "start_radius_m": 12000, "min_radius_m": 700, "max_requests": 1000,
    "targets": [{"included_type": "plumber",  "vertical": "trades"},
                {"included_type": "electrician", "vertical": "trades"}]},
   {"type": "map",
    "targets": [{"selector": "\"craft\"~\"plumber|electrician\"", "vertical": "trades"}]},
   {"type": "directory",
    "url": "https://example.org/members?page={page}", "pages": 20,
    "name": "<h3[^>]*>(.*?)</h3>", "link": "href=\"(https?://[^\"]+)\"",
    "country": "AU", "vertical": "trades"}
 ]}
```

| type | index it reads | use it when |
|---|---|---|
| `map` | OpenStreetMap via Overpass | the ICP is **premises-bound** and well mapped (hotels, clinics, law offices). Free, exhaustive per city, 0 tokens. |
| `places` | Google Places API (New) | the ICP has **no premises** or OSM is thin — mobile trades, home services, anything that lives on a Google Business Profile. |
| `directory` | any paginated web listing | the authoritative list is a register or association roster (licensing boards, member directories). |

**Why `places` exists — measured, not assumed (2026-07-31).** A live Overpass
probe of the *entire Australian continent* for `craft~plumber|electrician|hvac`
returned **450 elements, 438 named, 159 with a website tag**. That is the national
ceiling, and au-trades had consumed it: 16/16 metros ledgered and fires decaying
51 → 4 → 20 → 5 fresh candidates. OSM maps places; a mobile trade business is not
a place. Google indexes those businesses because they create a Business Profile in
order to get called, and `plumber`, `electrician`, `roofing_contractor`,
`locksmith`, `painter` are first-class filterable Places types.

**The 20-result cap and why the sweep is a quadtree.** Nearby Search (New) returns
at most 20 places per call with no pagination, so a wide query has the same rank
ceiling that killed DDG sourcing. `source_places.py` beats it with geometry, not
ranking: it tiles each city in circles and **subdivides any circle that comes back
with a full 20** (a full page means "at least 20 and we are blind to the rest"),
down to `min_radius_m`. Sparse cells cost one request; dense CBDs spend requests
only where the businesses are. A city that is still saturated at the floor, or cut
short by the request ceiling, is deliberately **not ledgered** — the next fire
resumes it.

**Cost.** The field mask picks the SKU. `tier: "enterprise"` returns
`websiteUri` + `nationalPhoneNumber` — 1,000 free requests/month, then $35/1,000,
i.e. **~$1.75 per 1,000 businesses with website and phone** at 20 results a call.
`tier: "pro"` is 5,000 free requests/month (~100,000 businesses free) but returns
no website, so pair it with `"resolve_domains": true` and accept Stage 2.5's
lossy-but-proven domain resolution. Every run prints its request count and cost
estimate; `max_requests` is a hard stop. Needs `GOOGLE_PLACES_API_KEY` in
`project/.env` — a missing key is a loud abort with setup instructions, never a
silent fallback.

The `places` city ledger is keyed `places:<vertical>:<included_type>`, deliberately
separate from the OSM `<vertical>` key: ground OSM has exhausted is fresh ground
here, so the two sources must not retire each other's cities. Domain dedup at
merge still guarantees nobody is contacted twice.

### Places sources: the three rules every fixture's places block must follow

Every campaign carries a `places` source as of 2026-08-10, because OSM indexes
PREMISES and the map ground goes stale — `eu-hotels` had swept 279/279 of its
cities and `au-trades` 16/16, so both were firing zero candidates. These three
rules are what keep ~20 simultaneous places sources safe. Read them before adding
or editing one.

**1. `included_type` MUST be a Places API (New) TABLE A value.** An invalid type
is not a soft skip: the sweep returns nothing and `sweep_outcome()` hard-aborts
the run. Verify against Google's live Table A docs, not memory. The trap that
caught this project: **`general_contractor` is TABLE B only** — it is real in
Google's taxonomy but invalid as an `included_type`, so a construction fixture
using it would break on every fire. Types with no Table A equivalent at all
(HVAC, pest control, counselling/psychotherapy, language/driving schools beyond
the generic `school`) simply cannot be sourced this way; use the closest honest
type or leave that sub-vertical to OSM, and say so in the fixture's `_comment`.

**2. `max_requests` is a HARD per-fixture cap, and it exists for money.**
`FREE_REQUESTS["pro"]` is 5,000 — but that is Google's **monthly allowance for
the whole Cloud project** behind the single `GOOGLE_PLACES_API_KEY`, not a
per-fixture or per-run budget. `source_places.py` defaults an unset
`max_requests` to that full 5,000, and each fixture's own `Budget.report()` is
blind to what every other fixture already spent that month. With ~20 fixtures
re-firing on this project's normal cadence (re-fires are routine — see
`../CLAUDE.md`), leaving it unset would silently cross into $32/1,000 billing.
Every fixture therefore pins **`"max_requests": 150`**, which keeps a full sweep
of all campaigns inside one month's free allowance with headroom for re-fires.
Raise it only against the whole-project monthly total, never per campaign in
isolation.

**3. The places `vertical` MUST be fixture-scoped.** `places_ledger_key()` writes
`places:<vertical>:<included_type>` into the **single global**
`vault/lead-outreach/overpass-cities-fired.txt` with no campaign scoping. Two
fixtures sharing a bare vertical name will retire each other's cities — a silent
cross-campaign supply collapse with no connection to the affected campaign's own
history. This is not hypothetical: `us-clinics`, `lb-receptionist`,
`gcc-receptionist` and `worldwide-receptionist` all use the vertical `clinic`,
and `New York`/`Los Angeles` appear in more than one `places.txt`. So use
`clinic-us`, `clinic-lb`, `clinic-gcc`, `clinic-ww` — never bare `clinic`. The
map source keeps its original bare vertical, since its ledger key is built
differently and existing OSM ground must not be re-swept.

A places source is safe to ship before the API key exists: without one the source
reports no fresh ground (exit 8) and the run simply continues on its other
sources.

## Template-first rule (new campaigns)

A **new campaign fixture is not fire-ready until its email copy is
user-approved.** For `draft_mode=template` (the default), that means a
`pitch.json` whose `subject_template`/`body_template` the user either supplied
or explicitly confirmed from proposed examples (see the "New-campaign contract"
in `../CLAUDE.md`). The system never invents copy at fire time; per-business
variation happens only through template slots (`{opener}` via `signal_openers`,
`{name}`, `{salutation}`, `{vertical}`, `{country}`). `draft_mode=custom`
(freeform per-business writing) is opt-in only, on the user's explicit request.
