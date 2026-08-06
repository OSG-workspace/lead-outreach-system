# Pipeline Contract

The Automate outreach pipeline. 8 stages. Read this file at the start of every session.

**See `ARCHITECTURE.md` for the full file-structure map (paths, permissions, sub-agent contract).** This doc is the stage-by-stage contract. ARCHITECTURE.md is the operational layout.

## Quick reference

| Stage | Script | Input | Output |
|---|---|---|---|
| 1 | `run_fire.py` (deterministic orchestrator) | fixture control files | run folder resolved, Stage 2 launched |
| 2 | `tools/scripts/source_overpass.py` (OSM) / `source_places.py` (Places) — **deterministic enumeration, 0 agents, 0 tokens** | sourcing.json, places.txt | candidates-batch-N.txt |
| 3 | `tools/scripts/merge_candidates.py` | candidates-batch-*.txt | candidates-all.txt |
| 4 | `tools/scripts/fetch_html.sh` → `fetch_html.py` (**crawl4ai JS-render, multi-page: /, /contact, /about, /team**) | candidates-all.txt | raw_html/{domain}__{slug}.html |
| 5 | `tools/scripts/extract_leads.py` (scans all pages per domain; computes score, signal, hotel_volume) | raw_html/, candidates-all.txt | leads-extracted.json |
| 5.3 | `tools/scripts/qualify_leads.py` (drop freemail/low-score/dead-signal + per-run gate e.g. hotel size; rank by fit; **cap top-N**) | leads-extracted.json, qualify.json | leads-qualified.json |
| 5.5a | `tools/scripts/enrich_contact_person.py --phase prep` (reads leads-qualified.json; writes 1 enrich-batch per lead) | leads-qualified.json | enrich-batch-*.txt |
| 5.5b | **Haiku `name-finder` sub-agents (1 per lead, max parallelism)** | enrich-batch-*.txt | enrich-out-*.json |
| 5.5c | `tools/scripts/enrich_contact_person.py --phase merge` (drops leads without name+gender) | enrich-out-*.json | leads-with-contact.json |
| 6 | `tools/scripts/draft_emails.py` (role-class ≥85 gate + strict Mr./Mrs.+Surname gate) | leads-with-contact.json | emails-drafted.json |
| 7 | `tools/scripts/send.py` *(reviewed mode)* OR `tools/scripts/send_batch_brevo.py` *(auto-fire / `/fire`)* | emails-drafted.json | emails-sent.jsonl |
| 8 | `tools/scripts/persist_sent_log.py` | emails-sent.jsonl | vault/lead-outreach/sent-log.md |

**Auto-fire path (`/fire`):** runs the **Haiku-DDG parallel path**. Stage 2 dispatches **one sub-agent per query** for maximum parallelism (~130 agents for a 130-query batch). Stage 4 fetches up to 6 pages per domain (homepage + /contact + /contact-us + /about + /about-us + /team) — yields ~2-3× more extracted emails than homepage-only. Stage 5 scoring: person base 90, role base 82, +5 for 5-15 branches, +3 for 16-30 branches. Stage 6 enforces the non-negotiable role-class ≥85 gate (role-class only sent when chain-sized). Stage 7 uses Brevo `messageVersions` (1 HTTP call / 1000 emails). **Hard-halt rule**: any fallback (sub-agent permission denied, <50 merged candidates, <40% fetch rate, 0 extracted, 0 drafted, Brevo 4xx) aborts the run; never ships a degraded campaign. See `.claude/commands/fire.md`, `agents/run-auto.md`, and the `feedback_kill_on_fallback` + `no-path-swap-on-fallback` memories.

**Deprecated:** `tools/scripts/run_campaign.py --mailscout` (Google-Maps + crawl4ai + MailScout SMTP). Was the original auto-fire path; replaced because (a) Maps scrape took 25+ min per pass, (b) crawl4ai timed out frequently, (c) MailScout SMTP validation returns 0 against GCC catch-all domains. The current path runs end-to-end in ~5-10 min and ships 10-20× more sends.

## To start a new run

Read `agents/run-kickoff.md`. Follow it exactly.

## Run folder layout

Each run folder SHOULD carry a `README.md` (structure + scoring manifest — see
`runs/2026-06-23-eu-hotels/README.md` for the canonical example) documenting the
exact fields each task reads and scores on for that run.

```
runs/YYYY-MM-DD-<slug>/
  # ---- CONTROL FILES (inputs you set; cloned forward on bootstrap) ----
  README.md             ← per-run structure + scoring manifest
  icp.yaml              ← scoring rubric / ICP intent
  countries.txt         ← ISO-2 country filter (absent → GCC default)
  draft_mode.txt        ← template | custom
  channels.json         ← ["email"] / ["whatsapp"] / both
  pitch.json            ← fixed copy overlay (template mode)
  vertical.txt          ← required for custom runs
  qualify.json          ← per-run qualify gate (e.g. hotel size/volume)
  # ---- ARTIFACTS (regenerated each /fire; safe to delete) ----
  candidates-batch-*.txt  ← sourcing agent outputs
  candidates-all.txt      ← merged + deduped (Stage 3)
  raw_html/               ← fetched HTML, 6 pages/domain (Stage 4)
  leads-extracted.json    ← scored leads + signals + hotel_volume (Stage 5)
  leads-qualified.json    ← qualified + capped top-N (Stage 5.3)  ← NEW
  enrich-batch-*.txt / enrich-out-*.json      ← name-finder I/O (Stage 5.5)
  leads-with-contact.json ← contact resolved (Stage 5.5)
  lead-batch-*.txt / lead-out-*.json          ← combined lead-writer I/O (custom email)
  gap-batch-*.txt / gap-out-*.json            ← gap-writer I/O (custom+WhatsApp email)
  emails-drafted.json     ← ready-to-send drafts (Stage 6)
  emails-sent.jsonl       ← send results (Stage 7)
  send-log.txt            ← human-readable send log
```

## Key constraints

- **NEVER re-contact** an email already in `vault/lead-outreach/sent-log.md`
- **Dedup is non-negotiable** — merge_candidates.py enforces it at Stage 3; extract_leads.py enforces it again at Stage 5
- **Country scope** (Stage 3) — `merge_candidates.py` filters candidates by country. Precedence: `<run>/countries.txt` (`ALL`/`*` = worldwide) → else `icp.yaml geography: worldwide` = worldwide → else GCC default `{AE,SA,QA,BH,KW}`. A run whose ICP declares `geography: worldwide` is NEVER silently narrowed to GCC (the bug that capped the 2026-05-30 worldwide runs at ~51 candidates).
- **Send cap** — `MAX_EMAILS_PER_RUN` env (default 1000); **enrich cap** — `ENRICH_MAX_LEADS` env (default 80, top-N by fit) caps the Stage 5.5 fan-out
- **Approval**: `/fire` (auto mode) does NOT prompt — it is pre-authorized. The `--send` explicit-yes gate applies only to reviewed mode (`run-kickoff.md`)
- **modern_booking signal** → skip the lead (gap already solved)
- **Salutation contract** — every draft must open `Hello Mr./Mrs. <Surname>,`. Leads without a resolved contact-person + gender are dropped at Stage 6. See `CLAUDE.md` → "Salutation contract".

## Stage 5.5 — contact-person enrichment

Between Extract (Stage 5) and Draft (Stage 6), the pipeline resolves a contact-person + gender for every extracted lead so the email opens with `Hello Mr./Mrs. <Surname>,`.

**Phase A (prep, orchestrator-side):** `enrich_contact_person.py --phase prep --run-dir <RUN>` reads `leads-extracted.json` and writes `enrich-batch-NNN.txt` — one file per lead containing:
```
LeadId: web-thewarehousegym-com
Business: The Warehouse Gym
Country: AE
Vertical: fitness
Website: https://thewarehousegym.com
OutputFile: /abs/path/to/runs/<slug>/enrich-out-NNN.json
```

**Phase B (Haiku fan-out, orchestrator-side):** The orchestrator dispatches one `name-finder` Haiku sub-agent **per enrich-batch file** in a single message (max parallelism, exactly like Stage 2). Each agent searches the web for the named business's owner/CEO/founder, picks the most-senior decision-maker, classifies their gender, and writes a JSON result to `enrich-out-NNN.json`:
```json
{"lead_id": "...", "first_name": "Ahmed", "last_name": "Al Sayed", "title": "Mr.", "role": "Founder & CEO", "source_url": "https://thewarehousegym.com/about", "confidence": "high"}
```
On no match, the agent writes `{"lead_id": "...", "found": false, "reason": "..."}` so we have an audit trail.

**Phase C (merge, orchestrator-side):** `enrich_contact_person.py --phase merge --run-dir <RUN>` reads all `enrich-out-*.json`, joins them to leads-extracted.json, drops leads with no name or no gender, and writes `leads-with-contact.json` (the new Stage 6 input).

The Haiku sub-agent contract lives in `.claude/agents/name-finder.md` (locked tools: `WebSearch`, `WebFetch`, `Write`).

## Scripts

All scripts use the project venv:
```bash
source tools/venv/bin/activate
```

All scripts accept `--run-dir <path>` as their only required argument (except fetch_html.sh which takes it as $1).

The `.env` file at `project/.env` holds `BREVO_MCP_TOKEN`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`.

## Sourcing

Sourcing dispatches **no agents at all**. `source_overpass.py` enumerates
OpenStreetMap (and `source_places.py` the Google Places API) directly, emitting
`candidates-batch-*.txt` in the same pipe-delimited format every later stage
already reads. Zero LLM tokens, exhaustive per place, and the per-vertical city
ledger (`vault/lead-outreach/overpass-cities-fired.txt`) guarantees each fire
opens ground no earlier run covered.

The old agent-per-query "search" method — N Haiku `source-agent`s over
`queries.txt` — was retired 2026-08-05 together with its six agent definitions
and the fixtures' `queries.txt`/`fit_criteria.txt` files. No fixture had used it
since the move to map/places sourcing.

## Permissions (root-cause fix for past dispatch failures)

`.claude/settings.json` is loaded from the **session cwd** = the repo root. Sub-agents inherit these permissions. The settings.json MUST include `WebSearch` (and `WebFetch`, `Bash`, etc.) in `permissions.allow` — otherwise sub-agents will hit "permission denied" mid-run.

The legacy `project/.claude/settings.json` is NOT loaded by Claude Code because it's not at cwd. That file is kept for reference only.
