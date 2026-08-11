# Lead Outreach System — Architecture (canonical map)

**This is the single source of truth for "what runs, where the files are, and which agents deploy with which tools." Read this first on any trigger.** PIPELINE.md is the long-form stage contract; `.claude/commands/fire.md` is the executable playbook; this file is the map that ties them together.

> **One line:** a fire trigger → bootstrap a dated run folder → deterministic OSM/Places enumeration (0 agents, 0 tokens) → merge+dedup → fetch HTML → extract+score → **qualify+cap** → **enrich decision-maker (name-finder fan-out)** → draft → Brevo batch send → persist. Email-only by default; WhatsApp is opt-in. Halts on any fallback. 5–12 min end to end.

---

## 0. On a trigger, read in THIS order

1. **`project/CLAUDE.md`** — the natural-language **router** (which target/ICP the phrase maps to; when to ASK vs fire).
2. **THIS file** — file map + agent roster + tool matrix + active-vs-legacy scripts.
3. **`project/.claude/commands/fire.md`** — the step-by-step executable contract.
4. PIPELINE.md only if you need the long-form rationale for a single stage.

Everything else under `project/` (README.md, `agents/*.md`, most of `tools/scripts/`) is **reference or legacy** — see §6. Do not execute from it.

### Two execution modes (same stages, same agents, same scripts)

| Mode | How | Orchestrator brain | When |
|---|---|---|---|
| **Deterministic** ⭐ | `python3 tools/scripts/run_fire.py <slug> [--dry-run] [--plan]` | **none** — pure Python; fan-outs via `agent_dispatch.py` (headless `claude -p`) | default; reproducible; ~0 orchestrator tokens |
| **In-session** | `/fire <slug>` → follow `fire.md` | Claude loop dispatches via the Agent tool | when already in a Claude session / debugging a single stage |

Both run the **identical** Haiku sub-agents (same definitions/tools/model) and the identical Stage 3–8 scripts, so sub-agent quality is the same either way — validated head-to-head 2026-06-24. The phrase→slug routing (CLAUDE.md) is a thin Claude turn that hands off to `run_fire.py`. Use `--plan` to trace the stage sequence for a slug without executing; `--dry-run` to draft without sending.

---

## 1. File map (✅ live · 🗄️ legacy/reference)

```
<repo-root>/                                      ← SESSION cwd (Claude Code loads .claude from here)
│
├── .claude/
│   ├── settings.json                                     ✅ authoritative permissions (orchestrator + ALL sub-agents)
│   └── agents/                                            ✅ ALL sub-agent definitions live HERE (cwd level)
│       ├── name-finder.md             ✅ decision-maker enrichment (Stage 5.5)
│       ├── lead-writer.md             ✅ combined find+write (custom email-only path)
│       ├── gap-writer.md              ✅ custom email writer (custom + WhatsApp path)
│       └── wa-writer.md               ✅ custom WhatsApp writer (Stage 8.5)
│
└── project/                                               ← pipeline implementation, run cwd for /fire scripts
    ├── CLAUDE.md                                          ✅ user instructions + fire ROUTER
    ├── ARCHITECTURE.md                                    ✅ THIS file (canonical map)
    ├── PIPELINE.md                                        ✅ long-form 8-stage contract
    ├── README.md                                          🗄️ 2025 free-stack edition (Maps/IG/LI) — deprecated, see header
    │
    ├── .claude/
    │   ├── commands/fire.md                               ✅ THE /fire playbook
    │   ├── commands/{find-leads,analyze-lead,setup-tools,vault-bootstrap}.md  🗄️ older skills-era commands
    │   ├── agents/personalizer.md                         🗄️ reviewed-mode personalizer (NOT used by /fire)
    │   └── settings.json                                  🗄️ NOT loaded (only cwd-level settings.json is)
    │
    ├── agents/{run-kickoff,run-auto}.md                   🗄️ human-readable reference only — NOT loaded as sub-agents
    │
    ├── tools/scripts/
    │   ├── run_fire.py                                    ✅ DETERMINISTIC orchestrator (no AI brain) — runs the whole pipeline
    │   ├── agent_dispatch.py                              ✅ headless sub-agent dispatch engine (claude -p pool) used by run_fire.py
    │   └── … (see §5 for the other LIVE scripts; rest are 🗄️ legacy)
    ├── tools/venv/                                        ✅ Python venv (crawl4ai, ddgs, etc.)
    │
    ├── vault/lead-outreach/                               ✅ THE ONLY vault (single source of truth)
    │   ├── sent-log.md                                    ✅ dedup source-of-truth — NEVER re-contact
    │   ├── bounce-list.md                                 ✅ hard-bounce suppression
    │   ├── voice.md / voice-us.md / voice-lb-wa.md        ✅ per-campaign copy voice
    │   ├── ICP-current.md / offer.md                      ✅ default ICP + offer
    │   └── targeting/us-inbox-scheduling.md               ✅ US vertical targeting doc
    │
    ├── templates/<base>/                                  ✅ FIXED campaign fixtures (canonical config; fire clones from HERE, never from runs/)
    ├── runs/YYYY-MM-DD-<slug>/                            ✅ per-run artifacts (see §3)
    ├── .archive/legacy-vaults/                            🗄️ retired duplicate vaults (see its README)
    └── .env                                               ✅ BREVO_MCP_TOKEN, BREVO_SENDER_EMAIL, MAX_EMAILS_PER_RUN, ENRICH_MAX_LEADS
```

**Rule:** sub-agent definitions live ONLY in cwd `/.claude/agents/`. The `project/agents/` and `project/.claude/agents/` files are reference/legacy and are never dispatched.

---

## 2. Agent roster — who deploys, with which tools, how many

Sub-agents: name-finder is **Haiku** (high-volume lookup work); the three writers (`lead-writer`, `gap-writer`, `wa-writer`) are **Sonnet** (low-volume, copy-quality-critical). All dispatched **foreground**, **≤50 Agent calls per assistant message** (never `run_in_background` — that re-reads the orchestrator context per completion and burns the usage limit).

| Agent | Tools (exact) | Stage | Deployed when | How many |
|---|---|---|---|---|
| `name-finder` | **Read, WebSearch, WebFetch, Write** | 5.5 | every run **except** the combined custom email-only path | **1 per qualified lead** (≤ cap) |
| `lead-writer` | **Read, Write, WebSearch, WebFetch** | 5.5+6 (combined) | `draft_mode=custom` **and email-only** (US gap verticals) — replaces name-finder+gap-writer | **1 per qualified lead** |
| `gap-writer` | **Read, Write, WebSearch, WebFetch** | 6 | `draft_mode=custom` **and** run also uses WhatsApp | **1 per qualified lead** |
| `wa-writer` | **Read, Write, WebSearch, WebFetch** | 8.5 | WhatsApp channel + custom WA mode | **1 per company w/ a CEO mobile** |
| `li-finder` | **Read, WebSearch, WebFetch, Write** | 8.6b | LinkedIn channel, for companies whose LinkedIn **company page yielded no person** | **1 per uncovered company** (≤ cap 120) |
| `li-writer` | **Read, Write** | 8.6 | LinkedIn channel | **1 per qualified person** |
| `personalizer` | Read, Write | — | 🗄️ reviewed-mode only — **not used by `/fire`** | n/a |

**Tool-lockdown is deliberate.** Source agents have *only* WebSearch+Write so they physically cannot fall back to Bash/ddgs/crawl4ai. Writer + name-finder agents add Read so they load their own input/scraped pages **from disk** — the orchestrator dispatches a file PATH, never inlined page text, so per-lead content never bloats the orchestrator context (the big Stage-5.5 token saving).

**Two fan-outs dominate cost:** Stage 2 (1/query) and Stage 5.5 (1/qualified-lead). Everything else is single-process Python. Token discipline at 5.5: (a) page text is read by the agent from disk, not relayed; (b) cookie/nav boilerplate is stripped at prep (`email_utils.extract_page_text`), ~2× smaller injected text; (c) the phone ladder is OFF unless WhatsApp is on.

---

## 3. The pipeline stages (current) and who owns each

| # | Stage | Owner | Output | Halt if |
|---|---|---|---|---|
| 1 | Bootstrap | orchestrator (CLAUDE.md router → fire.md) | `runs/<slug>/` with control files | template missing |
| 2 | Source | ONE stage, deterministic enumeration from `sourcing.json`: `map` → `source_overpass.py` (OSM) · `places` → `source_places.py`. **Zero agents, zero tokens.** | `candidates-batch-*.txt` | all Overpass mirrors fail |
| 3 | Merge+dedup | `merge_candidates.py` | `candidates-all.txt` | **0** merged (a count under 50 only prints a NOTE — it does not halt) |
| 4 | Fetch HTML | `fetch_html.sh` (crawl4ai, multi-page) | `raw_html/{domain}__{slug}.html` | never halts; **WARNs** below 40% domain yield (`FETCH_YIELD_WARN_PCT`) |
| 5 | Extract+score | `extract_leads.py` | `leads-extracted.json` | 0 extracted |
| 5.3 | **Qualify+cap** | `qualify_leads.py` | `leads-qualified.json` | 0 qualified |
| 5.5 | **Enrich decision-maker** | **name-finder** (1/lead) → `enrich_contact_person.py --merge` | `leads-with-contact.json` | 0 with name+gender+direct email |
| 6 | Draft | `draft_emails.py` (template) · `draft_lead_custom.py` (combined) · `draft_custom.py` (gap-writer) | `emails-drafted.json` | 0 drafts |
| 7 | Send | `send_batch_brevo.py --send` | `emails-sent.jsonl` | any Brevo 4xx/5xx |
| 8 | Persist | `persist_sent_log.py` | appends `vault/.../sent-log.md` | n/a |
| 8.5 | WhatsApp (opt-in) | **wa-writer** (1/company) → `draft_whatsapp_custom.py` → `send_campaign.js` | `whatsapp-sent.jsonl` | 0 drafts / send fails |
| 8.6 | LinkedIn (opt-in) | `draft_linkedin.py --prep` → `walk_companies.js` (company page → people) | `people-raw.json` | challenge / selector miss |
| 8.6b | LinkedIn **profile find** | `resolve_li_profiles.py --prep` → **li-finder** (1/uncovered company) → `--merge` → `walk_companies.js --profiles` | merged into `people-raw.json` | none — a company with no findable owner is simply skipped |
| 8.6c | LinkedIn gates + copy | `qualify_people.py` (owners/CEOs, alive, reachable, geo) → **li-writer** (1/person) → `linkedin_queue.py` | `linkedin-leads.json` + `linkedin/state/backlog.json` | 0 people qualified |
| 8.6d | LinkedIn **queue, not send** | `queue/generate.js` (invites, note-less) · `queue/generate-dm.js` (already-reachable) | `state/queue.json`, `state/dm-queue.json` | near-duplicate messages |

> **LinkedIn does not send during a fire.** It queues weeks of invite supply; `sender.js`,
> `sweep_acceptance.js` and `messenger.js` drip it out daily (8–18 invites/day, 90/week).
> A fire that "sends 0 on LinkedIn" is working correctly — see `linkedin/README.md`.

**Run control files** (in `runs/<slug>/`, cloned from the template on bootstrap — see `templates/README.md` for THE general fixture structure; a new campaign is only a new folder): `icp.yaml` (required), **`sourcing.json`** (required — the single Stage-2 contract: `{"method":"map","selector":"\"office\"=\"lawyer\"","vertical":"law","max_per_run":350}`), `draft_mode.txt` (template\|custom), `channels.json` (`["email"]` and/or `"whatsapp"`), `countries.txt`, `qualify.json` (per-run gate, e.g. hotel size/volume, `min_traffic_tier` floor), `pitch.json` (fixed-copy campaigns), optional `places.txt` `City|ISO2|lat|lon|half_width` (map method; default: built-in 195-city EU table). Map method walks the ledger `vault/lead-outreach/overpass-cities-fired.txt`, keyed `vertical|city`, so consecutive fires cover fresh cities region by region and different verticals never block each other (validated 2026-07-17: Chicago law-firms sweep, 44 usable candidates, zero code changes).

---

## 4. Routing — phrase → target (summary; CLAUDE.md is authoritative)

| Phrase names… | Slug base |
|---|---|
| US + law/staffing/clinics/property | `us-<vertical>` |
| "gcc" / "consumer chains" | `gcc-auto` |
| "lebanese run" / biggest LB companies | `lb-enterprise` |
| "lb receptionist" | `lb-receptionist` |
| "worldwide" | `worldwide-receptionist` |
| Lebanon fintech/AML/bank-compliance | `lb-fintech-compliance` |
| Lebanon supermarkets/food retail | `lb-supermarkets` |
| Lebanon insurers/TPA/hospitals | `lb-insurance-tpa` |
| Lebanon FMCG distributors/industrial | `lb-fmcg-distributors` |
| Lebanon construction/engineering | `lb-construction` |
| Lebanon restaurants/hotels/beach clubs | `lb-restaurants-hotels` |
| Lebanon NGOs/international orgs | `lb-ngos` |
| explicit slug (e.g. `2026-06-23-eu-hotels`) | — |

- **Every fire = a COMPLETELY NEW dated run folder, launched immediately via `run_fire.py`. Act, don't analyze; never reuse/resume an existing folder.**
- **Named target → fire at once, no questions.** **"US" with no vertical → ASK which of 4.** **Bare "fire run" → ASK which campaign** (no silent default). **Bare "lebanon" → ASK lb-enterprise vs lb-receptionist.** The routing question is the only permitted pause; after it, fire.

---

## 5. Scripts: LIVE vs legacy

**ORCHESTRATORS:** `run_fire.py` (deterministic, runs everything below) + `agent_dispatch.py` (its headless dispatch engine). **LIVE stage scripts (in order):** `source_overpass.py` (Stage 2 map method — exhaustive OSM enumeration per city for any tag selector/vertical, region-by-region per-vertical city ledger, ZERO LLM tokens; selected by `sourcing.json` `"method":"map"`; validated 2026-07-17: 3 cities → 272 usable hotel candidates in ~30 s vs 108 merged from 141 Haiku agents on the DDG path, and 44 Chicago law firms with the same code) · `merge_candidates.py` · `fetch_html.sh` (→ `fetch_html.py`) · `extract_leads.py` · `qualify_leads.py` · `enrich_contact_person.py` · `draft_emails.py` / `draft_lead_custom.py` / `draft_custom.py` · `draft_whatsapp_custom.py` / `draft_whatsapp.py` (WA) · `sync_brevo_events.py` (bounce/block/spam suppression sync, runs before every live send) · `send_batch_brevo.py` · `persist_sent_log.py` · `gen_run_readme.py` · `new_campaign.py` (scaffolds a complete new-campaign fixture in one command — see templates/README.md "Creating a new campaign"). **Shared libs (imported):** `email_utils.py` (incl. the broad `COUNTRY_NAMES_ISO` map both drafters use), `email_template.py`.

**🗄️ LEGACY (Maps/MailScout/funnel era) — DELETED from the tree on 2026-08-05.** None were reachable from `/fire`, so they were dead weight a reader had to disambiguate from the live chain: `run_campaign.py`, `balanced_funnel.py`, `build_funnel_config.py`, `funnel_lib.py`, `run_funnel_dry_run.py`, `score_leads.py`, `draft_qualified.py`, `draft_from_pass.py`, `enrich_emails*.py`, `resolve_emails.py`, `extract_signals.py`, `qualify_signals.py`, `recover_signals.py`, `filter_maps_leads.py`, `source_directories.py`, `source_fit_filter.py`, `batch_leads.py`, `send.py`, `send_via_brevo.py`, `send_mockup.py`, `send_eligibility_gate.py`, `apply_claude_reviews.py`, `test-*`. They remain in git history (`git log --all -- project/tools/scripts/<name>`) if one is ever needed for reference. Every script now present under `tools/scripts/` is live.

---

## 6. Non-negotiable safety nets (current values)

| Rule | Value | Enforced in |
|---|---|---|
| Never re-contact a `sent-log.md` email | email-domain + `[[slug]]` + `dom@` tokens at merge; email-level at extract; **send-time suppression net** (sent-log + bounce-list) | `merge_candidates.py`, `extract_leads.py`, `send_batch_brevo.py` |
| **Fresh leads only, every fire** | contacted domains blocked FOREVER (sent-log nets); sourced-but-never-contacted (incl. searched-and-rejected) ALSO blocked **forever** by default (user directive 2026-07-17; ledger: `vault/lead-outreach/sourced-log.txt`); `SOURCED_SKIP_DAYS=<n>` restores an n-day retry window, `SOURCED_SKIP=off` disables for one run; merge WARNs to rotate queries when >50% of candidates are stale. Map-method sourcing ALSO applies both nets at source time, so `--max-candidates` counts only never-seen domains — each fire enters fetch with a full fresh batch | `merge_candidates.py`, `source_overpass.py` |
| **Per-run query scoping (search method)** | before Stage 2 dispatch, this run CLAIMS the queries this campaign has never fired, ledgers them one per line, and searches exactly those — so two runs of a campaign never cover the same ground and a run is never "affected by" an earlier one, it just owns a different slice. The query-granularity twin of the map method's per-city ledger. Ledger: `vault/lead-outreach/queries-fired-log.txt` (`slug_base\|query_hash\|date\|run_slug`, one row per query). `QUERY_REUSE_DAYS=<n>` re-opens queries older than n days; `QUERY_SCOPE=off` fires the pool as written. Pool fully spent → hard ABORT (exit 6) telling the operator to add new queries: a run that can search nothing must not burn a fan-out pretending otherwise. Never a warning, never a question — replaced the 2026-07-17 unrotated-queries warning, which halted a 2026-07-26 lb-insurance-tpa fire to ask the operator and violated "every fire is independent of prior runs" | `run_fire.py::scope_run_queries`, mirrored in `fire.md` Step 1.6 |
| Bounce/block/spam/unsub = dead-letter forever | synced from Brevo events into `bounce-list.md` before every live send | `sync_brevo_events.py`, `send_batch_brevo.py` |
| Constructed emails need URL evidence + live domain | `pattern_inferred`/`reconstructed` without an http(s) `email_source_url` are dropped; every contact domain must have MX/A; constructed ⇒ confidence ≤ medium | `enrich_contact_person.py` |
| Degraded fan-out = halt | enrich outputs < 60% of batches (`ENRICH_MIN_COMPLETION`) or >30% dispatch failures ⇒ ABORT | `enrich_contact_person.py`, `run_fire.py` |
| Min extract score | **82** (`QUALIFY_MIN_SCORE`); person=90 / role=82 / personal=78 base | `qualify_leads.py`, `extract_leads.py` |
| Drop freemail-only (`email_class: personal`) | always | `qualify_leads.py` |
| Skip `modern_booking` signal | always (gap already solved) | `extract_leads.py` |
| Per-run gate (e.g. hotel size ≥ medium) | `<run>/qualify.json` | `qualify_leads.py` |
| Enrichment queue ordered by traffic tier | always, all campaigns, no config | `qualify_leads.py` |
| Traffic floor (per-campaign, opt-in) | `<run>/qualify.json` `min_traffic_tier`; `unknown` exempt (near-empty-HTML fetches only, 0.6% of domains measured — NOT partial fetches, which silently drop a tier for ~17.7% and get no protection); drops NOT ledgered | `qualify_leads.py` |
| Decision-maker DIRECT email required | generic mailboxes never sent | `name-finder` + `enrich_contact_person.py --merge` |
| Phone/WhatsApp enrichment | **OPT-IN** (`--enrich-phone`, only when WhatsApp on) | `enrich_contact_person.py`, `name-finder.md` |
| Enrich cap (top-N by fit) | **`ENRICH_MAX_LEADS`, default 250** (safety ceiling; per-run `enrich_cap.txt` overrides) — see §7 | `qualify_leads.py` |
| Send cap | `MAX_EMAILS_PER_RUN` env (default 1000) | `send_batch_brevo.py` |
| Brevo send failure → ABORT (exit 6), never persist | always. Fixed 2026-08-11: this was documented but only half-true — failures were recorded as `result: "failed"` and the script exited **0**, so the run printed `DONE … sent+persisted` having sent nothing. The DONE line now reports the real count from `emails-sent.jsonl` | `send_batch_brevo.py`, `run_fire._send_outcome` |
| Channel-only runs skip the gates of channels they don't use | `li_only` (LinkedIn) and `wa_only` (WhatsApp) — a WhatsApp-only run gets `--wa-fallback` and skips the email drafter, instead of being emptied by a direct-email requirement it never needed | `run_fire.py` |
| A map sweep yielding only NAME-ONLY rows is success, not failure | exit 8 (no fresh ground) vs exit 1 (anomalous), decided by `sweep_outcome()`. Name-only rows are yield: Stage 2.5 resolves them | `source_overpass.py`, `source_places.py` |
| Kill on any fallback | always | `/fire` orchestrator |

---

## 7. Conversion levers (where leads are won/lost)

The funnel is `sourced → fetched → extracted → qualified → enriched → sent`. The two biggest losses are **not** quality drops:

1. **The enrich cap** (`ENRICH_MAX_LEADS`, default 250 — a safety ceiling, rarely binding) can hold back qualified leads on a very large extract. Set per-run via `enrich_cap.txt`.
2. **Enrichment email-resolution rate** (~60%) — leads die at 5.5 when no DIRECT decision-maker email is findable. Improving search precision here (verbatim-first, skip the phone ladder on email-only runs) raises sends without lowering quality.

Sourcing **precision** governs how many *sourced* leads survive to *qualified*: the OSM tag selector in the fixture's `sourcing.json` plus the criteria in `icp.yaml`. A loose selector floods the funnel with rows that die at Stage 5.3 having already cost a fetch; a tight, vertical-specific one keeps the survival rate high. (Until 2026-08-05 this stage was a `source-agent` sub-agent issuing search queries. It was replaced by deterministic OSM enumeration — same ground, zero LLM tokens, no query-precision tuning.)

---

## 8. Wall-clock target

Sourcing 1–3 min · fetch 1–2 min · enrich (biggest fan-out) 1–4 min · all Python stages <10 s each · Brevo send <1 s/1000. **Total 5–12 min.** >15 min ⇒ investigate the slowest stage.
