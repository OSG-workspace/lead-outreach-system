# Pipeline audit — one chain, more leads, no repeated work

**Date:** 2026-07-29
**Scope:** full-system audit of `lead-outreach-system`; fixes for lead volume, sourcing
reach, enrichment waste, chain/branch separation, and file organization.

---

## 1. Governing principle

> **One chain is the trunk. Every campaign is a branch of data hanging off it.**

Trunk code may reference only *generic* lead properties. A branch adds a folder
describing **where** to look, **who** to look for, **what to say**, and **what to
require**. Adding a campaign is adding a folder — never a code path, never an
agent, never a new chain.

This is currently violated. The violations are listed in §5 and made
non-recurrable by a test in §5.4.

---

## 2. Evidence: what the runs actually show

### 2.1 The last run, end to end (`2026-07-28-au-trades`, target 70 → 16 sent)

```
6 cities swept (all remaining AU ground)
  → 51 fresh candidates
    → 20 extracted + qualified (fresh)
    + 191 re-injected from the qualified-pending backlog
    = 211 dispatched to enrichment (211 name-finder agents)
      → 20 survived (9.5%)
        → 20 drafted → 16 sent
```

### 2.2 Enrichment is 91% loss, and most of the fan-out is repeat work

Cross-matching drop records across every au-trades run:

| Run | qualified | died at enrich | already failed in an earlier run |
|---|---|---|---|
| 2026-07-27-au-trades-1 | 140 | 116 | 59 |
| 2026-07-27-au-trades-2 | 218 | 190 | 176 |
| 2026-07-28-au-trades | 211 | 191 | 178 |

`pending_qualified.py` re-injects qualified-but-uncontacted leads every run. It
writes `attempts: 1` on insert and never increments it, and records nothing about
*why* a lead failed. ~178 agents per run re-run searches that already came back empty.
Backlog is 302 leads and growing (195 au-trades, 97 eu-hotels, 10 lb-fmcg).

**Critical counter-fact — the retries are load-bearing:**

```
                dispatched  →  emailed
fresh this run       20     →     7    (35% survive)
from backlog        191     →    13    ( 7% survive)
```

13 of 20 sends came from retries. Banning retries would have cut that run to 7
emails. So retries must become **cheap**, not forbidden.

**These are not re-sourced leads.** The sourced-log blocks previously-seen domains
at source and at merge; all 51 candidates were new. Sourcing precision already works.

### 2.3 Why enrichment fails (191 drop records, categorized)

| Reason | Count | Share |
|---|---|---|
| No direct/personal email found | 109 | 57% |
| No reason recorded (agent produced no output) | 62 | 33% |
| Only a generic/role mailbox | 15 | 8% |
| Other (incl. name found, email unverifiable) | 5 | 2% |

Two records describe leads that should have survived:
- *"owner name and direct personal email confirmed across multiple business
  listings; freemail is standard for owner-operated au tradies"* — killed by the
  freemail ban.
- *"first name confirmed by verbatim personal email from official contact page;
  surname not recoverable"* — the one-name rule permits this; the agent
  self-rejected with `found:false`.

`enrich-summary.json` reports `dropped_no_match: 186`, conflating "agent never
returned" (59 of 211 batches produced no output file) with "agent ran and returned
`found:false`" (127). Infrastructure failure and genuine unfindability are
indistinguishable in the diagnostics — both aborted runs (2026-07-20-eu-hotels-3,
2026-07-27-au-trades-3) were reported through this same undifferentiated counter.

### 2.4 Sourcing reach — measured against live Overpass

| Vertical | City | OSM elements | with `website` |
|---|---|---|---|
| Hotels | Barcelona | 536 | 289 (54%) |
| Clinics/doctors/dentists | Chicago | 312 | 121 (39%) |
| **Trades (current selector)** | **Sydney** | **22** | **16 (73%)** |
| Trades (widened craft list) | Sydney | 31 | 20 (65%) |

Overpass is strong for premises-mapped verticals and near-useless for service
trades. au-trades' total OSM universe is ~150 businesses across Australia; it has
been fired 8 times against it. All 6 cities are now ledgered, so **the next
au-trades fire aborts** with "every requested city is already in the ledger".

Two structural gaps, both with code that already exists and is unused:
- `resolve_domains.py` (Stage 2.5, zero-LLM, verified name-match) is **off in 17 of
  20 fixtures**. 39–61% of OSM elements are name-only. Its own measurement: Riyadh,
  61 clinics enumerated, 7 carried a website.
- `source_directory.py` (deterministic paginated-listing adapter) is wired at
  `run_fire.py:578` and **declared by zero fixtures**. Stage 2 was architected as a
  set of adapters and runs as one.

### 2.5 Dead layer

Stage 2 became one Overpass sweep (`cfg["method"] = "map"` … *"never branches"*,
`run_fire.py:164`), but only `run_fire.py` was updated:

| Dead | Evidence |
|---|---|
| 6 × `source-agent*.md` | Stage 2 dispatches zero agents |
| `queries.txt` in 20 fixtures (au-trades: 247 lines) | read by no Stage-2 code |
| `fit_criteria.txt` | same |
| `queries-fired-log.txt` (303 KB, 4345 rows) | `scope_run_queries()` / `commit_fired_queries()` never called |
| `load_covered_domains()`, `exclusions_for_query()` | never called |
| `new_campaign.py --method search --agent` | scaffolds config nothing reads |
| `merge_candidates.py` "rotate queries.txt" warning | points the operator at a dead file |

Docs are actively wrong: `ARCHITECTURE.md` §2 lists 5 source agents as live, §6
documents query scoping as an enforced net, §7 claims ~60% enrichment resolution
(actual 9.5%). `PIPELINE.md` numbers enrichment 6.5 where code says 5.5.
`README.md` and `MAILSCOUT_INTEGRATION.md` are self-declared deprecated.

---

## 3. Design

Ordered by effect on lead volume.

### 3.1 Sourcing: Stage 2 becomes the set of adapters it was designed as

**Trunk:** run every adapter a branch declares, in order, into the same candidate
contract. No adapter is vertical-aware.

- `resolve_domains` moves from per-branch opt-in to **trunk default** (a chain
  capability, not a branch behavior). Its verification gate is unchanged: unproven
  domains are dropped, per kill-on-fallback.
- Branches whose ICP is poorly mapped declare a `directory` source alongside `map`.
  For au-trades: trade licensing registers and association member lists.
- **`--probe` mode** (new): report element count and `website` share for a branch's
  selector × places **without firing**. Prevents another 8 runs against a
  31-element universe. Probe output is written to the branch folder as
  `coverage.md` so reach is a visible property of the branch.
- Widen the au-trades selector to the full trade craft list (22 → 31 in Sydney);
  correct but not sufficient on its own — the directory source is the real fix.
- City ledger records **yield per city**, so a thin sweep is visible instead of
  silently marking a metro done.

**Branch:** `sourcing.json` gains a `sources` array. Everything else unchanged.

### 3.2 Enrichment: one direct-email rule

Today the bar is enforced twice with different logic — `qualify_leads.py`
blanket-drops `email_class: personal`, then `enrich_contact_person.py` rejects
freemail domains. Collapse to one trunk rule:

> **A direct email is an address that evidence ties to a named person.**

- Generic locals (`info@`, `sales@`, `bookings@`) never pass — not person-tied.
  Unchanged.
- A freemail passes **only** with `email_basis: verbatim` plus a source URL tying it
  to the named decision-maker. An unattributed freemail still fails.
- The blanket `email_class: personal` drop at qualify is removed — it pre-empts the
  real gate with strictly less information. Qualify still *ranks* person > role >
  freemail, so the strongest leads sort first.

This removes a special case rather than adding one, and recovers the
owner-operated leads the drop log shows being discarded.

`name-finder.md` gains an explicit instruction that a person-tied freemail is a
valid `found:true`, and that the 2d one-name fallback is a real path — the agent
currently self-rejects on both.

### 3.3 Backlog: retries resume instead of restarting, and never crowd out fresh leads

New ledger `vault/lead-outreach/enrich-log.jsonl`, keyed by domain, written by
`enrich_contact_person.py --merge` on **every** outcome:

```json
{"domain":"atlan.com.au","outcome":"no_direct_email","attempts":2,
 "last":"2026-07-28","found_name":"Andy Hornbuckle",
 "found_format":null,"reason":"…"}
```

- A retried lead arrives **carrying prior findings** (name, observed email format),
  so attempt #2 resumes where #1 stopped rather than redoing the name search. This
  is data on the lead, not a second code path — the trunk treats fresh and retried
  leads identically.
- Cap at **3 attempts**. `no_agent_output` is infrastructure, not a verdict, and
  does not count against the cap.
- **Fresh-first ordering:** fresh leads fill a run to the cap; the backlog only tops
  up the remainder. A run can never be crowded out by the queue.

Expected on the last run's shape: same or more sends, at roughly a sixth of the
enrichment token cost, with the freed budget spent on a much larger fresh pool.

### 3.4 Diagnostics

Split `dropped_no_match` into `dropped_no_agent_output` and
`dropped_agent_found_nothing`. Key the >30% degraded-fan-out abort on the former
only — a genuinely unfindable market must not read as an infrastructure failure.

---

## 4. De-verticalizing the trunk

| Today (trunk knows the vertical) | After (trunk knows the shape) |
|---|---|
| `HOTEL_VERTICALS`, `hotel_volume`, `require_hotel_size_volume` in `qualify_leads.py` + `extract_leads.py` | generic `scale_tier`; the signals that earn each tier are declared in the branch's `qualify.json` |
| `COUNTRY_NAMES` = GCC + LB only (`enrich_contact_person.py:76`) | the full `COUNTRY_NAMES_ISO` already in `email_utils.py` |
| `draft_mode` → 3 draft scripts | one drafter; branch supplies `pitch.json` or a writer prompt |
| `resolve_domains` per-branch opt-in | trunk default |
| 6 × `source-agent-<region>.md` | deleted; Stage 2 is one adapter set |

Proof this matters: every dropped record in the **Australian plumbing** run carries
`"hotel_volume": "na"`. A trades run is ranked by a hotel-shaped function
(`qualify_leads.py:139`).

### 4.1 The rule becomes a test

`tests/test_one_chain.py` greps trunk scripts for vertical proper nouns (hotel,
clinic, plumber, law, trades, receptionist, …) and fails on a hit. Adding
eu-hotels-style special-casing to the trunk then breaks the build instead of
quietly accumulating.

**A new campaign is exactly one folder:** `places.txt` · `sourcing.json` ·
`pitch.json` · `icp.yaml` · `qualify.json`.

---

## 5. Cleanup

### 5.1 Delete
`.claude/agents/source-agent*.md` (6) · `queries.txt` + `fit_criteria.txt` in 20
fixtures · `queries-fired-log.txt` · `scope_run_queries()` ·
`commit_fired_queries()` · `load_covered_domains()` · `exclusions_for_query()` ·
search-method scaffolding in `new_campaign.py` · search branches in
`gen_run_readme.py` · the "rotate queries.txt" warning · root strays
(`marriott-output.json`, `name-finder-result.json`,
`seeko-hotel-finder-result.json`, `candidates-batch-construction.txt`) ·
`.git.bak` (440 KB) + `project/.git.bak` (34 MB) · `MAILSCOUT_INTEGRATION.md` ·
`project/README.md` · `.archive/legacy-skills/`.

### 5.2 Docs — four live files, each with a role header

| File | Role |
|---|---|
| `CLAUDE.md` | router only: phrase → branch. Drop the free-stack, approval-mode, and MCP sections describing the dead system. |
| `ARCHITECTURE.md` | the one map: trunk stages, adapter set, layout, gates, **real** numbers. |
| `.claude/commands/fire.md` | the playbook. |
| `templates/README.md` | how to add a branch. |

`PIPELINE.md` folds its still-true rationale into `ARCHITECTURE.md` and is deleted.
Every surviving MD opens with three lines: what it is, when to read it, what not to
use it for.

### 5.3 Run history
Keep `enrich-summary.json`, `leads-dropped.json`, `status.txt` for all 124 runs —
that trio is what made this audit possible. Strip `raw_html/` and intermediate JSON
from runs older than 30 days (`runs/` is currently 1.3 GB).

---

## 6. Success criteria

1. An au-trades fire sources materially more than 51 fresh candidates, and does not
   abort on an exhausted city ledger.
2. Enrichment dispatches agents only for leads with a plausible path to a
   decision-maker; repeat-failure share of the fan-out drops from ~93% to ~0.
3. Sends per run rise, with the verbatim/evidence-tied share of contact emails not
   falling — volume without lowering the bar.
4. `tests/test_one_chain.py` passes, and a new campaign requires no trunk change.
5. Every remaining MD states its role in its first three lines; no MD describes a
   stage or agent that no longer exists.

## 7. Out of scope

LinkedIn channel, Vapi/Kyle, WhatsApp bridge behavior, Brevo sending mechanics,
and copy/voice content. Untouched except where the chain/branch split requires it.

## 8. Risks

- **§3.2 widens the send bar.** Person-tied freemail becomes sendable on every
  campaign, including enterprise runs where a freemail is a weaker signal. Mitigated
  by requiring `verbatim` basis + a source URL; unattributed freemail still fails.
- **§3.1 directory adapters depend on third-party listings** whose structure can
  change. The adapter is config-driven and verification-gated, so a broken listing
  yields zero rather than wrong leads — but it needs the `--probe` check before a
  branch relies on it.
- **§4 de-verticalizing `qualify_leads.py` touches the gate every campaign passes
  through.** Needs the existing eu-hotels runs as a regression reference: the same
  input must produce the same qualified set before and after.
