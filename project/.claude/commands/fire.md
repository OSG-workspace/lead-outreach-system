---
name: fire
description: Auto-pilot the lead-outreach pipeline. /fire <slug> dispatches one Haiku source-agent per query (max parallelism), then merges → fetches → extracts → drafts → sends via Brevo batch (+ optional WhatsApp) → persists. Halts on any fallback. No approval prompt. Default ICP is GCC consumer chains; per-run config can switch source-agent (e.g. source-agent-lb for Lebanon AI-receptionist) and enable WhatsApp.
---

# /fire — Haiku-DDG parallel sourcing pipeline

You are the orchestrator. The user typed `/fire <slug>` (or `/fire` to operate on the latest GCC run). Take the run from sourcing to sent **without stopping to ask, and without ever silently degrading**. They pre-authorized by invoking the command.

This command runs the **Haiku-DDG path**: one `source-agent` Haiku sub-agent **per single query** (maximum parallelism). The Maps/`run_campaign.py` path is deprecated and must not be invoked from `/fire`.

## File structure (must be in place before /fire runs)

```
<home>/Desktop/lead-outreach-system/
├── .claude/
│   ├── settings.json                  # permissions: WebSearch, WebFetch, Bash, Write, etc.
│   └── agents/
│       └── source-agent.md            # the locked-tool sourcing sub-agent (WebSearch + Write only)
└── project/
    ├── .claude/
    │   └── commands/fire.md           # THIS file
    ├── tools/scripts/
    │   ├── merge_candidates.py        # Stage 3
    │   ├── fetch_html.sh              # Stage 4 (multi-page)
    │   ├── extract_leads.py           # Stage 5
    │   ├── draft_emails.py            # Stage 6 TEMPLATE mode (role-class ≥85 gate baked in)
    │   ├── draft_custom.py            # Stage 6 CUSTOM mode (gap-writer fan-out; draft_mode=custom)
    │   ├── send_batch_brevo.py        # Stage 7 (Brevo messageVersions)
    │   └── persist_sent_log.py        # Stage 8
    ├── vault/lead-outreach/sent-log.md  # dedup source of truth
    └── runs/YYYY-MM-DD-<slug>/        # per-run artifacts
```

If any required file is missing → ABORT with the missing path. Do not improvise.

## Critical rules

- **Locked tools**: the `source-agent` Haiku sub-agent has only `WebSearch` + `Write` in its tools list. It physically cannot reach for Bash or run ddgs/crawl4ai. The orchestrator must NOT add tools to the sub-agent's prompt.
- **Kill on fallback** (memory: `kill-on-fallback`, `no-path-swap-on-fallback`): if any stage degrades, HALT with `ABORT: <stage> <root cause>`. Never silently switch paths.
- **No approval prompts.** User pre-authorized at invocation time.
- **One query per sub-agent.** Max parallelism. ~130 queries → ~130 parallel Haiku agents in one batch.
- **One ICP per run.** Default is GCC consumer-chains via `source-agent`. Lebanon AI-receptionist uses `source-agent-lb` (set via `<run>/source_agent.txt`). Other ICPs must be added as a new source-agent variant before /fire can target them. Do NOT silently degrade or mix ICPs within one run.
- **Dispatch economics (why this command is safe on a Sonnet primary).** Sub-agents are Haiku and billed as Haiku; the orchestrator can run on any session model (Sonnet is fine). The ONE thing that makes the orchestrator expensive is dispatch mode. `run_in_background: true` wakes the primary loop and makes it re-read its full, growing context **once per sub-agent completion** — up to ~840 times per run across the two fan-outs (Step 3 + Step 6.5) — each re-read billed at the primary model's rate. That is what exhausted the plan usage limit on a Sonnet primary; a Haiku primary survived only because the identical re-reads were billed ~3× cheaper. **Always dispatch the fan-outs FOREGROUND, ≤50 Agent calls per assistant message.** Foreground calls in one message run concurrently and return together in ONE continuation, so the primary re-reads its context ~once per batch (~a dozen times per run) instead of ~840. Never set `run_in_background` on the Step 3 / Step 6.5 dispatches.

## Halt table

| Stage | Halt condition |
|---|---|
| Pre-flight | `.claude/agents/source-agent.md` missing, `.claude/agents/name-finder.md` missing, `.claude/settings.json` missing WebSearch |
| Sourcing | < 50 merged candidates after dedup |
| HTML fetch | < 40% of candidates yielded ≥1 fetched page |
| Extract | 0 leads in `leads-extracted.json` |
| Qualify+cap (Stage 5.3) | 0 leads survive the qualify gate → `leads-qualified.json` empty (exit 7) |
| Enrich (Stage 5.5) | 0 leads with resolved contact-person + Mr./Mrs. → `leads-with-contact.json` empty |
| Combined custom (6.5C) | 0 drafts after the contact+email contract gate (`emails-drafted.json` empty, exit 5) |
| Draft | 0 drafts in `emails-drafted.json` |
| Brevo send | any HTTP 4xx/5xx, or `result=failed` in `send-log.txt` |
| WhatsApp draft (custom) | `draft_whatsapp_custom.py` merge produced 0 drafts after the contract gate |
| WhatsApp send (if enabled) | `send_campaign.js` exits non-zero, or `whatsapp-sent.jsonl` has 0 `result=sent` lines |

## Procedure

### Step 0 — pre-flight (global)

```bash
# Settings must include WebSearch (sub-agents inherit this)
grep -q '"WebSearch"' <home>/Desktop/lead-outreach-system/.claude/settings.json \
    || { echo "ABORT: .claude/settings.json missing WebSearch in permissions.allow"; exit 1; }

# name-finder is required for every run (Stage 5.5)
[ -f <home>/Desktop/lead-outreach-system/.claude/agents/name-finder.md ] \
    || { echo "ABORT: .claude/agents/name-finder.md missing (Stage 5.5 contact enrichment)"; exit 1; }
```

### Step 1 — resolve run slug, pick source-agent

```bash
SLUG="$1"
if [ -z "$SLUG" ]; then
    SLUG=$(ls runs/ | grep -E '^[0-9]{4}-' | grep -E 'gcc|consumer-chain' | sort | tail -1)
fi
RUN="runs/$SLUG"

[ -d "$RUN" ] || { echo "ABORT: $RUN does not exist."; exit 1; }
[ -f "$RUN/icp.yaml" ] && [ -f "$RUN/queries.txt" ] \
    || { echo "ABORT: $RUN missing icp.yaml or queries.txt."; exit 1; }

# Pick source-agent variant. Default = source-agent (GCC).
SOURCE_AGENT="source-agent"
if [ -f "$RUN/source_agent.txt" ]; then
    SOURCE_AGENT=$(head -1 "$RUN/source_agent.txt" | tr -d ' \n')
fi
[ -f "<home>/Desktop/lead-outreach-system/.claude/agents/${SOURCE_AGENT}.md" ] \
    || { echo "ABORT: source-agent definition missing: .claude/agents/${SOURCE_AGENT}.md"; exit 1; }

echo "Firing campaign: $RUN"
echo "Source agent:    $SOURCE_AGENT"
echo "Queries: $(wc -l < $RUN/queries.txt)"
```

### Step 1.5 — path-aware pre-flight (FAIL FAST, before any agent is dispatched)

Resolve which path this run uses, then validate **every** script, sub-agent
definition, and voice spec that path will touch. This turns a mid-run failure
(after dispatching dozens of Haiku agents) into a clean one-line ABORT up front.
Run this verbatim:

```bash
PROJ=<home>/Desktop/lead-outreach-system
ABORT(){ echo "ABORT (pre-flight): $1"; exit 1; }

# --- resolve the path from the run's control files ---
SOURCE_AGENT="source-agent"
[ -f "$RUN/source_agent.txt" ] && SOURCE_AGENT=$(head -1 "$RUN/source_agent.txt" | tr -d ' \n')
DRAFT_MODE="template"
[ -f "$RUN/draft_mode.txt" ] && DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')
EMAIL_ENABLED=1; WA_ENABLED=0
if [ -f "$RUN/channels.json" ]; then
    grep -q '"email"' "$RUN/channels.json" || EMAIL_ENABLED=0
    grep -q '"whatsapp"' "$RUN/channels.json" && WA_ENABLED=1
fi
COMBINED_CUSTOM=0
[ "$DRAFT_MODE" = "custom" ] && [ "$EMAIL_ENABLED" = "1" ] && [ "$WA_ENABLED" = "0" ] && COMBINED_CUSTOM=1
echo "Pre-flight path: source=$SOURCE_AGENT draft=$DRAFT_MODE email=$EMAIL_ENABLED wa=$WA_ENABLED combined=$COMBINED_CUSTOM"

# --- core scripts every run needs (Stages 3-8) ---
for s in merge_candidates.py fetch_html.sh extract_leads.py qualify_leads.py \
         enrich_contact_person.py send_batch_brevo.py persist_sent_log.py email_template.py; do
    [ -f "$PROJ/project/tools/scripts/$s" ] || ABORT "missing core script tools/scripts/$s"
done

# --- the resolved source-agent must exist ---
[ -f "$PROJ/.claude/agents/${SOURCE_AGENT}.md" ] || ABORT "source-agent definition missing: .claude/agents/${SOURCE_AGENT}.md"

# --- path-specific agents + scripts + voice specs ---
if [ "$COMBINED_CUSTOM" = "1" ]; then
    [ -f "$RUN/vertical.txt" ] && [ -n "$(head -1 "$RUN/vertical.txt" | tr -d ' \n')" ] \
        || ABORT "combined custom run needs a non-empty vertical.txt"
    [ -f "$PROJ/.claude/agents/lead-writer.md" ] || ABORT "missing .claude/agents/lead-writer.md (combined custom path)"
    [ -f "$PROJ/project/tools/scripts/draft_lead_custom.py" ] || ABORT "missing tools/scripts/draft_lead_custom.py"
    [ -f "$PROJ/project/vault/lead-outreach/voice-us.md" ] || ABORT "missing vault/lead-outreach/voice-us.md (lead-writer reads it)"
elif [ "$EMAIL_ENABLED" = "1" ] && [ "$DRAFT_MODE" = "custom" ]; then
    # custom email that ALSO uses WhatsApp -> name-finder + gap-writer two-step
    [ -f "$PROJ/.claude/agents/name-finder.md" ] || ABORT "missing .claude/agents/name-finder.md"
    [ -f "$PROJ/.claude/agents/gap-writer.md" ] || ABORT "missing .claude/agents/gap-writer.md"
    [ -f "$PROJ/project/tools/scripts/draft_custom.py" ] || ABORT "missing tools/scripts/draft_custom.py"
    [ -f "$RUN/vertical.txt" ] && [ -n "$(head -1 "$RUN/vertical.txt" | tr -d ' \n')" ] || ABORT "custom run needs vertical.txt"
    [ -f "$PROJ/project/vault/lead-outreach/voice-us.md" ] || ABORT "missing vault/lead-outreach/voice-us.md"
elif [ "$EMAIL_ENABLED" = "1" ]; then
    # template email
    [ -f "$PROJ/.claude/agents/name-finder.md" ] || ABORT "missing .claude/agents/name-finder.md"
    [ -f "$PROJ/project/tools/scripts/draft_emails.py" ] || ABORT "missing tools/scripts/draft_emails.py"
fi

# --- WhatsApp channel deps (only if enabled) ---
if [ "$WA_ENABLED" = "1" ]; then
    [ -f "$PROJ/.claude/agents/name-finder.md" ] || ABORT "missing name-finder.md (WhatsApp needs leads-with-contact.json)"
    if [ "$DRAFT_MODE" = "custom" ]; then
        [ -f "$PROJ/.claude/agents/wa-writer.md" ] || ABORT "missing .claude/agents/wa-writer.md (custom WhatsApp)"
        [ -f "$PROJ/project/tools/scripts/draft_whatsapp_custom.py" ] || ABORT "missing tools/scripts/draft_whatsapp_custom.py"
        [ -f "$PROJ/project/vault/lead-outreach/voice-lb-wa.md" ] || ABORT "missing vault/lead-outreach/voice-lb-wa.md (wa-writer reads it)"
    else
        [ -f "$PROJ/project/tools/scripts/draft_whatsapp.py" ] || ABORT "missing tools/scripts/draft_whatsapp.py"
    fi
    [ -f "$PROJ/project/bridge/send_campaign.js" ] || ABORT "missing project/bridge/send_campaign.js (WhatsApp send)"
fi

echo "Pre-flight OK: every file this path needs is present."

# Refresh this run's README manifest (structure + exact scoring fields per task)
# from the resolved control files, so the folder always documents its own path.
python3 tools/scripts/gen_run_readme.py --run-dir "$RUN"
```

If anything is missing, the run stops here with the exact path, before a single
Haiku agent is dispatched. Only continue past this point on `Pre-flight OK`.

### Step 2 — split queries 1-per-batch (maximum sub-agents)

```bash
# Clean prior artifacts so /fire starts fresh
rm -f "$RUN"/queries-batch-* "$RUN"/candidates-batch-* "$RUN"/candidates-all.txt
rm -rf "$RUN"/raw_html
rm -f "$RUN"/leads-extracted.json "$RUN"/leads-with-contact.json
rm -f "$RUN"/enrich-batch-* "$RUN"/enrich-out-*
rm -f "$RUN"/emails-drafted.json "$RUN"/emails-sent.jsonl "$RUN"/send-log.txt

# 1 query per batch → 1 sub-agent per query → max parallel Haiku agents
split -l 1 "$RUN/queries.txt" "$RUN/queries-batch-"
BATCHES=$(ls "$RUN"/queries-batch-* | wc -l | tr -d ' ')
echo "Will dispatch $BATCHES parallel source-agent sub-agents (1 query each)."
```

### Step 3 — dispatch ALL sub-agents in parallel (FOREGROUND, batched assistant messages)

Issue the `Agent` tool calls **foreground** — do NOT set `run_in_background`. Foreground calls in one assistant message run concurrently and their results return together in a **single** continuation turn, so the primary context is re-read ~once per batch instead of once per agent (see "Dispatch economics" above — this is what keeps a Sonnet orchestrator under the usage limit). Concurrent dispatch is also what makes it fast — sequential defeats the purpose.

**Batch size:** put up to **50** Agent calls in one assistant message. If there are more than 50 `queries-batch-*` files, send several sequential messages of ≤50 calls each. The harness concurrency-caps parallel sub-agents regardless, so 50/message yields the same true parallelism as 600/message while keeping each message — and each primary context re-read — bounded.

**Pick which source-agent variant to use** based on the run folder:

```bash
SOURCE_AGENT="source-agent"  # default = GCC consumer-chains
if [ -f "$RUN/source_agent.txt" ]; then
    SOURCE_AGENT=$(head -1 "$RUN/source_agent.txt" | tr -d ' \n')
fi
echo "Using sub-agent: $SOURCE_AGENT"
```

Currently shipped variants:
- `source-agent` — GCC multi-location consumer chains (default)
- `source-agent-lb` — Lebanon AI-receptionist ICP (phone-heavy single- and multi-location businesses)
- `source-agent-worldwide` — worldwide receptionist ICP (any country; used by the EU-hotels run)
- `source-agent-us` — small US service businesses (law firms, staffing, med spas/dental/clinics, property mgmt) for the US inbox/scheduling gap-based campaign
- `source-agent-lb-enterprise` — biggest Lebanese companies/brands for the WhatsApp custom-consultant pitch

If `$RUN/source_agent.txt` doesn't exist, the default `source-agent` is used.

For each `queries-batch-XX` file, dispatch ONE Agent call with this exact shape:

- **subagent_type**: `$SOURCE_AGENT`  ← whichever variant was resolved above
- **model**: `haiku`  ← sub-agents stay Haiku regardless of the session/orchestrator model
- **run_in_background**: **omit it** — dispatch foreground. Do NOT set `run_in_background: true`; that re-invokes the primary loop once per completion and is what blew the Sonnet usage limit.
- **prompt** (minimal — the agent definition has the full role baked in):
  ```
  Query: <the one query in this batch>
  OutputFile: /absolute/path/to/runs/<slug>/candidates-batch-XX.txt
  ```

That's it. Do NOT inline the source-agent instructions into the prompt — they live in `.claude/agents/<source-agent>.md` and are loaded automatically when the matching `subagent_type` is used.

After all agents complete, count outputs:

```bash
TOTAL_RAW=$(cat "$RUN"/candidates-batch-*.txt 2>/dev/null | grep -c '^[a-z0-9]')
echo "Raw candidates: $TOTAL_RAW (pre-dedup)"
```

### Step 4 — merge + dedup

```bash
python3 tools/scripts/merge_candidates.py \
    --run-dir "$RUN" \
    --sent-log vault/lead-outreach/sent-log.md

MERGED=$(wc -l < "$RUN/candidates-all.txt")
[ "$MERGED" -ge 50 ] || { echo "ABORT: only $MERGED merged candidates (need >=50)."; exit 2; }
```

### Step 5 — multi-page HTML fetch

```bash
bash tools/scripts/fetch_html.sh "$RUN"

DOMAINS_WITH_HTML=$(find "$RUN/raw_html" -name "*.html" 2>/dev/null | sed 's/.*\///;s/__.*//' | sort -u | wc -l | tr -d ' ')
MIN_FETCH=$(( MERGED * 40 / 100 ))
[ "$DOMAINS_WITH_HTML" -ge "$MIN_FETCH" ] || { echo "ABORT: only $DOMAINS_WITH_HTML / $MERGED domains fetched (<40%)."; exit 3; }
```

### Step 6 — extract

```bash
python3 tools/scripts/extract_leads.py \
    --run-dir "$RUN" \
    --sent-log vault/lead-outreach/sent-log.md

EXTRACTED=$(wc -l < "$RUN/leads-extracted.json")
[ "$EXTRACTED" -ge 1 ] || { echo "ABORT: 0 leads extracted."; exit 4; }
```

### Step 6.4 — qualify + cap (Stage 5.3) — RUN ALWAYS, BEFORE any fan-out

This gate is what stops the pipeline from spending its most expensive work
(per-lead web-research agents) on leads it is about to drop. It runs with **zero
agents** — pure scoring on data already in the extract — disqualifies weak leads
(freemail-only, below score floor, dead signals), ranks by fit, and **caps to the
top-N** (`ENRICH_MAX_LEADS`, default **250** — a safety ceiling, not a throttle).
Every downstream fan-out reads `leads-qualified.json`, so the cap propagates. The
ceiling exists only so a pathological 1000-lead extract can't dispatch 1000
agents; in a normal run it lets **every qualified lead** through to outreach. A
run can override it with a one-line `<run>/enrich_cap.txt`.

```bash
# Per-run cap override (optional): runs/<slug>/enrich_cap.txt with a single integer.
[ -f "$RUN/enrich_cap.txt" ] && export ENRICH_MAX_LEADS="$(tr -dc 0-9 < "$RUN/enrich_cap.txt")"
python3 tools/scripts/qualify_leads.py --run-dir "$RUN"   # halts (exit 7) if 0 qualified
QUALIFIED=$(wc -l < "$RUN/leads-qualified.json")
echo "Qualified + capped: $QUALIFIED leads carried into enrichment (cap=${ENRICH_MAX_LEADS:-250})."
# If qualify_leads.py reported leads "CAPPED OFF", that many qualified leads were
# held back — raise enrich_cap.txt / ENRICH_MAX_LEADS to outreach them too.
```

### Step 6.45 — pick the per-lead path (custom-email vs template / WhatsApp)

Two enrichment paths exist. Detect channels + draft mode now so the rest of the
run knows which one to use:

```bash
EMAIL_ENABLED=1; WA_ENABLED=0
if [ -f "$RUN/channels.json" ]; then
    grep -q '"email"' "$RUN/channels.json" || EMAIL_ENABLED=0
    grep -q '"whatsapp"' "$RUN/channels.json" && WA_ENABLED=1
fi
DRAFT_MODE="template"
[ -f "$RUN/draft_mode.txt" ] && DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')

# COMBINED path = email-only custom run (the US gap verticals). ONE lead-writer
# Haiku per lead finds the contact AND writes the email in a single web pass,
# replacing the name-finder + gap-writer two-step (half the agents, half the web
# work). Any run with WhatsApp stays on the name-finder route below, because the
# WhatsApp drafters depend on leads-with-contact.json.
COMBINED_CUSTOM=0
if [ "$DRAFT_MODE" = "custom" ] && [ "$EMAIL_ENABLED" = "1" ] && [ "$WA_ENABLED" = "0" ]; then
    COMBINED_CUSTOM=1
fi
echo "Path: draft_mode=$DRAFT_MODE email=$EMAIL_ENABLED whatsapp=$WA_ENABLED combined_custom=$COMBINED_CUSTOM"
```

**If `COMBINED_CUSTOM=1`: SKIP Step 6.5 and Step 7 entirely** and run the
combined **Step 6.5C** below instead. **Otherwise** run Step 6.5 (name-finder)
then Step 7 as before.

### Step 6.5C — combined contact+email (custom email-only runs)

Require the vertical (same rule as the old custom draft path — never defaulted):

```bash
VERT=""
[ -f "$RUN/vertical.txt" ] && VERT=$(head -1 "$RUN/vertical.txt" | tr -d ' \n')
[ -n "$VERT" ] || { echo "ABORT: custom run has no vertical.txt."; exit 5; }

python3 tools/scripts/draft_lead_custom.py --phase prep --run-dir "$RUN"
LEAD_BATCHES=$(ls "$RUN"/lead-batch-*.txt | wc -l | tr -d ' ')
echo "Will dispatch $LEAD_BATCHES parallel lead-writer sub-agents."
```

Then **issue the `Agent` tool calls FOREGROUND, up to 50 per assistant message**,
one per `lead-batch-NNN.txt`. Same dispatch economics as Step 3 — never set
`run_in_background`. Each call: **subagent_type** `lead-writer`, **model**
`haiku`, **prompt** = the contents of `lead-batch-NNN.txt` verbatim. Do NOT inline
the lead-writer instructions — they live in `.claude/agents/lead-writer.md`.

```bash
python3 tools/scripts/draft_lead_custom.py --phase merge --run-dir "$RUN"
DRAFTED=$(wc -l < "$RUN/emails-drafted.json")
[ "$DRAFTED" -ge 1 ] || { echo "ABORT: 0 combined custom drafts after the contract gate."; exit 5; }
```

This writes `emails-drafted.json` directly (same schema as the other draft
modes), so **after Step 6.5C, jump straight to Step 8 (Brevo send)** — Steps 6.5
and 7 are already done by this one fan-out.

### Step 6.5 — contact-person enrichment (Stage 5.5)

Resolves a real Mr./Mrs. + Surname per lead so the salutation contract in CLAUDE.md is honored. Mirrors the source-agent fan-out from Step 3: one Haiku `name-finder` sub-agent **per lead**, dispatched **foreground** in batched assistant messages (≤50 calls each). This phase is the bigger fan-out — it can reach 600+ leads — so it is exactly where `run_in_background` previously exhausted the Sonnet usage limit. Never use `run_in_background` here (see "Dispatch economics" and Step 3).

**Phone enrichment is OPT-IN.** The name-finder mobile/WhatsApp ladder is the
single biggest source of wasted searches, and an email-only run never uses a
phone number. So pass `--enrich-phone` to the prep ONLY when this run actually
sends over WhatsApp (`WA_ENABLED=1`, set in Step 6.45). Without the flag, the prep
writes `EnrichPhone: no` into every batch and name-finder skips step 3 entirely.

```bash
# Phase A: write per-lead prep files for the orchestrator to dispatch from.
#          --enrich-phone ONLY when WhatsApp is the (or a) channel for this run.
PHONE_FLAG=""; [ "${WA_ENABLED:-0}" = "1" ] && PHONE_FLAG="--enrich-phone"
python3 tools/scripts/enrich_contact_person.py --phase prep --run-dir "$RUN" $PHONE_FLAG
ENRICH_BATCHES=$(ls "$RUN"/enrich-batch-*.txt | wc -l | tr -d ' ')
ABS_RUN="$(cd "$RUN" && pwd)"   # name-finder runs from the SESSION root, so dispatch ABSOLUTE paths
echo "Will dispatch $ENRICH_BATCHES parallel name-finder sub-agents (phone: ${PHONE_FLAG:-off})."
echo "Dispatch each agent the absolute path: $ABS_RUN/enrich-batch-NNN.txt"
```

Now **issue the `Agent` tool calls foreground, up to 50 per assistant message** — one per `enrich-batch-NNN.txt`. If there are more than 50 enrich-batch files, send several sequential messages of ≤50 calls each. Same fan-out shape as Step 3:

- **subagent_type**: `name-finder`
- **model**: `haiku`
- **run_in_background**: **omit it** — dispatch foreground. Do NOT set `run_in_background: true`.
- **prompt** — pass only the FILE PATH, not the contents. The name-finder has a
  `Read` tool and reads its own batch file, so the per-lead page text never
  enters the orchestrator's context (this is the big token saving — at the 250
  cap the inlined-content approach pushed ~250k tokens through the orchestrator).
  Use exactly:
  ```
  Your input file: <ABSOLUTE path to runs/<slug>/enrich-batch-NNN.txt>
  Read it, then follow your name-finder instructions and write your JSON to the OutputFile named inside it.
  ```

Do NOT inline the batch contents and do NOT inline the name-finder instructions — both are read by the agent itself (`.claude/agents/name-finder.md` loads automatically with `subagent_type: name-finder`).

After all agents complete, merge:

```bash
python3 tools/scripts/enrich_contact_person.py --phase merge --run-dir "$RUN"

# Halts internally with exit 7 if 0 leads survived enrichment.
SURVIVED=$(wc -l < "$RUN/leads-with-contact.json")
[ "$SURVIVED" -ge 1 ] || { echo "ABORT: Stage 5.5 produced 0 leads with resolved contact + gender."; exit 7; }
```

### Step 7 — draft

> **If `COMBINED_CUSTOM=1` (set in Step 6.45), SKIP this entire step** — Step
> 6.5C already wrote `emails-drafted.json`. Go straight to Step 8. Step 7 below
> is only for template runs and custom runs that ALSO use WhatsApp.

**Channel gate (read first).** Determine which channels this run uses:

```bash
EMAIL_ENABLED=1
WA_ENABLED=0
if [ -f "$RUN/channels.json" ]; then
    grep -q '"email"' "$RUN/channels.json" || EMAIL_ENABLED=0
    grep -q '"whatsapp"' "$RUN/channels.json" && WA_ENABLED=1
fi
echo "Channels: email=$EMAIL_ENABLED whatsapp=$WA_ENABLED"
```

If `channels.json` is absent, email stays ON (every existing run is unaffected).
**When `EMAIL_ENABLED=0`, SKIP the rest of Step 7 (draft) and Step 8 (Brevo send)
entirely** — do not draft or send any email. Go straight from Stage 5.5 to Step 8.5.

Run the rest of Step 7 ONLY if `EMAIL_ENABLED=1`. Two draft modes. Detect which one
this run uses:

```bash
DRAFT_MODE="template"
if [ -f "$RUN/draft_mode.txt" ]; then
    DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')
fi
echo "Draft mode: $DRAFT_MODE"
```

#### Step 7a — TEMPLATE mode (default; GCC / LB / worldwide receptionist runs)

Signal-opener template (role-class ≥85 gate + salutation contract baked in):

```bash
python3 tools/scripts/draft_emails.py --run-dir "$RUN"

DRAFTED=$(wc -l < "$RUN/emails-drafted.json")
[ "$DRAFTED" -ge 1 ] || { echo "ABORT: 0 drafts after send-gate."; exit 5; }
```

#### Step 7b — CUSTOM mode (`draft_mode.txt` = `custom`; e.g. US gap-based runs)

**No template.** One `gap-writer` Haiku sub-agent writes a fully custom email per
firm, built on the specific manual inbox/scheduling gap it finds on that firm's
scraped pages. This mirrors the Step 6.5 name-finder fan-out exactly.

First, if this is a custom run, the run MUST declare its vertical (per the
US targeting overrides — vertical is per-run and never defaulted silently):

```bash
if [ "$DRAFT_MODE" = "custom" ]; then
    VERT=""
    [ -f "$RUN/vertical.txt" ] && VERT=$(head -1 "$RUN/vertical.txt" | tr -d ' \n')
    [ -n "$VERT" ] || { echo "ABORT: custom run has no vertical.txt. Set the target vertical before firing (do not default)."; exit 5; }
    echo "Custom-draft vertical: $VERT"
fi
```

Phase A — write per-lead gap-writer batch files:

```bash
python3 tools/scripts/draft_custom.py --phase prep --run-dir "$RUN"
GAP_BATCHES=$(ls "$RUN"/gap-batch-*.txt | wc -l | tr -d ' ')
echo "Will dispatch $GAP_BATCHES parallel gap-writer sub-agents."
```

Now **issue the `Agent` tool calls FOREGROUND, up to 50 per assistant message** —
one per `gap-batch-NNN.txt`. Same dispatch economics as Step 3 / Step 6.5:
never set `run_in_background`. Each call:

- **subagent_type**: `gap-writer`
- **model**: `haiku`
- **run_in_background**: omit it (foreground)
- **prompt**: the contents of `gap-batch-NNN.txt` verbatim.

Do NOT inline the gap-writer instructions — they live in
`.claude/agents/gap-writer.md` and load automatically with `subagent_type: gap-writer`.
The gap-writer reads `vault/lead-outreach/voice-us.md` itself.

Phase B — merge + enforce the email contract:

```bash
python3 tools/scripts/draft_custom.py --phase merge --run-dir "$RUN"

DRAFTED=$(wc -l < "$RUN/emails-drafted.json")
[ "$DRAFTED" -ge 1 ] || { echo "ABORT: 0 custom drafts after the contract gate."; exit 5; }
```

The merge step drops any draft that breaks the contract (missing
`Hello Mr./Mrs. <Surname>,`, any money word, missing `automatelb.com` link,
generic mailbox, em/en dash) — kill-on-fallback, never patched to ship volume.

Both modes produce `emails-drafted.json` with the same schema, so Step 8 (send)
is identical regardless of draft mode.

### Step 8 — Brevo batch send

Run Step 8 ONLY if `EMAIL_ENABLED=1`. If `EMAIL_ENABLED=0`, skip directly to Step 8.5.

```bash
CAP="${MAX_EMAILS_PER_RUN:-1000}"
python3 tools/scripts/send_batch_brevo.py --run-dir "$RUN" --send --cap "$CAP"

grep -q "result=failed" "$RUN/send-log.txt" 2>/dev/null \
    && { echo "ABORT: Brevo returned failure. Do NOT persist."; exit 6; }
```

### Step 8.5 — WhatsApp channel (only if `<RUN>/channels.json` contains "whatsapp")

This step runs ONLY when the run was bootstrapped with WhatsApp enabled. Detection:

```bash
WA_ENABLED=0
if [ -f "$RUN/channels.json" ] && grep -q '"whatsapp"' "$RUN/channels.json"; then
    WA_ENABLED=1
fi
```

If `WA_ENABLED=1`, choose the WhatsApp drafter by draft mode:

```bash
WA_DRAFT_MODE="template"
[ -f "$RUN/draft_mode.txt" ] && WA_DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')
echo "WhatsApp draft mode: $WA_DRAFT_MODE"
```

**If `WA_DRAFT_MODE` = `custom`** (e.g. the lb-enterprise run): use the `wa-writer`
fan-out (no template). One custom WhatsApp message per company, built on the specific
workflow gap it finds.

Phase A — prep per-company batches:

```bash
python3 tools/scripts/draft_whatsapp_custom.py --phase prep --run-dir "$RUN"
WA_BATCHES=$(ls "$RUN"/wa-batch-*.txt 2>/dev/null | wc -l | tr -d ' ')
[ "${WA_BATCHES:-0}" -ge 1 ] || { echo "WARN: 0 leads with a CEO mobile; skipping WhatsApp."; WA_ENABLED=0; }
echo "Will dispatch $WA_BATCHES parallel wa-writer sub-agents."
```

Then issue the `Agent` tool calls FOREGROUND, up to 50 per assistant message, one per
`wa-batch-NNN.txt`. Same dispatch economics as Step 3 / Step 6.5 — never set
`run_in_background`. Each call: **subagent_type** `wa-writer`, **model** `haiku`,
**prompt** = the contents of `wa-batch-NNN.txt` verbatim. Do NOT inline the wa-writer
instructions — they live in `.claude/agents/wa-writer.md` and load automatically.

Phase B — merge + enforce the contract:

```bash
python3 tools/scripts/draft_whatsapp_custom.py --phase merge --run-dir "$RUN"
WA_DRAFTED=$(wc -l < "$RUN/whatsapp-drafted.json" 2>/dev/null | tr -d ' ')
[ "${WA_DRAFTED:-0}" -ge 1 ] || { echo "ABORT: 0 custom WhatsApp drafts after the contract gate."; exit 8; }
```

The merge step drops any draft that breaks the contract (missing `Hello Mr./Mrs.
<Surname>,`, any money word, em/en dash, any `automatelb.com` or "Automate" mention,
unparseable mobile) — kill-on-fallback, never patched to ship volume.

**Otherwise (template mode)** — GCC / LB receptionist / worldwide — keep the existing
template drafter:

```bash
python3 tools/scripts/draft_whatsapp.py --run-dir "$RUN"

WA_DRAFTED=$(wc -l < "$RUN/whatsapp-drafted.json" 2>/dev/null | tr -d ' ')
[ "${WA_DRAFTED:-0}" -ge 1 ] || { echo "WARN: 0 WhatsApp drafts (no leads carried contact_phone); skipping WhatsApp send."; WA_ENABLED=0; }
```

If still enabled, send via the bridge's shared wwebjs auth:

```bash
WA_CAP="${WHATSAPP_MAX_PER_RUN:-200}"
node project/bridge/send_campaign.js --run-dir "$(pwd)/$RUN" --send --cap "$WA_CAP"

# send_campaign.js writes whatsapp-sent.jsonl + whatsapp-send-log.txt in $RUN.
grep -q '"result":"sent"' "$RUN/whatsapp-sent.jsonl" 2>/dev/null \
    || { echo "ABORT: WhatsApp batch produced no successful sends. Check whatsapp-send-log.txt."; exit 8; }
```

If `channels.json` is absent or does not mention `"whatsapp"`, this step is skipped entirely. Email is the always-on channel.

### Step 9 — persist

Persists BOTH channels: email sends from `emails-sent.jsonl` and WhatsApp sends
from `whatsapp-sent.jsonl` (each WhatsApp row records the business email/domain
plus a `wa:<phone>` token, so a WhatsApp-contacted lead is blocked on EITHER
channel in future runs). Runs after Step 8.5 so `whatsapp-sent.jsonl` exists.

```bash
python3 tools/scripts/persist_sent_log.py \
    --run-dir "$RUN" \
    --sent-log vault/lead-outreach/sent-log.md
```

### Step 9.5 — cleanup (ALWAYS, right after persist succeeds)

`raw_html/` is spent cache once the run has sent: everything useful is already
in the leads/emails JSON, and no later stage or future run ever reads it (a
past backlog reached 28 GB). The per-agent batch/out intermediates are equally
dead. Delete them now — keep the merged/final artifacts (`candidates-all.txt`,
`leads-*.json`, `emails-*.json*`, `send-log.txt`, `whatsapp-*`):

```bash
rm -rf "$RUN/raw_html"
rm -f "$RUN"/candidates-batch-* "$RUN"/queries-batch-* \
      "$RUN"/enrich-batch-* "$RUN"/enrich-out-* \
      "$RUN"/lead-batch-* "$RUN"/lead-out-* \
      "$RUN"/gap-batch-* "$RUN"/gap-out-* \
      "$RUN"/wa-batch-* "$RUN"/wa-out-*
echo "Cleanup: raw_html + per-agent intermediates removed."
```

Skip ONLY if the user explicitly asked to keep artifacts for debugging
(`KEEP_RUN_ARTIFACTS=1` is the equivalent flag on the `run_fire.py` path).

### Step 10 — final report

```
Campaign $SLUG fired (Haiku-DDG path, source-agent + name-finder dispatch).
  source sub-agents dispatched : $BATCHES
  raw candidates               : $TOTAL_RAW
  merged + deduped             : $MERGED
  domains with HTML            : $DOMAINS_WITH_HTML
  leads extracted              : $EXTRACTED
  name-finder agents dispatched: $ENRICH_BATCHES
  survived enrichment          : $SURVIVED  (Mr./Mrs.+Surname resolved)
  drafted (post-gate)          : $DRAFTED
  email sent                   : <from emails-sent.jsonl where result=sent>
  whatsapp drafted             : <wc -l whatsapp-drafted.json, only if channels.json had "whatsapp">
  whatsapp sent                : <from whatsapp-sent.jsonl where result=sent, only if WA enabled>
  fallbacks                    : none
Sent-log updated. Done.
```

## Hard rules (do not violate)

- **Never** invoke `run_campaign.py` — deprecated Maps path.
- **Never** prompt the user for approval. Auto-fire is pre-authorized.
- **Never** batch queries together. One query per agent.
- **Never** silently switch paths on failure. HALT, report root cause, exit non-zero.
- **Never** inline source-agent, name-finder, or gap-writer instructions into the dispatch prompt — they live in `.claude/agents/source-agent.md`, `.claude/agents/name-finder.md`, and `.claude/agents/gap-writer.md`.
- **Custom draft runs** (`draft_mode.txt` = `custom`): use Step 7b (gap-writer fan-out), never the template `draft_emails.py`. Require `vertical.txt`; if it is blank, ABORT and have the user specify the vertical (never default it silently).
- **Never** skip Stage 5.5 (contact-person enrichment). Every send must open with `Hello Mr./Mrs. <Surname>,` per the salutation contract in `project/CLAUDE.md`. Leads without a resolved Mr./Mrs.+Surname are dropped.
- **Never** auto-generate `icp.yaml` or `queries.txt`. If missing, abort and ask user.
- Dedup against `vault/lead-outreach/sent-log.md` is enforced for EMAIL inside merge_candidates.py + extract_leads.py, and for WhatsApp inside draft_whatsapp.py (pre-draft) + send_campaign.js (send-time net). Both channels persist via persist_sent_log.py. A lead contacted on either channel is never re-contacted on either.
- Honor `MAX_EMAILS_PER_RUN` env var as send cap (default 1000).
- **WhatsApp-only runs** (`channels.json` has `"whatsapp"` and not `"email"`): skip
  Steps 7 and 8; never send email. **Custom WhatsApp runs** (`draft_mode.txt`=`custom`)
  use `draft_whatsapp_custom.py` (wa-writer fan-out), never `draft_whatsapp.py`. Never
  inline wa-writer instructions — they live in `.claude/agents/wa-writer.md`.
