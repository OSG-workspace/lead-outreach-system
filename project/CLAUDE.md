# Lead Outreach System — operating doc

Free-stack lead pipeline: source businesses (Google Maps / Overture / OSM / Places / directories — no paid API), score against a per-run ICP, resolve the decision-maker, send personalised cold email via Brevo. WhatsApp and LinkedIn are opt-in arms of the same chain (`channels.json` per fixture). **`run_fire.py` is the only orchestrator.** `ARCHITECTURE.md` is the file/agent/stage map; `.claude/commands/fire.md` is a note for debugging one stage by hand.

## Salutation contract (non-negotiable)

**Every email opens `Hello Mr. <Surname>,` / `Hello Mrs. <Surname>,` (or `Hi …`).** Never bare `Hello,` / `Hi,` and never `<Business> team,`.

**One-name-is-enough exception (user directive 2026-07-22).** When a **verified direct email** (verbatim or evidence-reconstructed — the same bar as any send-eligible address) confirms EITHER name component, that alone is send-eligible: first name → `Hello <First>,` (no Mr./Mrs.); surname + assignable gender → `Hello Mr./Mrs. <Surname>,`. Search for the full name first; a name with NO verified direct email is still dropped — the exception lowers the salutation bar, never the email bar. Per lead: name (≥1 component), gender when the surname is used, and a **source URL**; anything less is dropped at draft (kill-on-fallback). Implemented at Stage 5.5 by the `name-finder` Haiku agent (`<repo-root>/.claude/agents/name-finder.md`), one headless agent per qualified lead.

## Natural-language fire trigger (ROUTER)

> **DIRECTIVE — ACT, DON'T ANALYZE.** A fire phrase ALWAYS starts a **completely new run**: a fresh dated `runs/` folder, launched **immediately** via `fire_campaign.sh` → `run_fire.py`. Do not deliberate, re-read the codebase or summarise; never reuse, resume or overwrite a run folder. The ONLY permitted pause is the single routing question below, and only when the target is genuinely unnamed.
>
> **Every fire is INDEPENDENT of every previous run.** Prior runs of the same campaign — earlier today, still in progress, however many — are never a reason to pause, inspect or ask ("already fired today?", stale ground, expected low yield, "fire anyway vs rotate first" are all forbidden — user directives 2026-07-17, 2026-07-26). Do not scan `runs/` or count today's sends. The only cross-run state is the dedup ledgers (`sent-log.md`, `sourced-log.txt`, `disqualified-log.txt`, `overpass-cities-fired.txt`), applied silently inside `run_fire.py`; a ledger that leaves nothing to search makes the run ABORT with a root cause, never ask. `fire_campaign.sh` auto-suffixes the slug (`-1`, `-2`, …); runs are parallel and independent.

Fire phrases (any session, no slash command): "fire … run", "fire it", "fire the pipeline", "let's fire", "send a campaign", "send out a batch", "go" (pipeline context), "ship it". **The trigger is a ROUTER** — identify the TARGET first; never default a bare phrase to GCC.

### Step 1 — identify the target

`SLUG_BASE` is the `templates/<base>` fixture to clone (fixed path, no `runs/` scan). A US fire must name both "us"/"usa"/"states" AND one of the four verticals:

| If the phrase names… | `SLUG_BASE` |
|---|---|
| US + "law firm(s)" / "lawyers" / "law" | `us-law-firms` |
| US + "staffing" / "recruiting" / "recruiter" | `us-staffing` |
| US + "med spa" / "clinic" / "dental" / "medspa" | `us-clinics` |
| US + "property" / "property management" | `us-property` |
| "gcc" / "consumer chain(s)" | `gcc-auto` |
| "marketing agencies" / "gcc agencies" / "agencies gcc" | `gcc-agencies` |
| "eu hotel(s)" / "european hotels" / hotels in ES/IT/FR | `eu-hotels` |
| "lebanese" / "lebanese run" / "biggest lebanese companies" | `lb-enterprise` |
| "lb receptionist" / "receptionist lebanon" | `lb-receptionist` |
| bare "lebanon" / "lb" (no other word) | ASK: `lb-enterprise` or `lb-receptionist` |
| "worldwide" | `worldwide-receptionist` |
| "linkedin" / "li run" / "fire linkedin" / "gcc linkedin" | `gcc-outreach-li` |
| "outreach the linkedin run for <brief>" / "message the linkedin leads" | **not a fire** — `./linkedin-run handoff <brief>` (`.claude/commands/li-outreach.md`): li-search's delivered pool → li-writer → the same backlog |
| "australia" / "australian" / "ai receptionist australia" / AU trades ("aussie plumbers", "australian tradies") | `au-trades` |
| "fintech" / "money transfer" / "AML" / "bank compliance" (Lebanon) | `lb-fintech-compliance` |
| "supermarkets" / "food retail" (Lebanon) | `lb-supermarkets` |
| "insurance" / "insurers" / "TPA" / "hospitals" (Lebanon) | `lb-insurance-tpa` |
| "FMCG" / "distributors" / "industrial groups" (Lebanon) | `lb-fmcg-distributors` |
| "construction" / "engineering" / "contractors" (Lebanon) | `lb-construction` |
| "restaurants" / "hotels" / "beach clubs" (Lebanon, NOT "lb receptionist") | `lb-restaurants-hotels` |
| "NGOs" / "international organizations" (Lebanon) | `lb-ngos` |

**Generic rule:** a phrase naming any existing `templates/<base>` folder (or an unambiguous part, "fire the gcc receptionist run" → `gcc-receptionist`) fires that base; `ls templates/` is the authoritative list. To change a campaign, edit its fixture, never a past run.

- **Target named** → Step 2, no clarifying questions. **"US" without a vertical** → ask which of the four. **No target at all** ("fire it", "go") → do not default; ask via `AskUserQuestion` (US [which] · GCC · Lebanon · Worldwide · new vertical → new-campaign contract first).

### New-campaign contract (TEMPLATE-FIRST)

The system never invents email copy at fire time. For a vertical with no `templates/<base>/`:
0. `python3 tools/scripts/new_campaign.py --base <base> --vertical <v> --countries <ISO2,…> --selector '<osm selector>'` scaffolds the fixture (`campaign.json` = every knob, `icp.yaml`, `places.txt` stub) and prints what still blocks a fire. A fixture is exactly `campaign.json` + `icp.yaml` + `pitch.json` + `places.txt` [+ `qualify.json`] — schema in `templates/README.md`.
1. Ask for their email template; if they have one, store it verbatim as `templates/<base>/pitch.json` (`subject_template` + `body_template`; slots `{salutation}` `{name}`, optionally `{opener}` `{vertical}` `{country}`).
2. If none, draft 2-3 non-generic examples (no em/en dashes, no banned words per `vault/lead-outreach/voice-us.md`, evidence-shaped opener, one CTA, one link) and offer them via `AskUserQuestion`. **Never wire a template the user has not confirmed.**
3. Offer refinement; the saved `pitch.json` is then FIXED copy.
4. Per-business variation only through slots (`{opener}` + `signal_openers`), never by rewriting.
5. `draft_mode=template` is the default; `custom` (lead-writer/gap-writer) is opt-in on explicit request, `voice-us.md` + merge gates mandatory.

### Step 2 — fire: ONE command, zero deliberation

```bash
cd <your-checkout>/project
bash tools/scripts/fire_campaign.sh <SLUG_BASE>     # --dry-run drafts only · --plan traces · --target-leads N
```

**A lead volume in the phrase ("100 leads", "send to 30") → `--target-leads N`:** sourcing scales to ~N qualified leads, the send caps at N, and a shortfall is reported in `status.txt` + the DONE line — relay it. `fire_campaign.sh` validates the fixture (`campaign_config.py --validate`: `campaign.json` schema + `icp.yaml`; `pitch.json` for template mode, `vertical` for custom, `places.txt` for gmaps/overture/places sources), materialises `campaign.json` into the per-file layout inside a new dated run folder and hands off to `run_fire.py`. A missing piece ABORTs naming it — the only case where you stop and talk to the user.

**LinkedIn runs (`channels.json` has `"linkedin"`) end differently — say so.** A LinkedIn fire **queues, it does not send** (8-18 invites/day, 90/week; one fire loads weeks of supply into `linkedin/state/backlog.json`), so `drafted=0` / "sent 0" are CORRECT. `preflight.js` runs first — relay its `fix:` line if it aborts (usually `bash linkedin/scripts/start_chrome.sh`). Send side: `cd project/linkedin && bash scripts/daily.sh --live`. Report N queued, M messageable today, weeks of backlog.

**Live status — NEVER go dark (user directive).** Launch with `run_in_background: true`, poll `cat runs/<slug>/status.txt` every 60-90 s, relay ONE line per stage change, surface `ABORTED: …` immediately, finish with the counts (candidates → qualified → contact → drafted → sent).

### When to ASK instead of firing

The ONLY reasons to pause: bare phrase with no target (ask which campaign) · bare "lebanon"/"lb" (ask `lb-enterprise` WhatsApp vs `lb-receptionist` email; "lebanese run" alone → `lb-enterprise`) · named target has no fixture with `campaign.json` + `icp.yaml` (set it up, then fire) · `vault/lead-outreach/sent-log.md` missing (bootstrap the vault) · "dry-run"/"preview" (still a NEW dated run, launched at once with `--dry-run`).

## VERIFY AGAINST THE LAST RUN BEFORE ASSERTING ANYTHING (user directive 2026-07-31)

**Never describe how this pipeline behaves from the code or the docs — read the last run's artifacts first.** Claims made without opening a run folder have repeatedly been wrong in the optimistic direction.

| Artifact | Answers |
|---|---|
| `runs/<slug>/status.txt` | where the run ended, or what aborted it |
| `runs/<slug>/candidates-all.txt` | how many candidates sourcing ACTUALLY produced |
| `runs/<slug>/leads-extracted.json` | how many survived fetch + extraction |
| `runs/<slug>/leads-qualified.json` | how many qualified (all freshly sourced) |
| `runs/<slug>/enrich-summary.json` | per-gate drop counts for enrichment |
| `runs/<slug>/leads-dropped.json` | per-lead `agent_reason` — WHY leads died |
| `runs/<slug>/emails-sent.jsonl` | what was really sent |
| `vault/lead-outreach/overpass-cities-fired.txt` | which ground is already spent, per source + vertical |
| `vault/lead-outreach/disqualified-log.txt` | domains retired forever |

`0 candidates` HALTS the run and that is correct: a **supply** signal (add `places.txt` rows or a source), not a broken run. Compare several consecutive fires:

```bash
cd project/runs && for d in $(ls -1t | head -12); do
  printf "%-30s cand=%-5s extr=%-5s qual=%-5s contact=%-4s sent=%s\n" "$d" \
    "$(grep -c . $d/candidates-all.txt 2>/dev/null)" "$(grep -c . $d/leads-extracted.json 2>/dev/null)" \
    "$(grep -c . $d/leads-qualified.json 2>/dev/null)" "$(grep -c . $d/leads-with-contact.json 2>/dev/null)" \
    "$(grep -c . $d/emails-sent.jsonl 2>/dev/null)"; done
```

Judge sourcing by `candidates-all.txt`, never by the DONE line. If a run folder contradicts this file or ARCHITECTURE.md, the run folder is right — fix the doc.

## Approval mode

| Mode | Invoke | Gate | Cap / sender |
|---|---|---|---|
| **Dry-run** | `fire_campaign.sh <base> --dry-run` | drafts only; review `emails-drafted.json` | nothing sent |
| **Auto-fire** | fire phrase / `fire_campaign.sh <base>` | NONE — pre-authorised by the phrase | `MAX_EMAILS_PER_RUN` (1000) via `send_batch_brevo.py` (Brevo `messageVersions`) |

Safety nets in both (enforced without approval): every send logged to `vault/lead-outreach/sent-log.md`; dedup against it at merge, extract and send; Brevo bounce/block/spam synced before every live send; below-threshold leads never contacted; `email_class: personal` dropped at qualify; `modern_booking` skipped; constructed emails need an evidence URL + live domain.

## The free sourcing stack

| Source | Tool |
|---|---|
| Google Maps (primary) | `gosom/google-maps-scraper` via `source_gmaps.py` — website + category per row, no key |
| Bulk POI | Overture Maps via DuckDB (`source_overture.py`) — no key, no ceiling |
| OSM | Overpass (`source_overpass.py`) — premises only |
| Google Places | `source_places.py` — `GOOGLE_PLACES_API_KEY`, Pro tier, name-only → Stage 2.5 |
| Search / fetch | `ddgs`, `crawl4ai` |

**Every stage runs on `tools/venv/bin/python3`, and that is load-bearing.** Bare `python3` is the system interpreter without the pipeline's deps (on 2026-08-11 it silently scored Stage 2.5 at 0/300) — when a stage fails wholesale with a dependency-shaped symptom, check the interpreter before concluding anything about supply.
