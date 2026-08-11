# Web-traffic signal as a qualification criterion (all campaigns)

**Date:** 2026-08-10
**Status:** Approved, ready for implementation planning
**Scope:** `project/tools/scripts/` — new `traffic_signals.py`, changes to `extract_leads.py` and `qualify_leads.py`, plus a new key in every `templates/<base>/qualify.json`.

## Problem

The pipeline spends its most expensive and least reliable stage — Stage 5.5 enrichment, one sub-agent per lead, capped at 250 (`ENRICH_MAX_LEADS`) — without any notion of how busy the target business actually is. A quiet three-room guesthouse and a 400-room resort consume an identical agent slot.

Measured across 2,133 leads that entered Stage 5.5 (11 `enrich-summary.json` files), 80% die at `no_match`, and bucketing 1,730 `agent_reason` strings, 77% of all drops are an email-availability failure. Enrichment slots are scarce and mostly wasted, so *which* leads get one is a high-leverage decision that is currently made without any demand signal.

The business rationale: a website a lot of people open means more customers, which means the missed-call pain the AI-receptionist offer solves is real. High traffic is a positive qualification signal.

### Constraint discovered during design

`qualify_leads.py` **never reads `icp.yaml`**. Across the whole tree, `icp.yaml` is consumed only by `merge_candidates.py` (one `geography:` line), `fire_campaign.sh` and `fire.md` (existence checks), and `gen_run_readme.py` / `new_campaign.py` (docs and scaffolding). No sub-agent reads it either.

Every criterion written into those ICP files is documentation that enforces nothing. Qualification honours exactly two config keys today, both hotel-only, read from `<run>/qualify.json` at `qualify_leads.py:81-88`:

```json
{"require_hotel_size_volume": true, "min_hotel_volume": "medium"}
```

Therefore a traffic criterion cannot be added by editing ICP files. It requires a real signal produced in `extract_leads.py` and a real gate in `qualify_leads.py`.

## Goals

1. Every campaign, with no per-fixture configuration, orders its enrichment queue so the busiest businesses get the scarce agent slots.
2. Each campaign can optionally set a minimum traffic floor that drops clearly-dead sites.
3. No new cost, no new paid API, no new network requests — the free-stack constraint holds.
4. No regression for the 20 fixtures that have no `qualify.json` today.

## Non-goals

- Public rank lists (Tranco, Majestic Million, Open PageRank, Cloudflare Radar). Explicitly declined: they are top-1M-only and cover few SMB targets.
- Paid traffic APIs (SimilarWeb, Semrush, Ahrefs). These break the free-only-stack constraint that Apify was removed for.
- Fixing the separately-confirmed bugs found during chain verification (see "Out of scope" below).

## Architecture

### Component 1 — `project/tools/scripts/traffic_signals.py` (new)

A self-contained, vertical-agnostic scoring module with one public function:

```python
detect_traffic(
    html_lower: str,
    pages_scanned: list[str],
    branches_estimate: int,
) -> tuple[str, int, str, list[str]]
```

Returns `(tier, score, evidence, signals)`:

- `tier` — `"high" | "medium" | "low" | "unknown"`
- `score` — `int` 0–100, for fine-grained ordering within a tier
- `evidence` — human-readable string, e.g. `"1,247 reviews; GA4+GTM+Meta Pixel; 8 pages; live chat"`
- `signals` — `list[str]` of machine-readable signal keys, for later threshold tuning

It reads only HTML the pipeline has already fetched into `raw_html/`. It performs no network I/O, imports no new third-party dependency, and is pure — same input always yields the same output. This matters: `extract_leads.py` already had a PYTHONHASHSEED nondeterminism bug in `pick_best` (11/279 leads changed `to_email` between re-extracts), so determinism is a hard requirement, not a nicety.

**Why a separate module rather than inline in `extract_leads.py`:** the existing `detect_hotel_volume` is inline and can only be exercised by running the whole extract stage against real fetched pages. A standalone pure function is unit-testable against fixture HTML strings.

#### Signal groups, strongest first

**A. Real customer counts (strongest).** A review count is a literal tally of people who transacted, and is the best free traffic proxy that exists for a small business.
- `schema.org` JSON-LD `AggregateRating.reviewCount` / `ratingCount`
- Microdata `itemprop="reviewCount"` / `itemprop="ratingCount"`
- Visible text patterns: `based on 1,247 reviews`, `4.8 ★ (892)`, and multilingual equivalents (`opiniones`, `avis`, `recensioni`, `Bewertungen`)

**B. Traffic-driving stack depth (strong).** Nobody installs a retargeting stack for a site nobody visits; paying to drive traffic is itself the signal. Count distinct vendors present:
- Google Analytics 4 (`gtag/js?id=g-`), Google Tag Manager (`gtm-`)
- Meta Pixel (`fbq(`, `connect.facebook.net/*/fbevents.js`)
- Google Ads conversion (`googleadservices`, `aw-`)
- Hotjar, Microsoft Clarity, Segment, Mixpanel, TikTok pixel, LinkedIn Insight

**C. Operational load (moderate).** Proxies for inbound volume forcing operational investment:
- Live-chat widget: Intercom, Tawk, Crisp, Drift, Zendesk, LiveChat, Tidio
- `hreflang` distinct language count — multi-language implies international demand
- `len(pages_scanned)` — already present on the lead record
- `branches_estimate` — already present on the lead record

#### Tiering

Thresholds are deliberately **not fixed in this spec**. They are to be derived from the calibration pass (see "Calibration" below) and recorded in the module with the measurement that justified them — matching the precedent set by `_WEAK_SOLO_KEYWORDS` in `extract_leads.py:113-121`, which documents the 27%-vs-47% enrichment hit-rate measurement behind it.

`unknown` is returned when there is too little HTML to judge — concretely, when the concatenated page text falls below a minimum length or no page was successfully fetched. `unknown` is a distinct state from `low` and is treated very differently at the gate.

### Component 2 — `extract_leads.py` (modified)

Add four fields to the lead dict built at `extract_leads.py:335-353`:

```python
"traffic_tier":     tier,        # high | medium | low | unknown
"traffic_score":    score,       # int 0-100
"traffic_evidence": evidence,    # str
"traffic_signals":  signals,     # list[str]
```

The change is purely additive. `qualify_leads.py:174` re-serialises each record verbatim, and all three drafters rebuild the record from scratch for the sender, so no downstream consumer breaks.

Call site: alongside the existing `detect_hotel_volume` call, using the same concatenated `html_lower` that `collect_html_for_domain` already produced. No extra file reads.

### Component 3 — `qualify_leads.py` (modified)

#### 3a. Ranking — automatic, every campaign, no configuration

> **AMENDMENT (2026-08-10, post-implementation):** the design below — traffic
> as the LEADING sort key — was the original plan and was **deliberately
> rejected** before ship. What actually shipped puts traffic 4th, BELOW
> `-score` and `CLASS_RANK`, as a tie-break only. See "Why this was reversed"
> below for the evidence. The original design is kept here, struck through in
> spirit, so a future reader can see what was tried and why it changed — not
> to describe current behaviour.

`qualify_leads.py:164-171` currently sorts ascending by:

```
(-VOLUME_RANK[hotel_volume] if != "na" else 0, -score, CLASS_RANK[email_class], -branches_estimate)
```

Originally-designed sort key (NOT shipped — see amendment above and "Why this
was reversed"):

```
(-TRAFFIC_RANK[traffic_tier],
 -VOLUME_RANK[hotel_volume] if != "na" else 0,
 -score,
 CLASS_RANK[email_class],
 -traffic_score,
 -branches_estimate)
```

with `TRAFFIC_RANK = {"high": 3, "medium": 2, "unknown": 1, "low": 0}`.

Two deliberate choices behind the original design:

- **Tier leads, but only at bucket granularity.** Traffic decides the coarse ordering so busy businesses get the 250 slots. Within a bucket the existing `score` still decides, preserving the email-class ordering (`person` 90 / `role` 82) that governs whether enrichment can succeed at all. Sorting purely by traffic would raise the value of each hit while lowering the hit rate; this ordering raises value without sacrificing rate.
- **`unknown` ranks above `low`, below `medium`.** A lead we failed to measure should not be punished as though it were measured and found quiet.

Because this is a sort-key change with no config, it takes effect for **all runs** immediately. **This last sentence did not hold** — see below.

#### What shipped instead

`qualify_leads.py` sorts (see `qualify_leads.py` around lines 195-217):

```
(-VOLUME_RANK[hotel_volume] if != "na" else 0,
 -score,
 CLASS_RANK[email_class],
 -TRAFFIC_RANK[traffic_tier],
 -traffic_score,
 -branches_estimate)
```

Traffic is now the **4th key**, a tie-break used only when two leads are
already equal on hotel-volume, `score`, and email-class. It still uses the
same `TRAFFIC_RANK = {"high": 3, "medium": 2, "unknown": 1, "low": 0}` and the
`unknown`-above-`low` protection, but it can never outrank a better-scoring or
better-email-class lead the way the original leading-key design would have.

#### Why this was reversed

Calibration (990 domains, 5 run folders) validated the tier as a real
busyness measure: it discriminates cleanly at 21.1% high / 34.6% medium /
44.2% low, and that part of the design held.

What it could NOT validate is the actual hypothesis the leading-sort-key
design depended on — that busier businesses are EASIER to enrich (find a
named decision-maker with a direct email). Only one run on disk had both
`raw_html/` (needed to score traffic) and Stage 5.5 enrichment ground truth
(`leads-with-contact.json` + `leads-dropped.json`): `2026-07-27-lb-ngos-1`,
n=37, a single vertical (NGOs), with only 1 lead in the high tier. Within that
thin sample, the relationship ran the WRONG way — low tier enriched at 80%,
medium at 62.5%, high at 0% (n=1, not meaningful alone) — and the
point-biserial correlation between raw traffic score and enrichment survival
was -0.34, an inverse relationship.

The plausible mechanism: larger/busier organisations more often publish only
a role inbox (`info@`, `reception@`) rather than a named decision-maker's
address, and email-availability failure is already 77% of all Stage 5.5
drops. A leading traffic key would have pushed exactly those harder-to-reach
leads ahead of reachable ones, for a stage that is already supply- and
yield-constrained.

n=37 in one non-representative vertical is too thin to prove the inverse
relationship generalizes, but it was enough to reject the positive-correlation
hypothesis the leading-key design required — so the design was scaled back to
a same-quality-only tie-break, which has upside (free reordering among
equally-reachable leads) with no ability to push a hard-to-reach lead ahead of
a reachable one. Revisit the leading-key design if a non-NGO run ever produces
both `raw_html/` and Stage 5.5 outcomes and shows the expected positive
relationship.

#### 3b. Floor — opt-in per campaign

New optional key read alongside the existing two at `qualify_leads.py:81-88`:

```json
{"min_traffic_tier": "low"}
```

Accepted values: `"low"`, `"medium"`, `"high"`. **Key absent means no floor** — so the 20 fixtures that currently have no `qualify.json` continue to behave exactly as today. Unknown keys are already silently ignored, so adding this key is backwards-compatible with any older checkout.

New gate, placed **last** in the existing first-match-wins chain at `qualify_leads.py:101-125`, after `freemail-only`, `dead-signal`, `score<82`, and `hotel-volume-too-low`:

```
if min_traffic_tier is set
   and lead.traffic_tier != "unknown"
   and TRAFFIC_RANK[lead.traffic_tier] < TRAFFIC_RANK[min_traffic_tier]:
       drop, reason = f"traffic-too-low:{lead.traffic_tier}"
```

Placed last so that existing drop reasons keep their current attribution in `disqualified-log.txt` and in the per-gate counters, which keeps historical run comparisons meaningful.

### Safety rules

Two rules exist specifically to protect a funnel that is already supply-starved (`eu-hotels` sourced 0 candidates on its last two fires; 279/279 of its cities are spent).

**Rule 1 — `unknown` is never dropped.** Enforced by the explicit `!= "unknown"` clause above. If we could not fetch enough of a site to judge it, that is our fetch failing, not the business being quiet. Fetch health is genuinely variable: lifetime fetch-to-extract survival is 50.7% (16,572/32,681), with recent runs ranging 39%–82%. A fetch hiccup must never silently kill a good lead.

**Rule 2 — traffic drops are NOT written to `disqualified-log.txt`.** Every other drop in `qualify_leads.py` appends `(domain, reason)` to that ledger, which `merge_candidates.load_disqualified_domains` then uses to block the domain at Stage 3 **permanently, across all channels and all future runs**. Traffic detection depends on how well we crawled a given site on a given day, so it is not durable proof about the business. A traffic drop must therefore be a this-run decision only.

Implementation note: the `disqualified` list is built inside the same loop, so the new gate must `continue` without appending to it — the counter still increments and the drop is still reported.

## Data flow

```
raw_html/{domain}__*.html          (already fetched — no new requests)
        |
        v
extract_leads.collect_html_for_domain  ->  html_lower
        |
        v
traffic_signals.detect_traffic(html_lower, pages_scanned, branches_estimate)
        |  -> (tier, score, evidence, signals)
        v
leads-extracted.json               (+4 fields per lead)
        |
        v
qualify_leads.py
        |-- optional floor gate  -> dropped (this run only, NOT ledgered)
        |-- sort by traffic tier first
        |-- cap at ENRICH_MAX_LEADS (250)
        v
leads-qualified.json               -> Stage 5.5 enrichment fan-out
```

## Calibration

Thresholds must be set from measurement, not guessed — per the `project/CLAUDE.md` directive to verify against real run artifacts before asserting pipeline behaviour.

There are **5,368 already-fetched HTML files** on disk across `project/runs/`, including 790 in `2026-08-01-eu-hotels`. These cost nothing to reuse.

Calibration procedure:

1. Add a report-only entry point (`python3 tools/scripts/traffic_signals.py --report <run-dir>`) that scores every domain in a run's `raw_html/` and prints the tier and signal distribution, broken down by vertical.
2. Run it across the available run folders.
3. Set the tier thresholds so the distribution is discriminating rather than degenerate — a detector that labels 95% of leads `high` is worthless, and so is one that labels 95% `low`.
4. Where a run has both `raw_html/` and a matching `leads-dropped.json` / `enrich-summary.json`, cross-tabulate tier against Stage 5.5 outcome. If higher tiers show a higher enrichment success rate, that is direct evidence the signal is real. Record the number in the module.
5. Only then set each campaign's `min_traffic_tier` in `templates/<base>/qualify.json`.

> **AMENDMENT (2026-08-10, post-implementation):** step 5 above did not
> happen and should not be read as describing what shipped. Calibration
> validated the tier as a busyness measure (step 3), but the cross-tab in
> step 4 came back thin and adverse (n=37, one vertical, inverse
> correlation — see "Why this was reversed" in the Architecture section
> above), so no fixture sets `min_traffic_tier`, and the ranking design was
> scaled back from a leading sort key to a same-quality-only tie-break. The
> floor MECHANISM shipped (an opt-in `qualify.json` key, `unknown` exempt,
> drops not ledgered) but is dormant everywhere. Setting a floor on any
> fixture requires re-measuring both the enrichment cross-tab in a non-NGO
> vertical AND fetch-completeness stability (see the `MIN_HTML_FOR_JUDGMENT`
> comment in `traffic_signals.py`) before it can be considered safe.

Ship order: ranking first (safe, no drops), floors second (after calibration).

## Error handling

- **Malformed JSON-LD** — wrap parsing; a parse failure contributes no review-count signal rather than raising. `extract_leads.py` processes hundreds of domains per run and must not die on one bad page.
- **Absurd review counts** — clamp to a sane ceiling before scoring so a single scraped junk number cannot dominate the ordering.
- **Missing/short HTML** — return `("unknown", 0, "insufficient html", [])`. Never raise.
- **Malformed `qualify.json`** — already handled: `except: cfg = {}` at `qualify_leads.py:81-88`. An invalid `min_traffic_tier` value must be treated as absent (no floor) rather than as a floor of `low`, so a typo cannot silently start dropping leads.
- **Backwards compatibility** — leads produced by an older `extract_leads.py` lack the traffic fields. `qualify_leads.py` must default a missing `traffic_tier` to `"unknown"` and a missing `traffic_score` to `0`, exactly as `_domain()` already falls through to `website` for the missing `domain` field.

## Testing

Unit tests for `traffic_signals.detect_traffic`, against fixture HTML strings:

1. JSON-LD `AggregateRating.reviewCount` parsed correctly; microdata variant; visible-text variant; multilingual visible-text variant.
2. Each tracker vendor detected individually; stack depth counted as distinct vendors, not occurrences (a GTM snippet appearing four times on one page is one vendor).
3. Each chat vendor detected; `hreflang` language count.
4. Empty HTML, whitespace-only HTML, and sub-minimum-length HTML all return `unknown` — never `low`.
5. Malformed JSON-LD does not raise.
6. Determinism: the same input returns an identical tuple across repeated calls and across processes with differing `PYTHONHASHSEED`.
7. Clamping: an absurd review count does not produce an out-of-range score.

Gate tests for `qualify_leads.py`:

8. `min_traffic_tier` absent → no lead dropped for traffic (protects the 20 fixtures with no `qualify.json`).
9. `min_traffic_tier` set → `low` dropped, `medium`/`high` kept.
10. `min_traffic_tier` set → `unknown` **kept** (Rule 1).
11. A traffic drop does **not** append to `disqualified-log.txt`, while an adjacent `hotel-volume-too-low` drop in the same run still does (Rule 2).
12. Invalid `min_traffic_tier` value behaves as absent.
13. Sort order: given a mixed set, `high` precedes `medium` precedes `unknown` precedes `low`; within one tier, the higher `score` precedes.
14. A lead record lacking the traffic fields entirely is treated as `unknown` and is not dropped.

## Out of scope

Chain verification performed alongside this design confirmed four separate defects. They are **not** addressed here and should be tracked independently:

1. **`source_overpass.py:605`** — `sys.exit(<str>)` exits 1, but `run_fire.py:546` tolerates only 8. A target yielding 0 website-tagged rows kills the whole fire and discards the `unresolved` file it just wrote, before Stage 2.5 can resolve it. `source_places.py:242` already has the correct logic.
2. **`send_batch_brevo.py:262`** — a failed Brevo send is recorded as `result: "failed"` and the script exits 0, so `run_fire.py` prints `DONE … sent+persisted` when zero emails were sent. The "never persist" guarantee does hold; only the reporting lies.
3. **No `wa_only` bypass in `run_fire.py`** — a WhatsApp-only run (`lb-enterprise`) drops every lead lacking a direct *email*, then burns a Sonnet gap-writer fan-out producing an `emails-drafted.json` that `draft_whatsapp_custom.py` never reads, and aborts on exit 5 when that drafter yields nothing.
4. **Documented halts that do not exist** — ARCHITECTURE.md §3 claims Stage 3 halts below 50 merged candidates and Stage 4 halts below 40% domain yield. Neither is implemented.

Also noted: `hotel-volume-too-low` writes to the permanent `disqualified-log.txt` (453 domains retired on a same-day size judgment). This has the same durability problem Rule 2 avoids, but changing it is a separate decision.

Finally, the deeper constraint this feature does not solve: supply. `eu-hotels` is 279/279 cities spent, `au-trades` 16/16, and 19 of 21 fixtures have never fired a single city. Better ordering of a starved funnel is still better, but it does not create leads.
