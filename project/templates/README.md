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
| `gcc-receptionist/` | GCC AI receptionist — Riyadh-first owner-operated clinics, salons, brokerages, home services, restaurants, car service | template | email |
| `gcc-outreach/` | GCC outreach-system — Dubai/Sharjah real-estate brokerages first, then education + insurance | template | email |
| `gcc-outreach-li/` | Same GCC outreach ICP, LinkedIn arm — queues invites + custom DMs, sends nothing during the fire (`linkedin` block in campaign.json) | template (DMs are per-person) | linkedin |
| `li-handoff/` | **Not a fire fixture.** `<brief>.pitch.json` angles read by li-writer when `./linkedin-run handoff <brief>` queues li-search's delivered people | — | linkedin |
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
| `gcc-agencies/` | GCC independent marketing agencies (digital-employee angle) | template | email |

## THE fixture contract: five files (one chain — a new campaign is ONLY a new folder)

Every fixture has the SAME file set with the same meaning; the pipeline never
grows a campaign-specific path. Adding a new campaign type = adding one small
branch to this tree (a `templates/<base>/` folder), zero code changes:

```
templates/<base>/
  campaign.json    REQUIRED  every knob, one file (schema below)
  icp.yaml         REQUIRED  target definition (the human-readable WHAT)
  pitch.json       REQUIRED for template mode with an email or linkedin channel —
                             the user-approved copy (TEMPLATE-FIRST, below)
  places.txt       REQUIRED whenever sourcing uses gmaps / overture / places:
                             City|ISO2|lat|lon|half_width_deg, one per line
                             (a map-only fixture may rely on `countries` + the
                             built-in EU city table instead)
  qualify.json               optional per-campaign qualify gate (e.g. hotel size)
```

Only these files are fixture files. Research notes (`brief.md`, `targeting.md`)
are not read by any stage and do not belong in a fixture; free prose about the
campaign goes in `campaign.json` → `notes`.

**How it reaches the stages.** No stage script reads `campaign.json`. On every
fire `fire_campaign.sh` runs `tools/scripts/campaign_config.py --validate` (one
schema, one ABORT line per problem) and then `--materialize runs/<slug>`, which
writes the per-file layout the stages have always read (`sourcing.json`,
`channels.json`, `draft_mode.txt`, …) into the fresh run folder, copies the four
content files verbatim, and copies `campaign.json` itself so the run records its
source. A knob at its default is simply not written. So a run folder stays
self-documenting and byte-identical to what the old per-file fixtures produced.
A fixture that still has the legacy per-file layout (no `campaign.json`) loads
the same way; `tools/scripts/migrate_fixture.py <base>` converts it.

### campaign.json schema (`campaign_config.SCHEMA`)

| key | type | default | materialised as | read by |
|---|---|---|---|---|
| `channels` | list of `email` · `whatsapp` · `whatsapp-fallback` · `linkedin` · `linkedin-email-lookup` | `["email"]` | `channels.json` | `run_fire.py` channel arms |
| `draft_mode` | `template` \| `custom` | `template` | `draft_mode.txt` | `run_fire.py` Stage 6 drafter choice |
| `vertical` | string \| null — **required iff** `draft_mode=custom` | `null` | `vertical.txt` | fire validation |
| `countries` | list of ISO-2 (or `"*"` = worldwide) | `[]` (→ GCC default / icp geography) | `countries.txt` | `merge_candidates.py` country filter; `source_overpass.py` built-in table |
| `enrich_cap` | int \| null | `null` (chain default 400) | `enrich_cap.txt` | `run_fire.py` → `ENRICH_MAX_LEADS` for `qualify_leads.py` |
| `target_roles` | list of job titles | `[]` | `target_roles.txt` (one comma-joined line) | `enrich_contact_person.py` → `TargetRoles:` line per name-finder batch |
| `fetch_pages` | list of `slug\|path` | `[]` (built-in page list) | `fetch_pages.txt` | `fetch_html.py load_pages()` |
| `linkedin` | object \| null — **required iff** `linkedin` ∈ channels | `null` | `linkedin.json` | `run_fire.py` → `qualify_people.py --config`; `li_handoff.py` |
| `li_max_per_company` | int \| null | `null` (chain default 3) | `li_max_per_company.txt` | `run_fire.py` Stage 8.6 walk |
| `li_max_people` | int \| null | `null` (chain default 60) | `li_max_people.txt` | `run_fire.py` Stage 8.6 / 8.6b |
| `sourcing` | object — **required**; the exact Stage-2 contract (`selector`+`vertical`, `targets`, or `sources`, see below) | — | `sourcing.json` | `run_fire.py resolve_sourcing()` → every `source_*.py`, `resolve_domains.py` |
| `notes` | list of strings | `[]` | **never** | nobody — fixture-specific facts and measurements |

`_note` / `_comment` keys inside `sourcing` or `linkedin` are stripped before
materialising; put that prose in `notes`. `campaign_config.py --fixture
templates/<base> --show` prints the resolved config.

The routing phrase → fixture map lives in `../CLAUDE.md` (plus a generic rule:
naming any existing fixture base fires it — no table edit needed).

## Creating a new campaign (one command)

```bash
python3 tools/scripts/new_campaign.py --base us-hvac --vertical hvac \
    --countries US --selector '"shop"="hvac"'
```

This writes `campaign.json` + `icp.yaml` (+ a `places.txt` stub when the
built-in city table does not cover the countries) and prints the exact remaining
blockers straight from the same `validate()` the fire runs (approved `pitch.json`
copy, real places, ICP criteria). Firing a half-finished fixture always aborts
loudly naming the missing piece — the chain treats every campaign identically,
whatever its channels, draft mode, or geography.

## Why the source ladder is ordered gmaps → overture → map → places

This used to be pasted verbatim into every fixture's `sourcing.json`; it is a
fact about the stack, not about any one campaign, so it lives here once.

- **`gmaps` first.** Google Maps enumerated directly via the gosom scraper binary
  (free, already installed by `tools/install.sh`). It is the highest-yield source
  measured on this stack: one Wollongong sweep over 5 trade terms returned 124
  candidates ALREADY carrying a domain in ~2 minutes, against 111 for the whole
  `2026-08-17-au-trades-1` run from every other source plus 73 minutes of Stage
  2.5. Terms and the category denylist come from `tools/scripts/gmaps_presets.json`
  via each target's vertical, so precision is shared by every campaign rather than
  restated per fixture. TRADEOFF, on purpose: scraping Maps is contrary to
  Google's ToS, unlike the paid `places` source. See `source_gmaps.py`.
- **`overture` second.** Overture Maps bulk POI (free, CDLA/Apache, no request
  ceiling), added 2026-08-12. Categories come from
  `tools/scripts/overture_presets.json` via the target's vertical. ~Half its rows
  already carry the business's own domain, so they enter the chain as candidates
  without Stage 2.5. See "Overture sources" below.
- **`map` = each campaign's original OSM config, unchanged**, and **`places` was
  added 2026-08-10** so a swept OSM ledger no longer means a zero-candidate fire.
  See "Places sources" below for the Table A / budget / ledger-scoping rules that
  govern every fixture's places block.

## Keeping lead volume up

Every fire drops previously-contacted domains (forever), sourced-but-never-
contacted domains (forever by default; `SOURCED_SKIP_DAYS` re-opens a window)
and retired domains, and each source walks its own city ledger
(`vault/lead-outreach/overpass-cities-fired.txt`, keyed per source and
vertical). So consecutive fires open fresh ground on their own, and a fire that
reports `0 candidates` is a supply signal: add `places.txt` rows, widen the
selector, or add a source — there is no query pool or query ledger to rotate.

## Choosing the sourcing method (`campaign.json` → `sourcing`)

Sourcing is deterministic enumeration for EVERY campaign — no agents, no tokens.
The agent-per-query `"search"` method was retired 2026-08-05 along with its
`source-agent-*` definitions; a poorly-mapped vertical now reaches for a
`places` source (below) rather than a fan-out of WebSearch agents.

- **`"gmaps"`** — the primary source: `source_gmaps.py` drives the
  `gosom/google-maps-scraper` binary (`tools/google-maps-scraper`, installed by
  `tools/install.sh`) and returns the business's own website AND its Maps
  category in one pass (measured 2026-08-18: 19/20 rows with a website versus
  the Places API's name-only rows). Config: `{"type":"gmaps","targets":[{"vertical":
  "trades","terms":["plumber","electrician"]}]}` over the fixture's `places.txt`
  cities; a target with no `terms` takes them from the shared
  `tools/scripts/gmaps_presets.json`. City ledger key: `gmaps:<vertical>`.
- **`"overture"`** — bulk open POI enumeration via `source_overture.py`. Free,
  keyless, no request ceiling, and roughly half its rows already carry the
  business's own domain. See "Overture sources" below.
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

Sources run concurrently (`SOURCE_WORKERS`, default 4); order in the list is
only the merge priority.

| type | index it reads | use it when |
|---|---|---|
| `gmaps` | Google Maps via the gosom scraper binary | **the primary source**: website + category per row, free, no key; anything with a Business Profile. |
| `overture` | Overture Maps bulk POI (parquet on S3) | bulk premises-or-profile ICPs. Free, no request ceiling, no key, ~half the rows carry the business's own domain. |
| `map` | OpenStreetMap via Overpass | the ICP is **premises-bound** and well mapped (hotels, clinics, law offices). Free, exhaustive per city, 0 tokens. |
| `places` | Google Places API (New) | the ICP has **no premises** or OSM is thin — mobile trades, home services, anything that lives on a Google Business Profile. |
| `directory` | any paginated web listing | the authoritative list is a register or association roster (licensing boards, member directories). |

### Overture sources: bulk POI, and why it now leads the ladder

Added 2026-08-12. `map` and `places` both starve in their own way — OSM only holds
what volunteers mapped, and Places is per-request metered with a 20-result cap
that forces a quadtree. `source_overture.py` is the third shape: a **bulk**
dataset queried in place with DuckDB over S3, so a whole city comes back in one
read. No key, no request ceiling, no per-call cost, and the licence
(CDLA Permissive 2.0 / Apache 2.0) permits commercial use *and* storage of the
name/website fields — unlike the Places Content rules that pin `places` to Pro
tier.

Measured on release `2026-07-22.0` when this source was built:

| probe | result |
|---|---|
| Riyadh bbox, all categories | **41,396 POIs in 32s, 21,823 (52.7%) with a website** |
| New York bbox, `law` preset | 6,586 law POIs |
| Sydney bbox, `trades` preset | 6,531 trades POIs |

That last row is the point. `au-trades` was documented as **nationally
exhausted** — an Overpass probe of the entire Australian continent returned 450
elements, 159 with a website, and all 16 metros were ledgered. Overture returns
more trades businesses in Sydney alone, with real `.com.au` domains.

**Config is just the vertical.** Categories come from the shared preset map at
`tools/scripts/overture_presets.json`, so no fixture hand-writes a category list:

```json
{"type": "overture",
 "targets": [{"vertical": "clinic"}, {"vertical": "salon"}]}
```

Lookup is `preset` → the vertical verbatim → the vertical's first segment, so
`clinic-gcc`, `hotel-eu` and `trades-emergency` all resolve to the right preset
under this repo's existing naming convention. A target may still override with
its own `"categories": [...]` (exact list) or `"category_match": "regex"`.

**Why presets are shared rather than per-fixture.** Overture ships 1,862 distinct
primary categories and hand-written regexes get them wrong invisibly — measured
while building this: an `ngo` regex matched `bingo_hall` and
`mongolian_restaurant`, `fintech` matched `food_banks`, `homeservices` matched
`movie_television_studio`, and a `salon` regex sourced Sephora, MAC and H&M
(cosmetics *retail*, not salons). The curated lists are derived from the real
taxonomy, validated to exist in the release, and inherited by every campaign.
Retailers, suppliers and schools-of-the-trade are deliberately excluded: a paint
store is not a painter.

Other options: `"release"` pins a release (default = newest discovered),
`"min_confidence"` drops low-confidence rows, `"include_closed"` keeps
`operating_status = closed`, and `"require_website": false` also emits the
name-only rows for Stage 2.5 (which auto-enables `resolve_domains`). The city
ledger is keyed `overture:<vertical>`, separate from the OSM and `places:` keys,
so the three sources never retire each other's ground.

**Dependency:** `duckdb` in `tools/venv` (added to `tools/install.sh`). Missing
it exits 8 — "this source opened no ground" — so the fire carries on with its
other sources rather than dying.

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
type or leave that sub-vertical to OSM, and say so in the fixture's `notes`.

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
