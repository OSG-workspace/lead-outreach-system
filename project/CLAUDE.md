# Lead Outreach System (Free Stack Edition)

You are operating a lead outreach pipeline. The user finds qualified leads across Google Maps and the open web, scores them against a per-run ICP, analyzes business gaps, and sends personalized cold emails via Brevo. Email is the volume channel; WhatsApp and LinkedIn are opt-in arms of the same chain, selected per fixture in `channels.json`. (Instagram was intentionally removed — see "the free sourcing stack" below.)

This is the **zero-cost-scraper** edition. All sourcing runs through free open-source tools — no Apify, no SerpAPI, no paid APIs.

## ✉️ Salutation contract (non-negotiable)

**Every email sent by this pipeline must open with a personal salutation: `Hello Mr. <Surname>,` / `Hello Mrs. <Surname>,` or `Hi Mr. <Surname>,` / `Hi Mrs. <Surname>,`.** Never bare `Hello,` / `Hi,` and never `<Business> team,` — those are forbidden openers.

**One-name-is-enough exception (2026-07-22 user directive: surname isn't that
important; a name found via the email alone is enough).** Small owner-operated
businesses (tradies, independent shops) often publish a personal email
(`dave@business.com`, `smith@business.com`) but never write the person's full
name anywhere indexed. When a **verified direct email** (verbatim or
evidence-reconstructed, same bar as any send-eligible address) confirms EITHER
name component, that alone is send-eligible — the full first+last name is
preferred, not required:

- **First name known** (surname unrecoverable) → opener `Hello <First>,` / `Hi <First>,`.
  Gender/Mr./Mrs. not needed for this form.
- **Surname known + gender assignable** (first name unrecoverable) → opener
  `Hello Mr./Mrs. <Surname>,` as normal.

Still search for the full name first — this is a fallback after a genuine
search, not a shortcut. A name with NO verified direct email is still dropped
(unchanged) — this exception only ever lowers the salutation bar, never the
email-verification bar.

This forces every run to resolve, per lead:

1. **Contact-person name** (full name preferred; at minimum one component confirmed by the verified direct email) for the decision-maker we want to reach at the business
2. **Gender** of that person (boy/girl → Mr./Mrs.) — required only when the salutation uses the surname
3. A **source URL** that we found the name on (so we can audit it later)

Any lead where the pipeline cannot resolve at least one name component with reasonable confidence, backed by a verified direct email, is **dropped** at the draft stage — per [kill-on-fallback](memory:feedback_kill_on_fallback), we ship fewer personalized emails rather than degrade to generic ones.

This is implemented as **Stage 6.5: contact-person enrichment** in `PIPELINE.md`, executed via the `name-finder` Haiku sub-agent at `.claude/agents/name-finder.md`. The `/fire` orchestrator dispatches one `name-finder` agent per extracted lead in parallel, mirroring the Stage 6 writer fan-out.

## ⚡ Natural-language fire trigger (ROUTER)

> **DIRECTIVE — ACT, DON'T ANALYZE.** A fire phrase ALWAYS starts a **completely
> new run**: bootstrap a **fresh dated run folder** and launch it **immediately**
> via `run_fire.py`. This is mechanical and decisive — do **NOT** deliberate,
> re-read the codebase, weigh options, summarize, or "think it over." Identify the
> target → clone a new dated folder → launch. **Never** reuse, resume, or overwrite
> an existing run folder; **every fire = a new dated slug.** The ONLY permitted
> pause is the single routing question below, and ONLY when the target is genuinely
> unnamed — that one question sets scope, it is not analysis. After it's answered (or
> if the target was named), fire at once with no further questions.
>
> **Every fire is INDEPENDENT of every previous run.** Prior runs of the same
> campaign — earlier today, still in progress, or however many exist — are
> IRRELEVANT to a new fire and are never a reason to pause, inspect, or ask
> ("already fired today?" is a forbidden question). Do NOT scan `runs/`, count
> today's sends, or wait for an in-progress run to finish; a new fire runs in
> parallel, in its own fresh folder. The ONLY thing a new run takes from past
> runs is the dedup state, and that is enforced mechanically by the vault
> ledgers (`sent-log.md`, sourced-log, `overpass-cities-fired.txt`,
> `queries-fired-log.txt`) — no previously contacted or sourced lead can
> resurface and each run is scoped to query/city ground no earlier run covered,
> so re-firing is always safe by construction. Those ledgers are applied
> silently inside `run_fire.py`. They are NEVER surfaced as a warning, a
> question, or an "are you sure" — if a ledger leaves a run nothing to search,
> the run ABORTS with a precise root cause instead of asking.
>
> **Re-fires are ALWAYS legitimate — never mention them.** A campaign fired N
> times today (or ever) is NOT a reason to warn, ask, hesitate, or even remark
> "this was already fired today" (user directive 2026-07-17). This extends to
> anything DERIVED from prior runs: never ask about unrotated/stale/byte-identical
> queries, overlapping ground, or expected low yield, and never offer a
> "fire anyway vs rotate first" choice (user directive 2026-07-26). The pipeline
> scopes each run to fresh ground by itself. Repeat fires are
> the NORMAL way to top up volume: the sent-log + sourced-log nets guarantee
> each new run touches only never-seen businesses, so freshness is enforced by
> the chain, not by the router. `fire_campaign.sh` auto-suffixes the slug
> (`-1`, `-2`, …); runs are parallel and independent. The only per-lead
> restriction that exists is the dedup nets themselves.

A fire phrase in ANY session (no slash command needed) starts a run. Fire phrases:
"fire ... run", "fire it", "fire the pipeline", "let's fire", "send a campaign",
"send out a batch", "go" (when context is clearly the pipeline), "ship it".

**The trigger is a ROUTER.** First identify the TARGET from the user's words,
then act. Do NOT silently default a bare phrase to GCC.

### Step 1 — identify the target from the phrase

Scan the user's message for a named campaign / vertical / region.

**The US campaign has FOUR verticals** (each its own template folder). A US fire
must name BOTH "us"/"usa"/"states" (or a US-only vertical word) AND which vertical:

`SLUG_BASE` is the **`templates/<base>` fixture folder** to clone (one fixed path; no `runs/` scan):

| If the phrase names… | `SLUG_BASE` (clone `templates/<base>`) |
|---|---|
| US + "law firm(s)" / "lawyers" / "law" | `us-law-firms` |
| US + "staffing" / "recruiting" / "recruiter" | `us-staffing` |
| US + "med spa" / "clinic" / "dental" / "medspa" | `us-clinics` |
| US + "property" / "property management" | `us-property` |
| "gcc" / "consumer chain(s)" | `gcc-auto` |
| "eu hotel(s)" / "european hotels" / hotels in ES/IT/FR | `eu-hotels` |
| "lebanese" / "lebanese run" / "biggest lebanese companies" | `lb-enterprise` |
| "lb receptionist" / "receptionist lebanon" | `lb-receptionist` |
| bare "lebanon" / "lb" (no other word) | ASK: `lb-enterprise` or `lb-receptionist` |
| "worldwide" | `worldwide-receptionist` |
| "linkedin" / "li run" / "fire linkedin" / "gcc linkedin" | `gcc-outreach-li` |
| "australia" / "australian" / "ai receptionist australia" / AU trades ("aussie plumbers", "australian tradies") | `au-trades` |
| "fintech" / "money transfer" / "AML" / "bank compliance" (Lebanon) | `lb-fintech-compliance` |
| "supermarkets" / "food retail" (Lebanon) | `lb-supermarkets` |
| "insurance" / "insurers" / "TPA" / "hospitals" (Lebanon) | `lb-insurance-tpa` |
| "FMCG" / "distributors" / "industrial groups" (Lebanon) | `lb-fmcg-distributors` |
| "construction" / "engineering" / "contractors" (Lebanon) | `lb-construction` |
| "restaurants" / "hotels" / "beach clubs" (Lebanon, NOT "lb receptionist") | `lb-restaurants-hotels` |
| "NGOs" / "international organizations" (Lebanon) | `lb-ngos` |

Every value above is a real folder under `templates/`. To change a campaign's queries/ICP/voice, edit its fixture there — never a past run.

**Generic rule (covers campaigns not in the table):** if the phrase names any
existing `templates/<base>` folder (its base name or an unambiguous part of
it, e.g. "fire the us hvac run" → `us-hvac`), fire that base — the table
above never needs a new row for a new campaign. `ls templates/` is the
authoritative list.

- **A specific target IS named** (e.g. "fire law firm run usa", "fire us staffing run",
  "fire a gcc run") → go to Step 2 and fire it. **No clarifying questions.**
- **"USA" / "US" named but NO vertical** (e.g. "fire a usa run", "fire the us campaign")
  → the US campaign has four verticals, so **ASK which one**: US law firms · staffing/
  recruiting · med spas/dental/clinics · property management. Then → Step 2.
- **NO target at all** (bare "fire run" / "fire it" / "go" / "ship it") → **DO NOT
  default. ASK first** via `AskUserQuestion`: which campaign? (US [then which vertical] ·
  GCC consumer chains · Lebanon · Worldwide · a new vertical). If they pick a brand-new
  vertical with no template yet, follow the **new-campaign contract** below before firing.
  Only after they answer → Step 2.

### New-campaign contract (TEMPLATE-FIRST — non-negotiable)

The system must **never invent email copy at fire time**. Freeform generation
during outreach produces generic emails and is the #1 quality risk. So when the
user asks for a **new campaign kind** (a vertical/region with no `templates/<base>/`
folder yet), set it up in THIS order, before any optimizing of queries/ICP:

0. **Scaffold the branch with ONE command** — it writes the complete general
   fixture structure (sourcing.json, countries.txt, icp.yaml skeleton, …)
   and prints exactly what still blocks a fire:
   ```bash
   python3 tools/scripts/new_campaign.py --base <base> --vertical <v> \
       --countries <ISO2[,ISO2…]> --selector '"amenity"="dentist"'
   ```
   The scaffold NEVER invents copy or places: fire refuses until the user's
   approved `pitch.json` (step 1-3 below) and real places exist. A
   half-finished fixture always aborts loudly with the exact missing file —
   the one chain treats every campaign identically.

1. **Ask for their email template first.** "Do you already have the email you
   want this campaign to send?" If yes → store it verbatim as
   `templates/<base>/pitch.json` (`subject_template` + `body_template`, slots
   `{salutation}` `{name}` and optionally `{opener}` `{vertical}` `{country}`),
   exactly like the AI-receptionist campaigns (`eu-hotels`, `lb-receptionist`).
2. **If they have none → draft 2-3 example templates** (different angles for
   their vertical) and present them via `AskUserQuestion` for a choice. The
   examples must be non-generic and human: no em/en dashes, no banned words
   (see `vault/lead-outreach/voice-us.md`), evidence-shaped opener, one CTA,
   one link. **Never wire a template the user has not confirmed.**
3. **Always offer refinement** after they pick: apply their word changes to the
   chosen template before saving it. The saved `pitch.json` is then FIXED copy.
4. **Per-business variation happens ONLY through template slots**, never by
   rewriting the email: the `{opener}` slot + `signal_openers` map in
   `pitch.json` lets a few words vary with the specific business's detected
   flaw (phone-led, contact form, no online booking, …) while the rest of the
   approved copy stays byte-identical. Offer this as an option when saving.
5. **`draft_mode=template` is the default for every new campaign.** The
   freeform per-business path (`draft_mode=custom`, lead-writer/gap-writer) is
   opt-in ONLY when the user explicitly asks for fully custom emails, and its
   voice contract (`voice-us.md`) + merge gates stay mandatory.

### Step 2 — fire: ONE command, zero deliberation

Every campaign's config is a **fixed, fire-ready fixture** at `templates/<SLUG_BASE>/`.
Once Step 1 resolved the base, execution is exactly ONE command — do NOT scan
`runs/`, inspect previous runs, read pipeline code, or rebuild any bash:

```bash
cd <your-checkout>/project
bash tools/scripts/fire_campaign.sh <SLUG_BASE>          # add --dry-run to preview, --plan to trace
```

**Lead target in the phrase → `--target-leads N`.** If the fire phrase names a
lead volume ("fire australia run, 100 leads", "target 50 leads", "send to 30",
"get me 200 leads"), append `--target-leads N` to the command. The orchestrator
then scales the sourcing fan-out itself — query agents dispatch in adaptive
waves until enough qualified leads exist (N / survival estimate) or the
fixture's queries run out — and caps the send at exactly N. More target = more
agents, smaller target = fewer agents, automatically. No number in the phrase =
classic full-queries run. A shortfall (queries exhausted below target) is
reported loudly in status.txt and the final DONE line — relay it to the user.

`fire_campaign.sh` does everything mechanically: validates the fixture is
fire-ready (icp + queries; `pitch.json` for template mode — the template-first
contract is enforced here; `vertical.txt` for custom mode), clones it into a
brand-new dated `runs/` folder (never reuses one), and hands off to the
deterministic orchestrator `run_fire.py` (pure Python, headless sub-agent
dispatch, ~0 orchestrator tokens). If anything is missing it ABORTs with the
exact file to create — that is the ONLY case where you stop and talk to the user.

**LinkedIn runs (`channels.json` contains `"linkedin"`) end differently — say so.**
A LinkedIn fire **queues, it does not send**: the channel drips 8-18 invites a
day, 90 a week, so one fire loads weeks of supply into
`linkedin/state/backlog.json`. `drafted=0` and "sent 0" are CORRECT there and
must not be reported as a failure. `run_fire.py` runs `linkedin/scripts/preflight.js`
FIRST on these runs (before any sourcing, ~5s) — if it aborts, relay its exact
`fix:` line, which is almost always `bash linkedin/scripts/start_chrome.sh`.
After the fire, the send side is ONE command, and you can run it yourself:

```bash
cd project/linkedin && bash scripts/daily.sh --live   # invites + sweep + DMs
```

Report the LinkedIn result as: N people queued, M directly messageable today,
and roughly how many weeks the backlog covers at the current ramp.

**Live status — NEVER go dark during a run (user directive).** A fire takes
5-12 minutes; the user must see progress, not silence. Launch the command with
`run_in_background: true` (ONE background Bash process is fine — the
no-background rule applies to Agent-tool fan-outs, not to this single process).
Then, while it runs, poll the run's heartbeat roughly every 60-90 seconds:

```bash
cat runs/<slug>/status.txt     # run_fire.py overwrites this at every stage + agent completion
```

and relay a ONE-LINE update to the user each time it changes stage (e.g.
"Sourcing: 41/96 agents done" → "Merged 214 fresh candidates, fetching HTML" →
"Enriching decision-makers: 30/80" → "Sent 46, persisting"). On completion,
give the Step-10-style final report. If status.txt shows `ABORTED: …`, surface
the exact abort line immediately.

**Alternative (in-session, debugging one stage only):** bootstrap with the same
script using `--plan`, then run `/fire <slug>` per `.claude/commands/fire.md`.

### When to ASK instead of firing

**These are the ONLY reasons to pause. In every other case, fire immediately** —
a fresh dated run → `run_fire.py`. Do not invent other reasons to stop and analyze.
The existence of previous runs (same campaign, same day, even one still running)
is explicitly NOT a reason to pause — the dedup ledgers make re-firing safe.

- **Bare fire phrase, no named target** → ask which campaign (Step 1). This is the
  one case that used to auto-default to GCC and now must ask.
- **Bare "lebanon"/"lb" with no other qualifier** → ASK which Lebanon run:
  `lb-enterprise` (WhatsApp, biggest companies, custom consultant pitch) or
  `lb-receptionist` (AI receptionist, phone-heavy SMBs). But "lebanese run" alone →
  `lb-enterprise`, no question.
- Named target has no template folder with `icp.yaml` + `sourcing.json` → tell
  the user, set it up (or ask the missing region/vertical), then fire.
- `vault/lead-outreach/sent-log.md` missing (vault not initialized) → bootstrap vault first.
- User said "**dry-run**"/"**preview**" a fire → still a NEW dated run, launched at
  once with `python3 tools/scripts/run_fire.py <slug> --dry-run` (drafts, no send).

## Core principle: ARCHITECTURE.md is the map, fire.md is the playbook

On any trigger, read in this order:

1. **This file (`CLAUDE.md`)** — the natural-language fire ROUTER (below): which target/ICP a phrase maps to, and when to ASK vs fire.
2. **`project/ARCHITECTURE.md`** — the **canonical map**: file layout (live vs legacy), the agent roster (every sub-agent, its exact tools, when it deploys and how many), the current pipeline stages, the gates, and the conversion levers. If you're unsure where something lives or which script/agent is real, the answer is here.
3. **`project/.claude/commands/fire.md`** — the only executable playbook `/fire` follows, step by step.

`project/PIPELINE.md` is the long-form rationale for individual stages — read it only when you need the "why" behind one stage.

Sub-agent definitions that `/fire` actually dispatches live ONLY in the repo-root `.claude/agents/` — six of them, and nothing else in the tree defines one. (The deprecated `project/README.md`, the reference-only `project/agents/*.md`, and `personalizer.md` were deleted on 2026-08-05; if a doc still points you at them, it is stale.)

Long-term memory (sent history, voice, ICP) lives in the ONE canonical vault at `project/vault/lead-outreach/` (relative `vault/...` from the `project/` run cwd). There is exactly one sent-log; never reintroduce a second copy.

## The pipeline

The live pipeline is the /fire path documented in `ARCHITECTURE.md` §3:
source fan-out → merge+dedup (sent-log + sourced-log freshness) → fetch →
extract → qualify+cap → enrich decision-maker → draft (template or custom) →
bounce-sync + Brevo batch send → persist → cleanup. The old skills-era
9-step pipeline (icp-definition / lead-scoring / pipeline-orchestrator
skills) is retired — those skills are archived and must not be invoked.

## The free sourcing stack

| Channel | Tool | Notes |
|---|---|---|
| Google Maps | `gosom/google-maps-scraper` (Go binary in `./tools/`) | Free, MIT, 33+ data points, ~120 places/min, no API key |
| Web search | `ddgs` Python lib (DuckDuckGo) | Free, no key, no real limit |
| Web scraping | `crawl4ai` (Python) | Free, Apache 2.0, LLM-friendly markdown |

Instagram sourcing has been removed from this stack — the account-flag risk and credential overhead weren't worth it for the user's typical briefs. The `lead-sourcing-instagram` skill is intentionally absent; if a brief genuinely needs creator-on-IG targets, say the channel is disabled and suggest web search instead.

**LinkedIn is LIVE again (2026-07-31) and is a full arm of the one chain** — see
`project/linkedin/README.md`. It is NOT a sourcing channel: sourcing stays
deterministic OSM enumeration exactly like every other campaign, and LinkedIn is
only the *outreach surface*, reached by walking an already-qualified company to
its people. Opt in with `channels.json` = `["linkedin"]`. The old
`lead-sourcing-linkedin` skill stays absent — do not reintroduce person-first
scraping.

All of these are installed by `bash project/tools/install.sh` — it builds the venv (crawl4ai, ddgs), and pulls or compiles the `google-maps-scraper` binary into `tools/`. (The old `setup-tools` skill is retired along with the rest of the skills era.)

## 🔍 VERIFY AGAINST THE LAST RUN BEFORE ASSERTING ANYTHING (user directive 2026-07-31)

**Never describe how this pipeline behaves from the code, the docs, or this file.
Read the last run's artifacts first.** Every stage writes its own evidence, and
the code says what it *intends*, not what it *did*. Claims made without opening a
run folder have repeatedly been wrong in the same direction: optimistic.

Before any diagnosis, plan, design, or answer about pipeline behaviour, open the
most recent runs for the campaign in question and read:

| Artifact | The question it answers |
|---|---|
| `runs/<slug>/status.txt` | where the run ended, or what aborted it |
| `runs/<slug>/candidates-all.txt` | how many candidates sourcing ACTUALLY produced |
| `runs/<slug>/leads-extracted.json` | how many survived fetch + extraction |
| `runs/<slug>/leads-qualified.json` | how many qualified this run (all freshly sourced — nothing is re-injected) |
| `runs/<slug>/enrich-summary.json` | the per-gate drop counts for enrichment |
| `runs/<slug>/leads-dropped.json` | the per-lead `agent_reason` — WHY leads died |
| `runs/<slug>/emails-sent.jsonl` | what was really sent |
| `vault/lead-outreach/overpass-cities-fired.txt` | which ground is already spent, per vertical |
| `vault/lead-outreach/disqualified-log.txt` | domains retired forever (proven unqualified, or no reachable direct email) |

**Sourcing exhaustion HALTS the run, and that is the correct behaviour
(2026-08-05).** A campaign whose ledgered ground is fully swept has nothing to
fire, so `run_fire.py` aborts at the Stage 3 / Stage 5 / Stage 5.3 zero-count
gate and names the fix. When a fire reports `0 candidates`, that is a **supply**
signal to act on (add `places.txt` rows, add targets, add a `places` source), not
evidence the run was broken.

The `qualified-pending.jsonl` backlog that used to rescue this path was
**removed**. It replayed leads that qualified but were never contacted — and
measured across every run on disk, 92% of those (3,760 / 4,095) died for one
reason that never changes on a retry: the decision-maker was found but publishes
no direct email. Three fires on 2026-08-05 each spent ~2 hours and 208
name-finder agents on the SAME 208 hotels. Those domains are now retired to
`disqualified-log.txt` at Stage 5.5 instead, so they are never sourced again.

**Two sources exist for Stage 2, declared per fixture in `sourcing.json`:**
`{"type": "map"}` enumerates OpenStreetMap (free, zero tokens) and
`{"type": "places"}` enumerates Google Places (needs `GOOGLE_PLACES_API_KEY`,
$32/1k Pro-tier requests, the default). OSM indexes PREMISES, so any ICP without
a shopfront is largely absent from it — measured 2026-08-03, all of Australia
holds 161 trade websites in OSM. Reach for a `places` source whenever a
map-sourced campaign's yield collapses. A fixture may declare a places source
**before** the key exists: without it that source reports no fresh ground and
the run carries on.

**Places defaults to Pro tier, never Enterprise, and that default is load-bearing —
do not "fix" a places source by adding `"tier": "enterprise"` without reading
`resolve_tier()` in `source_places.py` first.** Independent research (2026-08-03)
found the Places API (New) ToS treats only the place ID as storage-eligible
indefinitely; the website/phone fields Enterprise tier returns are not, and this
pipeline persists everything it sources into `vault/lead-outreach/sourced-log.txt`
and `sent-log.md` forever. Pro tier never requests those fields, so there is
nothing non-compliant to store: `resolve_sourcing()` in `run_fire.py` auto-enables
Stage 2.5 (`resolve_domains.py`) for any places source not pinned to Enterprise,
which independently proves each business's domain via search + fetch +
name-token match against its own site — the same verification already used for
weakly-mapped OSM markets — rather than trusting Google's returned field.
Enterprise tier still exists as an explicit per-source opt-in for a campaign that
genuinely needs the phone field, but choosing it is a deliberate ToS/legal
tradeoff the fixture author must make on purpose, never a silent default.

Compare **several consecutive fires**, not one. Saturation and regressions only
show up as a trend. Cheap way to see the whole shape at once:

```bash
cd project/runs && for d in $(ls -1t | head -12); do
  printf "%-30s cand=%-5s extr=%-5s qual=%-5s contact=%-4s sent=%s\n" "$d" \
    "$(grep -c . $d/candidates-all.txt 2>/dev/null)" \
    "$(grep -c . $d/leads-extracted.json 2>/dev/null)" \
    "$(grep -c . $d/leads-qualified.json 2>/dev/null)" \
    "$(grep -c . $d/leads-with-contact.json 2>/dev/null)" \
    "$(grep -c . $d/emails-sent.jsonl 2>/dev/null)"; done
```

Two traps this exists to catch, both real:
- **`qualified` used to hide a sourcing failure.** Until 2026-08-05 Stage 5.3
  re-injected the pending backlog, so a run that sourced 5 candidates reported
  `qualified=179` (2026-07-31-au-trades did exactly that). The backlog is gone
  and the two numbers now agree, but still judge sourcing by
  `candidates-all.txt`, never by the DONE line.
- **A healthy-looking DONE line can sit on top of a dead stage.** The same run
  reported `qualified=179 drafted=7 sent+persisted` while its OSM ground was
  100% exhausted and 96% of its enrichment fan-out returned nothing.

If a run folder contradicts a claim in this file or in ARCHITECTURE.md, the run
folder is right — fix the doc.

## Reality checks (be honest with the user)

- **Google Maps**: completely fine. No login. Run as much volume as your machine handles. Email extraction is built in.
- **Web**: completely fine. No login. Use `ddgs` for discovery + `crawl4ai` for fetching.

## Approval mode

The system has **two run modes**:

| Mode | How to invoke | Approval gate | Default cap | Sender |
|---|---|---|---|---|
| **Reviewed** | `run-kickoff.md` (default for "run a campaign") | Shows 3 sample emails, asks yes/no before Stage 7 | 30 | `send.py` (paced, one-by-one) |
| **Auto-fire** | `/fire <slug>` or "just send, no approval" | NONE — pre-authorized by the command | `MAX_EMAILS_PER_RUN` env, default 1000 | `send_batch_brevo.py` (Brevo `messageVersions` batch endpoint, 1 HTTP call / 1000 emails) |

A fire trigger is pre-authorized — do not stop to confirm. Execute via the deterministic
orchestrator `python3 tools/scripts/run_fire.py <slug>` (or the in-session `/fire <slug>` →
`.claude/commands/fire.md`). (`agents/run-auto.md` is legacy reference, not the live path.)

Safety nets that apply to BOTH modes (non-negotiable, no approval needed to enforce them):

- Every email is logged to `vault/lead-outreach/sent-log.md` after the send call fires
- Dedup against `sent-log.md` is enforced at extract time
- Below-threshold leads (score < 70) are NOT contacted — only persisted to vault for review
- `email_class: personal` leads are dropped at draft time
- `modern_booking` signal → skip the lead (gap already solved)

## Run artifacts

Each run creates a folder under `runs/YYYY-MM-DD-<slug>/` with:
- `brief.md` — the user's original instruction
- `icp.yaml` — the scoring rubric used
- `leads-raw.json` — pre-dedup, pre-scoring sourcing output
- `leads-scored.json` — all leads with scores and reasons
- `gaps/<lead-slug>.md` — one gap analysis per qualified lead
- `emails-sent.json` — final sent emails with Brevo message IDs

These are gitignored. Aggregate insights are written back to the vault.

## When in doubt

- Read the relevant SKILL.md fully before acting.
- Read the vault before asking the user a question that might already be answered there.
- When sourcing returns junk, lower the search scope and try again rather than asking the user to refine.
- Never invent emails. If a lead has no public email, mark `email_status: missing` and skip the send.
- If a free tool fails (rate-limited, blocked), fall back to a different sourcing channel rather than aborting the run.

## MCP servers in use

- **Brevo** — email send + tracking (only paid thing in the stack — Brevo's own free tier is 300/day)
- **MCPVault** — Obsidian read/write

Sourcing tools are NOT MCPs — they're local CLI binaries / Python scripts the skills invoke via bash. This is intentional: it keeps the system free and self-contained.
