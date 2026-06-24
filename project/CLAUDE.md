# Lead Outreach System (Free Stack Edition)

You are operating a lead outreach pipeline. The user finds qualified leads across Google Maps and the open web, scores them against a per-run ICP, analyzes business gaps, and sends personalized cold emails via Brevo. (Instagram and LinkedIn channels were intentionally removed — see "the free sourcing stack" below.)

This is the **zero-cost-scraper** edition. All sourcing runs through free open-source tools — no Apify, no SerpAPI, no paid APIs.

## ✉️ Salutation contract (non-negotiable)

**Every email sent by this pipeline must open with a personal salutation: `Hello Mr. <Surname>,` / `Hello Mrs. <Surname>,` or `Hi Mr. <Surname>,` / `Hi Mrs. <Surname>,`.** Never bare `Hello,` / `Hi,` and never `<Business> team,` — those are forbidden openers.

This forces every run to resolve, per lead:

1. **Contact-person name** (first + last) for the decision-maker we want to reach at the business
2. **Gender** of that person (boy/girl → Mr./Mrs.)
3. A **source URL** that we found the name on (so we can audit it later)

Any lead where the pipeline cannot resolve both name **and** gender with reasonable confidence is **dropped** at the draft stage — per [kill-on-fallback](memory:feedback_kill_on_fallback), we ship fewer personalized emails rather than degrade to generic ones.

This is implemented as **Stage 6.5: contact-person enrichment** in `PIPELINE.md`, executed via the `name-finder` Haiku sub-agent at `.claude/agents/name-finder.md`. The `/fire` orchestrator dispatches one `name-finder` agent per extracted lead in parallel, mirroring the Stage 2 source-agent fan-out.

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

Every value above is a real folder under `templates/`. To change a campaign's queries/ICP/voice, edit its fixture there — never a past run.

- **A specific target IS named** (e.g. "fire law firm run usa", "fire us staffing run",
  "fire a gcc run") → go to Step 2 and fire it. **No clarifying questions.**
- **"USA" / "US" named but NO vertical** (e.g. "fire a usa run", "fire the us campaign")
  → the US campaign has four verticals, so **ASK which one**: US law firms · staffing/
  recruiting · med spas/dental/clinics · property management. Then → Step 2.
- **NO target at all** (bare "fire run" / "fire it" / "go" / "ship it") → **DO NOT
  default. ASK first** via `AskUserQuestion`: which campaign? (US [then which vertical] ·
  GCC consumer chains · Lebanon · Worldwide · a new vertical). If they pick a brand-new
  vertical with no template yet, ask the 1-2 details needed (region + vertical) and tell
  them you'll set it up before firing. Only after they answer → Step 2.

### Step 2 — clone the FIXED template fixture into a new dated folder, then fire

The campaign config is a **fixed fixture in the file structure** at `templates/<SLUG_BASE>/`
— do **NOT** scan `runs/` or inspect previous runs. Set only `SLUG_BASE` (= the
template folder name from the Step-1 table) and run verbatim:

```bash
cd <home>/Desktop/lead-outreach-system/project
SLUG_BASE='us-law-firms'       # <-- the templates/<base> folder name from Step 1

# 1. FIXED template fixture — known path, no runs/ scan, no analysis.
TPL="templates/$SLUG_BASE"
{ [ -f "$TPL/icp.yaml" ] && [ -f "$TPL/queries.txt" ]; } \
    || { echo "ABORT: no template at $TPL (needs icp.yaml + queries.txt). Create the fixture there first."; exit 1; }

# 2. ALWAYS a completely new run folder — never reuse/overwrite (bump until unused).
SLUG="$(date +%Y-%m-%d)-$SLUG_BASE"; COUNTER=1
while [ -d "runs/$SLUG" ]; do
    SLUG="$(date +%Y-%m-%d)-$SLUG_BASE-$COUNTER"; COUNTER=$((COUNTER+1))
done
NEW="runs/$SLUG"; mkdir -p "$NEW"

# 3. Clone every control file in the fixture into the new run.
cp "$TPL"/* "$NEW"/ 2>/dev/null
echo "Bootstrapped $NEW from $TPL."
```

Then launch immediately (deterministic orchestrator): `python3 tools/scripts/run_fire.py "$SLUG"`.

Then hand off to the **deterministic orchestrator** — your job (the Claude turn) was
only to resolve the phrase → slug; execution has no AI brain:

```bash
python3 tools/scripts/run_fire.py "$SLUG"        # add --dry-run for a preview (no send)
```

`run_fire.py` runs the whole pipeline as pure Python and dispatches the Haiku
sub-agents itself via headless `claude -p` (`agent_dispatch.py`) — same agents,
same scripts, same quality as the in-session path, but ~0 orchestrator tokens and
fully reproducible. It reads `draft_mode.txt` / `channels.json` and picks the
template / combined-custom / WhatsApp path automatically. When a target was named,
do not stop in between. **Alternative (in-session):** invoke `/fire $SLUG` to run
the same stages through the Agent tool (read `.claude/commands/fire.md`) — useful
for debugging one stage. Use `--plan` to trace what a slug would run without executing.

### When to ASK instead of firing

**These are the ONLY reasons to pause. In every other case, fire immediately** —
a fresh dated run → `run_fire.py`. Do not invent other reasons to stop and analyze.

- **Bare fire phrase, no named target** → ask which campaign (Step 1). This is the
  one case that used to auto-default to GCC and now must ask.
- **Bare "lebanon"/"lb" with no other qualifier** → ASK which Lebanon run:
  `lb-enterprise` (WhatsApp, biggest companies, custom consultant pitch) or
  `lb-receptionist` (AI receptionist, phone-heavy SMBs). But "lebanese run" alone →
  `lb-enterprise`, no question.
- Named target has no template folder with `icp.yaml` + `queries.txt` → tell the
  user, set it up (or ask the missing region/vertical), then fire.
- `vault/lead-outreach/sent-log.md` missing (vault not initialized) → bootstrap vault first.
- User said "**dry-run**"/"**preview**" a fire → still a NEW dated run, launched at
  once with `python3 tools/scripts/run_fire.py <slug> --dry-run` (drafts, no send).

## Core principle: ARCHITECTURE.md is the map, fire.md is the playbook

On any trigger, read in this order:

1. **This file (`CLAUDE.md`)** — the natural-language fire ROUTER (below): which target/ICP a phrase maps to, and when to ASK vs fire.
2. **`project/ARCHITECTURE.md`** — the **canonical map**: file layout (live vs legacy), the agent roster (every sub-agent, its exact tools, when it deploys and how many), the current pipeline stages, the gates, and the conversion levers. If you're unsure where something lives or which script/agent is real, the answer is here.
3. **`project/.claude/commands/fire.md`** — the only executable playbook `/fire` follows, step by step.

`project/PIPELINE.md` is the long-form rationale for individual stages — read it only when you need the "why" behind one stage.

**Do not** follow `project/README.md` (2025 Maps/Instagram/LinkedIn "free-stack" edition — deprecated), `project/agents/*.md` (human-readable reference, not loaded as sub-agents), or `project/.claude/agents/personalizer.md` (reviewed-mode only). Sub-agent definitions that `/fire` actually dispatches live ONLY in the cwd-level `.claude/agents/`.

Long-term memory (sent history, voice, ICP) lives in the ONE canonical vault at `project/vault/lead-outreach/` (relative `vault/...` from the `project/` run cwd). There is exactly one sent-log; never reintroduce a second copy.

## The pipeline

```
1. Brief intake     → user states "find me [target], pitch on [gap]"
2. ICP definition   → icp-definition skill formalizes brief into scoring rubric
3. Sourcing         → free local tools pull raw leads
4. Dedup            → cross-check against vault/lead-outreach/sent-log.md
5. Scoring          → lead-scoring skill assigns 0-100 to each lead
6. Gap analysis     → business-gap-analysis skill researches qualified leads
7. Copywriting      → outreach-copywriting skill drafts personalized emails
8. Send             → brevo-send skill ships via Brevo MCP
9. Persist          → obsidian-memory writes everything back to vault
```

The `pipeline-orchestrator` skill runs this end-to-end.

## The free sourcing stack

| Channel | Tool | Notes |
|---|---|---|
| Google Maps | `gosom/google-maps-scraper` (Go binary in `./tools/`) | Free, MIT, 33+ data points, ~120 places/min, no API key |
| Web search | `ddgs` Python lib (DuckDuckGo) | Free, no key, no real limit |
| Web scraping | `crawl4ai` (Python) | Free, Apache 2.0, LLM-friendly markdown |

Instagram and LinkedIn sourcing have been removed from this stack — the account-flag risk and credential overhead weren't worth it for the user's typical briefs. Do not attempt to re-introduce them: the `lead-sourcing-instagram` and `lead-sourcing-linkedin` skills are intentionally absent. If a brief genuinely needs B2B-on-LinkedIn or creator-on-IG targets, tell the user that channel is disabled and suggest reframing via web search instead.

All remaining tools are installed by the `setup-tools` skill on first run. The skill writes a `tools/` folder with everything ready to go.

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
