# Lead Outreach System — Architecture (canonical map)

**The single source of truth for "what runs, where the files are, which agents deploy with which tools."** `CLAUDE.md` is the router and the contracts; this file is the map; `.claude/commands/fire.md` is a note for debugging one stage by hand.

> **One line:** fire phrase → `fire_campaign.sh <base>` clones `templates/<base>/` into a dated `runs/` folder → `run_fire.py` (pure Python, 0 orchestrator tokens) runs: source (gmaps / overture / map / places / directory, 0 agents) → merge+dedup → [resolve domains] → fetch HTML → extract+score → qualify+cap → **enrich decision-maker (one name-finder fan-out + parallel SMTP rescue)** → draft → bounce-sync + Brevo batch send → persist → cleanup. Email by default; WhatsApp / LinkedIn opt-in per fixture. Halts on any fallback.

## 0. Read order on any trigger

1. `project/CLAUDE.md` — router + contracts. 2. This file. 3. The last run folder (`runs/<slug>/status.txt` first) — the code says what it intends, the run says what happened.

There is ONE execution path: `run_fire.py`, dispatching sub-agents headlessly through `agent_dispatch.py` (`claude -p`). The in-session Agent-tool fan-out was retired 2026-09-02 (see fire.md).

## 1. File map

```
<repo-root>/                                  ← SESSION cwd; .claude/ loads from here
├── .claude/settings.json                     permissions for the orchestrator session
├── .claude/agents/                           THE 6 sub-agent definitions (nowhere else)
├── .claude/commands/{li-fire,li-outreach,linkedin-run}.md
├── .fire-work/<slug>/                        per-agent batch/out files for a live fire
│                                             (gitignored; deleted at run end; outside
│                                             project/ so no CLAUDE.md is attached to
│                                             sub-agents as nested memory)
├── linkedin-run                              LinkedIn entry point (venv + Chrome + handoff)
├── li-search/                                separate LinkedIn people-search tool
└── project/                                  run cwd for every stage script
    ├── CLAUDE.md · ARCHITECTURE.md           operating doc · this map
    ├── .claude/commands/fire.md              debugging note (not an orchestrator)
    ├── tools/scripts/                        every file here is LIVE (see §5)
    ├── tools/venv/                           python deps; every stage runs on its python3
    ├── tools/google-maps-scraper             gosom binary, primary Stage-2 source
    ├── templates/<base>/                     FIXED fixtures; fire clones from here only
    ├── runs/YYYY-MM-DD-<slug>/               per-run artifacts (§3)
    ├── vault/lead-outreach/                  THE ONLY vault: sent-log.md, bounce-list.md,
    │                                         sourced-log.txt, disqualified-log.txt,
    │                                         overpass-cities-fired.txt, voice*.md, offer.md
    ├── vault-template/                       skeletons bootstrap_vault.sh installs
    ├── linkedin/ · bridge/                   LinkedIn channel · WhatsApp channel
    ├── tests/                                pytest, no network
    └── .env                                  BREVO_MCP_TOKEN, BREVO_SENDER_EMAIL/NAME/ADDRESS,
                                              BREVO_REPLY_TO, GOOGLE_PLACES_API_KEY,
                                              OBSIDIAN_VAULT_PATH, MAX_EMAILS_PER_RUN
```

## 2. Agent roster (from each file's frontmatter in `<repo-root>/.claude/agents/`)

| Agent | Model | Tools | Stage | Deployed when | How many |
|---|---|---|---|---|---|
| `name-finder` | haiku | Read, WebSearch, WebFetch, Write | 5.5 | every email/WA run except the combined custom path | 1 per qualified lead not already named from its email |
| `lead-writer` | sonnet | Read, Write, WebSearch, WebFetch | 6.5C | `draft_mode=custom` AND email-only (US verticals) — replaces name-finder + gap-writer | 1 per qualified lead |
| `gap-writer` | sonnet | Read, Write, WebSearch, WebFetch | 6 | `draft_mode=custom` AND the run also uses WhatsApp | 1 per lead with contact |
| `wa-writer` | sonnet | Read, Write, WebSearch, WebFetch | 8.5 | WhatsApp channel + custom mode | 1 per company with a CEO mobile |
| `li-finder` | haiku | Read, WebSearch, WebFetch, Write | 5.6 / 8.6b | LinkedIn email-rescue (opt-in) / company page yielded no person | 1 per uncovered company |
| `li-writer` | sonnet | Read, Write | 8.6c / handoff | LinkedIn channel | 1 per qualified person |

**How they are launched.** `agent_dispatch.py` runs `claude -p` per prompt with the agent's `tools:` as `--allowedTools`, `cwd` = repo root, and `--setting-sources project --strict-mcp-config --disable-slash-commands --exclude-dynamic-system-prompt-sections --effort $AGENT_EFFORT` (default `low`), `AGENT_WORKERS` (default 32) in parallel. Prompts carry a file PATH, never page text: the agent Reads its batch file from `.fire-work/<slug>/` and Writes its JSON next to it. Batch files live outside `project/` precisely so `project/CLAUDE.md` is not attached as nested memory (measured 2026-09-02: 664/665 agents carried it, ≈7.5k tokens each). `name-finder.md`'s `OPTIONAL:phone` block is stripped unless the run has WhatsApp. `run_fire.py` aborts before sourcing if `agent_dispatch.foreign_memory_files()` finds a foreign `CLAUDE.md` up the parent chain (`ALLOW_FOREIGN_CLAUDE_MD=1` overrides).

**Names come from the email first, agents second.** Stage 5.5 prep runs `name_from_email.py` over each lead's own mailbox and harvested person-format addresses; a lead whose name parses strictly gets no agent (`derived-contacts.json`) and faces every downstream gate identically. `DERIVE_NAMES_FROM_EMAIL=0` disables.

## 3. Stages, owners, rationale

| # | Stage | Owner | Output | Halt if |
|---|---|---|---|---|
| 0 | Bootstrap | `fire_campaign.sh` (validates fixture, clones, auto-suffixes slug) | `runs/<slug>/` | fixture missing a required file |
| 0-LI | LinkedIn preflight | `linkedin/scripts/preflight.js` (linkedin runs only) | — | Chrome/session down (relay its `fix:` line) |
| 2 | Source | `sourcing.json` sources run **concurrently** (`SOURCE_WORKERS`, default 4): `gmaps` → `source_gmaps.py` (primary: website + category in one pass) · `overture` → `source_overture.py` · `map` → `source_overpass.py` · `places` → `source_places.py` · `directory` → `source_directory.py`. 0 agents. Each source applies the sent/sourced/disqualified nets and its city ledger at source time | `candidates-batch-*.txt` | every source dry (exit 8 = no fresh ground, not a failure) → Stage 3 zero gate |
| 2.5 | Resolve domains | `resolve_domains.py` (`RESOLVE_WORKERS`) for name-only rows (places Pro tier, `require_website:false`); overlapped with a pre-fetch | resolved rows appended | — |
| 3 | Merge+dedup | `merge_candidates.py` — sent-log, sourced-log, disqualified nets; country scope from `countries.txt` / `icp.yaml geography` (GCC default) | `candidates-all.txt` | **0** merged (<50 prints a NOTE) |
| 4 | Fetch | `fetch_html.sh` → `fetch_html.py` (crawl4ai, /, /contact, /about, /team…) | `raw_html/` | never; WARN <40% (`FETCH_YIELD_WARN_PCT`) |
| 5 | Extract+score | `extract_leads.py` (emails, signals, traffic tier; person 90 / role 82 / personal 78 base) | `leads-extracted.json` | 0 |
| 5.3 | Qualify+cap | `qualify_leads.py` — drop freemail-only, below `QUALIFY_MIN_SCORE` (82), dead signals, per-run `qualify.json`; rank by traffic tier; cap **`ENRICH_MAX_LEADS` default 400** (`enrich_cap.txt` / `--target-leads` override) | `leads-qualified.json` | 0 (exit 7) |
| 5.5 | Enrich decision-maker | `enrich_contact_person.py --phase prep` → **one name-finder fan-out** (full tools; the offline tier-1 was deleted 2026-09-02: 11/337 closed for 10.3M tokens) → `--phase merge`, which runs the **SMTP rescue in parallel** (`smtp_email_probe.py`, `SMTP_WORKERS` 16, 3 s connect timeout; skipped when preflight finds port 25 blocked, `SMTP_PROBE=0`), retires no-email domains to `disqualified-log.txt` | `leads-with-contact.json`, `enrich-summary.json`, `leads-dropped.json` | 0 with name + direct email; <60% agent completion (`ENRICH_MIN_COMPLETION`) |
| 5.6 | LinkedIn email rescue (opt-in `linkedin-email-lookup`) | `resolve_li_profiles.py` → li-finder → contact-info lookup → rescue merge | merged into 5.5 output | — |
| 6 | Draft | `draft_emails.py` (template + `pitch.json`, salutation gate) · `draft_custom.py` (gap-writer) · `draft_lead_custom.py` (6.5C lead-writer, replaces 5.5+6) | `emails-drafted.json` | 0 drafts |
| 7 | Send | `sync_brevo_events.py` then `send_batch_brevo.py --send --cap` (`MAX_EMAILS_PER_RUN`) | `emails-sent.jsonl`, `send-log.txt` | Brevo 4xx/5xx → persist what sent, THEN exit 6 |
| 8 | Persist | `persist_sent_log.py` (rows with `result == sent`) | `vault/.../sent-log.md` | — |
| 8.5 | WhatsApp (opt-in) | wa-writer → `draft_whatsapp_custom.py` / `draft_whatsapp.py` → `bridge/send_campaign.js` | `whatsapp-sent.jsonl` | 0 drafts / send fails |
| 8.6 | LinkedIn (opt-in) | `draft_linkedin.py --prep` → `walk_companies.js` → 8.6b li-finder → 8.6c `qualify_people.py` → li-writer → `linkedin_queue.py` → 8.6d `queue/generate*.js` | `linkedin/state/backlog.json`, queues | 0 people qualified |
| 9 | Cleanup | `run_fire.py` deletes `raw_html/` and `.fire-work/<slug>/` on every exit path (normal, `die()`, and a >6 h stale sweep at the next fire); `KEEP_RUN_ARTIFACTS=1` keeps them | — | — |

**Why the stages are shaped this way.** Sourcing is deterministic because agent-per-query search (retired 2026-08-05) cost ~130 Haiku agents per fire for worse recall than one enumeration. Qualify runs *before* any fan-out so the most expensive work is never spent on leads about to be dropped. Enrichment insists on a DIRECT email with evidence because generic mailboxes and guessed addresses bounce and cost the sending domain. The `qualified-pending` backlog was removed 2026-08-05: 92% of replayed leads (3,760/4,095) died for the same unfixable reason (decision-maker found, no direct email); those domains are retired instead. Instagram sourcing and person-first LinkedIn scraping are deliberately absent. `places` defaults to Pro tier because the Places ToS makes only the place ID storage-eligible; `resolve_domains` proves the domain independently instead. Send-before-persist ordering on a partial Brevo failure is deliberate: the messages Brevo really delivered must reach `sent-log.md` before the run dies. LinkedIn queues rather than sends (8-18 invites/day, 90/week).

**Run control files** (cloned from the fixture — see `templates/README.md`): `icp.yaml`, `sourcing.json`, `draft_mode.txt`, `channels.json`, `countries.txt`, `pitch.json`, `vertical.txt`, `qualify.json`, `target_roles.txt`, `places.txt`, `enrich_cap.txt`, `linkedin.json`.

**Run artifacts kept:** `status.txt`, `candidates-all.txt`, `leads-extracted.json`, `leads-qualified.json`, `leads-with-contact.json`, `leads-dropped.json`, `derived-contacts.json`, `enrich-summary.json`, `emails-drafted.json`, `emails-sent.jsonl`, `send-log.txt`, `source-attribution.tsv`, `whatsapp-*`. Bulk intermediates (`raw_html/`, `.fire-work/<slug>/`) are removed at run end.

## 4. Routing

`CLAUDE.md` is authoritative. Named target → fire at once; "US" without vertical → ask which of 4; bare "fire it" → ask which campaign; bare "lebanon" → ask enterprise vs receptionist. Every fire is a new dated folder.

## 5. Scripts and knobs

**Orchestration:** `fire_campaign.sh` → `run_fire.py` (+ `agent_dispatch.py`). **Every stage subprocess runs under `tools/venv/bin/python3`** (`run_fire.py::_interpreter()`); bare `python3` is the system interpreter and lacks `ddgs`/`crawl4ai`/`duckdb`. **Stage scripts:** `source_gmaps.py`, `source_overture.py`, `source_overpass.py`, `source_places.py`, `source_directory.py`, `resolve_domains.py`, `merge_candidates.py`, `fetch_html.sh`/`.py`, `extract_leads.py` (+`traffic_signals.py`), `qualify_leads.py`, `enrich_contact_person.py` (+`name_from_email.py`, `smtp_email_probe.py`), `resolve_li_profiles.py`, `draft_emails.py`, `draft_custom.py`, `draft_lead_custom.py`, `draft_whatsapp*.py`, `sync_brevo_events.py`, `send_batch_brevo.py`, `persist_sent_log.py`, LinkedIn: `draft_linkedin.py`, `qualify_people.py`, `linkedin_queue.py`, `li_handoff.py`, `import_lisearch.py`, `li_url.py`. **Setup:** `new_campaign.py`, `bootstrap_vault.sh`. **Libs / shared presets:** `email_utils.py`, `email_template.py`, `gmaps_presets.json`, `overture_presets.json` (search terms / categories per vertical, shared by every fixture). Legacy scripts were deleted 2026-08-05 (git history has them).

| Env knob | Default | Effect |
|---|---|---|
| `AGENT_WORKERS` | 32 | parallel headless agents per fan-out |
| `AGENT_EFFORT` | `low` | `--effort` passed to every headless agent |
| `SOURCE_WORKERS` | 4 | Stage-2 sources run concurrently |
| `RESOLVE_WORKERS` | 8 | Stage-2.5 resolver threads |
| `SMTP_WORKERS` | 16 | parallel SMTP rescue probes (3 s connect timeout) |
| `SMTP_PROBE` | 1 | `0` skips SMTP rescue (auto when port 25 is blocked) |
| `ENRICH_MAX_LEADS` | 400 | Stage-5.3 cap (`enrich_cap.txt` / `--target-leads` override) |
| `ENRICH_MIN_COMPLETION` | 0.6 | abort when fewer agents return |
| `MAX_EMAILS_PER_RUN` | 1000 | Stage-7 send cap |
| `KEEP_RUN_ARTIFACTS` | unset | `1` keeps `raw_html/` + `.fire-work/` |
| `SOURCED_SKIP_DAYS` / `SOURCED_SKIP=off` | forever | retry window for sourced-but-never-contacted domains |
| `DERIVE_NAMES_FROM_EMAIL` | 1 | `0` sends every lead to an agent |
| `ALLOW_FOREIGN_CLAUDE_MD` | unset | fire despite a foreign memory file in a parent dir |

## 6. Non-negotiable safety nets

| Rule | Enforced in |
|---|---|
| Never re-contact a `sent-log.md` email/domain; sourced-but-never-contacted and retired domains blocked too | sources, `merge_candidates.py`, `extract_leads.py`, `send_batch_brevo.py` |
| Bounce/block/spam/unsub = dead-letter forever (synced before every live send) | `sync_brevo_events.py`, `send_batch_brevo.py` |
| Constructed emails need an `http(s)` `email_source_url` + live MX/A domain; confidence ≤ medium | `enrich_contact_person.py` |
| Decision-maker DIRECT email required; generic mailboxes never sent | `name-finder` + merge |
| Degraded fan-out = halt (<60% agent outputs) | `enrich_contact_person.py` |
| Min score 82; freemail-only dropped; `modern_booking` skipped; per-run `qualify.json` gate; traffic-tier ordering | `qualify_leads.py`, `extract_leads.py` |
| Phone/WhatsApp enrichment OPT-IN (`--enrich-phone`, WhatsApp runs only) | `enrich_contact_person.py` |
| Brevo failure → persist what sent, then exit 6; DONE line reports the real count | `run_fire.py` Stage 7/8 |
| Commercial email carries a postal address (`BREVO_SENDER_ADDRESS`); WARN when unset (CAN-SPAM) | `send_batch_brevo.py`, `email_template._footer` |
| Channel-only runs skip the gates of channels they don't use (`li_only`, `wa_only`) | `run_fire.py` |
| Kill on any fallback; never silently switch paths | `run_fire.py` |

## 7. Conversion levers and the baseline to beat

Funnel: sourced → merged → extracted → qualified → contact → sent. The dominant loss is Stage 5.5 (no findable direct email); the second is sourcing precision (a loose selector floods Stage 5.3 with rows that already cost a fetch).

**Measured baseline — `2026-09-02-gcc-receptionist`:** 858 candidates → 390 extracted → 339 qualified → 82 with contact → 82 drafted → 80 sent, in 119 min, 663 headless agents, 21.7M written tokens. Of the 82 contacts: 48 `pattern_inferred`, 24 `verbatim`, 5 `reconstructed_from_mask`, 5 `smtp_verified` (71 probed). 257 leads retired as unreachable. **Beat this** — on wall-clock and tokens first (the single fan-out, concurrent sources, parallel SMTP and `.fire-work/` exist for that), then on the 82/339 contact rate.

## 8. Wall-clock

Sourcing 1-3 min · fetch 1-2 min · enrich is the long pole (agents + SMTP) · Python stages <10 s each · Brevo <1 s/1000. A run past the baseline's 119 min has a stalled stage — read `status.txt`.
