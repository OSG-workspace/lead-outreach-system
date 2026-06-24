---
name: pipeline-orchestrator
description: Runs the full lead-outreach pipeline end-to-end using the free scraping stack. Use whenever the user asks to "find leads", "run a campaign", "send outreach", or gives a brief like "find me dentists in Beirut and pitch them on bad websites". Chains setup-precheck, ICP definition, free-tool sourcing, scoring, gap analysis, copywriting, and Brevo send into one autonomous run.
---

# Pipeline Orchestrator (Free Stack)

You orchestrate the full pipeline. The route is **linear and simple** — there are no shortcuts, no fallbacks, no template substitutions.

## Haiku-mode contract (mandatory)

This pipeline is designed to run on Haiku 4.5 inside Claude Code. **No script in this system calls the Anthropic API directly.** All LLM work happens through Claude Code's own session — you, reading SKILL.md files and processing structured task queues.

### Role split (memorize this)

| Stage | Who does it | Where |
|---|---|---|
| Brief → ICP+queries | YOU (Haiku) via `icp-definition` skill | once per campaign |
| Source candidates from Maps | Python (`run_campaign.py`) | mechanical |
| Source from directories | Python (`source_directories.py`) | mechanical |
| Filter raw → canonical | Python (`filter_maps_leads.py`) | mechanical |
| Resolve emails via regex | Python (`enrich_emails.py`) | mechanical |
| Resolve emails via judgment | YOU (Haiku) via `claude-tasks/email-review/` queue | only if regex failed AND page shows owner |
| Score every lead | Python (`score_leads.py` / `funnel_lib.score_lead`) | mechanical |
| Adjudicate borderline scores | YOU (Haiku) via `lead-scoring` skill | only on ±5 of threshold |
| Extract page signals | Python (`extract_signals.py` + `classify_signal`) | mechanical |
| Pick primary gap + pitch angle | YOU (Haiku) via `business-gap-analysis` skill | per qualified lead, JSON only |
| Write paragraph 1 of email | YOU (Haiku) via `outreach-copywriting` skill | per qualified lead, JSON only |
| Compose subject + paragraphs 2-4 + footer | Python (`build_outreach_draft` / `compose_email.py`) | mechanical templates from voice.md |
| Send via Brevo | Python (`send_via_brevo.py` / `brevo-send` skill) | MCP call |

### What this means in practice

- **Never run `requests.post("https://api.anthropic.com/...")` from any script.** If you find code that does, delete it.
- **Never use WebFetch or WebSearch** during the per-lead loop. Python already crawled. You only read the local signals JSON.
- **Always output strict JSON** when a skill requires it. No prose, no markdown narratives. Haiku drifts when given free-form output slots.
- **Every per-lead Claude action is ONE turn.** No multi-step reasoning per lead. The pipeline budget assumes ~150 input + ~150 output tokens per lead.

## Processing the `claude-tasks/` queue

When `run_campaign.py` runs with `--claude-review-queue`, it writes review tasks to:

```
runs/<slug>/claude-tasks/email-review/<lead-id>.json
```

After the Python run completes, you (Haiku in Claude Code) process these files in a single pass:

```
1. List runs/<slug>/claude-tasks/email-review/ for *.json files
2. For each task file:
   a. Read it
   b. The file already contains: lead_id, lead_domain, page_excerpt, schema, rules
   c. Apply the rules in the file. Output a single JSON object to
      runs/<slug>/claude-tasks/email-review-results/<lead-id>.json
3. After all files processed, run:
     ./tools/run.sh tools/scripts/apply_claude_reviews.py \
       --run-slug <slug>
   (writes the recovered emails back into leads-email-resolved.json)
4. Re-run scoring on the recovered leads only
```

The task file format is fully self-describing — read the `schema` and `rules` fields and follow them literally. Do not add fields, do not omit fields, do not editorialize.

### Worked example — processing one email-review task

Input file `runs/2026-05-20-test/claude-tasks/email-review/maps-al-madar.json`:
```json
{
  "lead_id": "maps-al-madar",
  "lead_name": "Al Madar Logistics",
  "lead_domain": "almadar.ae",
  "website": "https://almadar.ae",
  "page_excerpt": "...About: Al Madar Logistics was founded by Rami Khoury, Managing Director, in 2008...",
  "task": "extract_owner_email",
  "schema": {...},
  "rules": [...]
}
```

Correct output to `runs/2026-05-20-test/claude-tasks/email-review-results/maps-al-madar.json`:
```json
{
  "lead_id": "maps-al-madar",
  "name": "Rami Khoury",
  "title": "managing director",
  "email": "rami.khoury@almadar.ae"
}
```

No prose. No "I think the email is...". Just the JSON.

## Fast path: `run_campaign.py`

For batch runs (e.g. "find me 100 qualified leads"), use the consolidated driver that runs every step below in one process with sourcing-until-target loop and parallelism on the slow stages:

```bash
./tools/run.sh tools/scripts/run_campaign.py \
  --run-slug <slug> \
  --target 100 \
  --max-passes 3 \
  --scraper-depth 2 --scraper-concurrency 8 \
  --enrich-concurrency 15
```

Prereqs in `runs/<slug>/`: `icp.yaml` (from `icp-definition`) and `queries.txt`. The driver writes per-pass artifacts, `qualified-final.json`, `emails-drafted.json`, `emails-eligible.json`, and `campaign-summary.txt`. Hand `emails-eligible.json` to `brevo-send` to ship.

Step-by-step invocation below is still the contract for when you need to inspect or rerun individual stages.

## The Canonical Route

```
1. SOURCE         → scrape candidate businesses from Maps + web
2. FIND BEST EMAIL → resolve the best reachable decision-maker email per candidate
3. QUALIFY        → apply target-profile + ICP criteria; pass/fail per lead
4. GAP ANALYSIS   → for each qualified lead, identify the specific business gap
                    from real public signals on their website
5. WRITE OUTREACH → write the unique email per lead referencing THAT lead's gap
                    and proposing our custom AI solution
6. SEND           → fire each email through Brevo with full per-lead audit trail
```

**A lead only progresses to step N+1 if step N succeeded for that lead specifically.** No lead is allowed to skip a step. No lead is allowed to receive a templated email because we couldn't extract a real gap.

## Hard Rules — NEVER Violate

0. **Token-cheap throughout.** The pipeline has mandatory pre-Claude filters that keep context bounded regardless of run size, plus mandatory hybrid reasoning rules that route mechanical work to Python and reserve Claude for genuine judgment.

   ### Mechanical filters (Python, deterministic)
   - `tools/scripts/filter_maps_leads.py` — slims raw Maps output (~1-3KB/lead) to canonical schema (~300 bytes/lead) BEFORE Claude sees it. Drops chains, low-rated, no-contact, out-of-geo leads via mechanical rules.
   - `tools/scripts/batch_leads.py` — chunks scoring input into 20-lead batches; carry only the qualified subset forward, drop raw lead data after each batch.
   - `tools/scripts/extract_signals.py` — returns ~1KB structured signals per lead's website rather than ~10KB full markdown. Used by `business-gap-analysis`.
   - `tools/scripts/resolve_email.py` — deterministic email-resolution waterfall (decision-maker → named non-DM → role-inbox-if-score≥85 → drop). Rule-based; never burn Claude tokens on this per-lead loop.
   - `tools/scripts/score_leads.py` — applies the rubric weights mechanically (geo + size + reachability are pure rules; vertical-fit is a small lookup). Score per lead deterministically; Claude only adjudicates leads that score borderline (within 5 of threshold).
   - `tools/scripts/compose_email.py` — builds paragraphs 2-4 (offer + differentiator + CTA), the compliance footer, and the subject template from the gap-signal type. Claude writes ONLY paragraph 1 per lead — every other line is templated from voice.md/offer.md/compliance.md and substituted in Python.

   ### Hybrid reasoning rules
   - **Rule-based first, Claude as fallback.** Each per-lead step (email resolution, scoring, gap classification, paragraph 1 writing) tries deterministic Python first. Claude is invoked only when the rule layer returns "uncertain" or for the irreducibly creative slot (paragraph 1).
   - **One Claude turn per lead at most.** No skill is allowed to invoke Claude more than once per lead per pipeline run. Per-lead total Claude budget = ~500 input + ~150 output tokens after caching.
   - **System prompt is cached.** Every Claude per-lead invocation in this pipeline uses Anthropic prompt caching (`cache_control: {"type": "ephemeral"}`) on the system prompt containing voice.md + offer.md + compliance.md + writing rules. Cache TTL is 5 minutes, so per-lead loops must complete within that window or be batched. Effective cost on cache hit: ~10% of fresh input cost.
   - **Static content goes in the cached system prompt.** Per-lead variation goes in the user message. If a string is identical across 90+% of per-lead calls, it must be in the system prompt block, not the user block.

   ### What's forbidden
   - Reading a raw Maps JSONL or full crawl4ai markdown into Claude context for any reason other than a single deep-dive on one specific lead.
   - Per-lead Claude calls that send voice.md/offer.md/compliance.md as fresh input every time (must be cached).
   - Claude reasoning over a 100-lead JSONL to do something deterministic (sorting, deduping, role-inbox detection, regex matching).
   - Multiple Claude turns per lead within a single pipeline run.

1. **No template paragraph 1.** Every email's opening paragraph must reference an evidence-backed observation about THAT specific lead's business, found in step 4. If gap analysis returns no concrete signal for a lead, the lead is dropped.
2. **No `info@` / role inboxes for low-scoring leads.** If a lead's score is below 85 and the only available email is a role inbox, the lead is dropped — not promoted by lowering the bar.
3. **No "qualified = sourced".** A lead becomes qualified only when steps 2, 3, and 4 all succeed. The qualified count drives the campaign size, not the sourced count.
4. **No background subprocesses.** Every step runs in this session, in the foreground, completes before the next step begins, and prints its result to the user.
5. **No fabricated gaps.** If `business-gap-analysis` returns a generic guess instead of a concrete observation, that lead is dropped. Templating a "manual operations" claim across leads is forbidden.
6. **No skipping `find best email`.** Every candidate gets the email-resolution step before qualification. The best decision-maker email available is what we send to — even if that means dropping leads where no decent email is found.
7. **Continue sourcing until N qualified.** The target is `qualified leads`, not `raw leads scraped`. If a sourcing pass produces fewer than N qualified after steps 2–4, loop back to step 1 with broader queries until the target is hit OR three full sourcing passes have been attempted (whichever comes first).

## Step 0 — Precheck the environment

Before running anything else, verify the toolchain is installed:

```bash
test -x ./tools/google-maps-scraper && echo "maps_ok" || echo "maps_missing"
test -d ./tools/venv && echo "venv_ok" || echo "venv_missing"
test -f .env && grep -q "^BREVO_MCP_TOKEN=" .env && echo "env_ok" || echo "env_missing"
test -f .env && grep -q "^BREVO_REPLY_TO=" .env || echo "warn: no Reply-To set"
```

If anything required is missing, invoke the `setup-tools` skill first.

## Step 1 — Bootstrap memory + brief

Read these from the Obsidian vault (use direct filesystem reads via the Read tool — MCPVault has known stale-read bugs):

- `lead-outreach/SYSTEM.md` — operating defaults
- `lead-outreach/target-profile.md` — standing ICP context (geography, verticals, gap signals)
- `lead-outreach/ICP-current.md` — active ICP for last/this run
- `lead-outreach/voice.md` — copywriting tone + structure rules
- `lead-outreach/offer.md` — what we deliver, framed for cold email
- `lead-outreach/compliance.md` — physical address + opt-out (legally required)
- `lead-outreach/sent-log.md` — dedup against past sends
- `lead-outreach/bounce-list.md` — permanent exclusion list
- `lead-outreach/deliverability.md` — warm-up state, sender domain, reply routing

If `voice.md`, `offer.md`, or `compliance.md` are stubs (`status: stub`), STOP. Don't proceed with template content.

Parse the user's brief into a per-run rubric overlay on top of `target-profile.md`:

- Target description (industry, geo)
- Optional gap focus
- Volume target N (default `MAX_EMAILS_PER_RUN`)
- Hard filters

Write `runs/$RUN_SLUG/brief.md` with the verbatim prompt and parsed interpretation.

Invoke `icp-definition` to produce `runs/$RUN_SLUG/icp.yaml` and refresh `vault/lead-outreach/ICP-current.md`.

## Step 2 — SOURCE candidates

Goal: produce a candidate pool that the next steps will winnow into N qualified leads. Source roughly **3× N** candidates initially, because the funnel will lose ~60–70% across email-resolution + qualification + gap-analysis.

Sourcing tools:

| Channel | Skill | Tool |
|---|---|---|
| google_maps | `lead-sourcing-maps` | `./tools/google-maps-scraper` |
| web | `lead-sourcing-web` | `ddgs` + `crawl4ai` |

Run sourcing **in the foreground** (one Bash invocation per channel), wait for completion, report counts before moving on.

### Token-cheap pre-Claude filter (mandatory)

Raw Maps output is ~1-3KB per lead and full of fields Claude doesn't need to see (CIDs, image URLs, full review JSON, pop-times). Run the mechanical filter BEFORE any Claude reasoning:

```bash
./tools/run.sh tools/scripts/filter_maps_leads.py \
  --input runs/$RUN_SLUG/maps-raw.json \
  --output runs/$RUN_SLUG/leads-raw.json \
  [--city "<geo>"] [--min-rating 3.0] [--require-website]
```

The filter drops: closed businesses, low-rated leads, known chain brands, leads with no contact channel, leads outside the target geography. It outputs ~300 bytes per lead in canonical schema. **Never read the raw Maps JSONL into Claude context — always read the filtered `leads-raw.json` instead.**

### Looping until target

If the candidate pool is < `3 × N` after the first pass, broaden queries (more cities, more verticals, more keywords) and run a second pass. After three passes total, proceed with whatever was sourced.

## Step 3 — FIND BEST EMAIL per candidate

This step is **fully mechanical — Python only, no Claude.** Run once per pool:

```bash
./tools/run.sh tools/scripts/resolve_email.py \
  --input runs/$RUN_SLUG/leads-raw.json \
  --output runs/$RUN_SLUG/leads-with-email.json \
  --rolemax-score 85
```

The script applies a deterministic waterfall per candidate:

1. **Already-published decision-maker email** — if Maps/web sourcing returned `firstname.lastname@company.com`, `firstname@company.com`, etc., use it directly.
2. **Extract from website** — fetch homepage + `/about` + `/team` + `/contact` and search for: a person's name with title (`Founder`, `CEO`, `Managing Director`, `Owner`) followed by an email pattern; if name is found but no email, try common patterns at their domain (`firstname@`, `firstname.lastname@`, `f.lastname@`).
3. **Best role inbox at the company's own domain** — accept `info@`, `contact@`, etc. **only if the lead's preliminary score will be ≥ 85**. Otherwise drop.
4. **No clean email** → drop the lead.

Categorical rejections (regex-coded in the script): image filenames, multi-email strings, placeholder domains, gov/edu/mil, boilerplate locals, local part < 3 chars.

Output: leads with `dm_email` + `dm_name` populated, leads with no resolvable email already dropped. **Claude never sees the dropped leads — they're filtered before scoring.**

## Step 4 — QUALIFY against criteria

Invoke `lead-scoring` against the rubric in `runs/$RUN_SLUG/icp.yaml`. The rubric is strict:

- Geography fit (must be in target geos)
- Size fit (must match target band)
- Industry fit (must match priority verticals)
- Reachability (must have `dm_email` from step 3)
- Buyer fit (must align with allowed roles in target-profile)

### Mechanical scoring first — Claude only on borderline

The scoring rubric (geo + size + vertical fit + reachability + gap_signal + buyer fit) is mostly deterministic. Run the rule-based scorer first:

```bash
./tools/run.sh tools/scripts/score_leads.py \
  --input runs/$RUN_SLUG/leads-with-email.json \
  --rubric runs/$RUN_SLUG/icp.yaml \
  --output runs/$RUN_SLUG/leads-scored.json
```

This emits a numeric `score` and `qualifies: true/false` per lead based on:
- Geography fit (lookup against `target-profile.md` → 0/15)
- Size fit (employee/branch band → 0/20)
- Vertical fit (priority list → 0/15)
- Reachability (has `dm_email` from step 3 → 0/15)
- Gap-signal pre-flag (raw signals from website pre-fetch → 0-25)
- Buyer fit (role classification → 0/10)

### Borderline-only Claude pass (token optimization)

For leads scoring within ±5 of `qualifying_threshold`, invoke `lead-scoring` for a Claude judgment pass. Use prompt caching: the rubric + target-profile go in the cached system prompt; only the borderline lead's data goes in the fresh user message.

```bash
./tools/run.sh tools/scripts/batch_leads.py \
  --input runs/$RUN_SLUG/leads-borderline.json \
  --out-dir runs/$RUN_SLUG/batches/ \
  --size 20
```

Process each batch through `lead-scoring`, drop the batch contents from working memory after each pass. Working set bounded at ~6KB regardless of total run size.

A lead is **qualified** only if the final score ≥ `qualifying_threshold` (default 70).

Carry only the qualified subset forward — keep their `lead_id`, `score`, `dm_email`, `dm_name`, `name`, `website`, `country`, `vertical` fields. Drop everything else (review counts, addresses, hours).

## Step 5 — GAP ANALYSIS per qualified lead

For each qualified lead, invoke `business-gap-analysis`. The skill must produce a concrete, evidence-backed observation from the lead's public footprint.

### Token-cheap signal extraction (mandatory)

Full crawl4ai markdown is 5-20KB per page. Multiplied by qualified leads × pages-per-lead, that's hundreds of KB of pure context bloat. Use the structured extractor instead:

```bash
# Build the URL list once
jq -r 'select(.qualifies==true) | {url: (.website // .url), id: .lead_id} | @json' \
  runs/$RUN_SLUG/leads-scored.json > runs/$RUN_SLUG/qualified-urls.jsonl

# Batch-extract signals (concurrent crawl4ai sessions, returns ~1KB per lead)
./tools/run.sh tools/scripts/extract_signals.py \
  --batch runs/$RUN_SLUG/qualified-urls.jsonl \
  --out-dir runs/$RUN_SLUG/signals/ \
  --concurrency 3
```

The script returns structured JSON per lead: `title`, `meta_description`, `h1/h2/h3` (capped 8 each), `emails`, `phones`, `social_links`, `copyright_year`, `tech_stack`, `tracking_pixels`, `has_booking_widget`, `has_contact_form`, `has_mobile_viewport`, `word_count`, `about_excerpt` (240 chars).

**Read the per-lead signals JSON into Claude context, never the full crawl4ai markdown.** This is a ~10× context-cost reduction. If a specific lead genuinely needs a deeper read, pass `--save-full-markdown` and pull that one file on demand.

### Acceptable signal types

Acceptable signals (non-exhaustive) for the gap analysis output:

- Booking-mode signal (WhatsApp-only, phone-only, contact-form-only)
- AI-mention signal (mentioned but no shipped tool)
- SaaS-stack signal (using Zoho/HubSpot/Salesforce without an AI layer)
- Branch-count signal
- Hiring signal (currently recruiting receptionists / coordinators / call-center / data-entry)
- PDF-menu / static-content signal
- Owner-name + visible operations gap

Each lead's gap analysis is written to `runs/$RUN_SLUG/gaps/<lead-id>.md` with `primary_gap`, `evidence`, `pitch_angle`, and `do_not_say` fields.

**If `business-gap-analysis` cannot find a concrete signal for a lead**, set `qualifies: false` in the gap file and DROP the lead. Do not fabricate a generic gap. Do not fall back to a vertical-template claim. The lead is no longer qualified — it has no defensible pitch.

## Step 6 — Loop check: do we have N qualified with gaps?

After step 5, count qualified leads with valid gap analyses. If `count < N`:

- If we've sourced fewer than 3 times, return to step 2 with broader queries (different cities, different verticals, more specific signal-rich keywords).
- If we've sourced 3 times and still don't have N, proceed with whatever count we have and tell the user we couldn't hit the target with the available channels.

If `count >= N`, take the top N by score and proceed.

## Step 7 — WRITE OUTREACH per lead

This step splits work between Python (deterministic skeleton) and Claude (the one creative slot per lead).

### 7a. Compose the email skeleton — Python

```bash
./tools/run.sh tools/scripts/compose_email.py \
  --leads runs/$RUN_SLUG/leads-scored.json \
  --gaps  runs/$RUN_SLUG/gaps/ \
  --voice  vault/lead-outreach/voice.md \
  --offer  vault/lead-outreach/offer.md \
  --compliance vault/lead-outreach/compliance.md \
  --out runs/$RUN_SLUG/emails-skeleton.json
```

The skeleton script emits per lead:
- `subject` — built from gap-signal type + business name (templated, deterministic)
- `body_text` with paragraph 1 marked as `<<<P1_PLACEHOLDER>>>`
- `body_html` matching skeleton
- Paragraph 2 (offer + AI consulting framing) — substituted from `offer.md` + lead's vertical
- Paragraph 3 (differentiator + risk reversal) — fixed string from `voice.md`
- Paragraph 4 (CTA) — fixed string from `voice.md`
- Sign-off + compliance footer — substituted from `compliance.md`

**No Claude tokens spent on paragraphs 2-4, the subject, sign-off, or footer.** All deterministic from vault content.

### 7b. Fill paragraph 1 per lead — Claude (the only creative slot)

Invoke `outreach-copywriting` with prompt caching:

- **Cached system prompt** (~3KB, cached at TTL 5 min): voice.md + offer.md + paragraph-1 writing rules + few-shot examples of "good paragraph 1" by signal type. Identical across all leads in the run.
- **Fresh user message per lead** (~500 tokens): just the lead's `name`, `vertical`, `country`, the gap analysis output (`primary_gap`, `evidence`, `pitch_angle`, `do_not_say`), and the instruction "write paragraph 1 only."
- **Output** (~80 words, ~150 tokens): the unique paragraph 1 referencing this lead's evidence.

Effective per-lead Claude cost on cache hit: ~150 fresh-input tokens + ~150 output tokens. For 100 leads, total Claude spend ≈ 30K tokens — about 90% cheaper than putting voice.md/offer.md in every fresh message.

The `compose_email.py` script then substitutes paragraph 1 back into the skeleton and writes final `emails-drafted.json`.

### Forbidden in any draft

- The literal phrase "manual phone- and walk-in booking workflows" (or any paragraph 1 text) repeated across multiple leads — `compose_email.py` rejects the run if it detects duplicate paragraph 1s
- Em-dashes (`—`) or en-dashes (`–`) anywhere — top AI-text tell, regex-rejected by `compose_email.py`
- Generic AI-personalization patterns (`As a [title] at [company]...`) — pattern-rejected
- Any claim not supported by THIS lead's gap analysis
- More than one CTA per email
- The word `Dear`

## Step 8 — Persist BEFORE send

For each draft, BEFORE the send call fires:

- Write the lead note at `vault/lead-outreach/leads/<lead-slug>.md` with `status: pending_send` and full frontmatter (lead_id, score, primary_gap, sender info, etc.)
- Append a placeholder line to `vault/lead-outreach/sent-log.md` with timestamp and `status: pending`

This ordering is non-negotiable: if send fails halfway, we still have a record of intent and won't double-send on retry.

## Step 9 — SEND via Brevo

Invoke `brevo-send` against `emails-drafted.json`. The send skill enforces:

- Sender domain auth checks (SPF, DKIM, DMARC) before the first send
- `Reply-To: BREVO_REPLY_TO` header on every send
- `List-Unsubscribe` headers per RFC 8058
- 90s default pacing (configurable per `deliverability.md` warm-up state)
- Domain cap: max 2 emails per recipient domain per run
- Dedup against `sent-log.md` (final safety check)
- Hard-stop on bounce-rate spike > 8% mid-run

After each successful send, update the lead note's `status: sent` with the Brevo `messageId`. Replace the placeholder line in `sent-log.md` with the real entry.

## Step 10 — Run summary

Print to user:

```
SOURCE         M raw leads from Maps + web (P sourcing passes)
FIND EMAIL     E candidates with valid dm_email
QUALIFY        Q leads passed scoring (≥ threshold)
GAP ANALYSIS   G leads with concrete gap (others dropped)
DRAFT          G unique emails written
SENT           S emails sent (S = G capped at MAX_EMAILS_PER_RUN)
```

Plus: top 3 highest-scoring leads with their gap + pitch angle. Path to the run folder. Append a paragraph to `vault/lead-outreach/run-log.md`.

## Failure modes

- **Step 0 fails** → run `setup-tools` skill
- **Step 2 returns 0 candidates after 3 passes** → tell user, suggest different brief
- **Step 3 returns 0 valid emails** → tell user, suggest paid email-finder integration (Hunter.io, Apollo)
- **Step 4 qualifies 0** → tell user the criteria are too narrow for the candidate pool
- **Step 5 finds gap for 0** → tell user the websites lack extractable signals; consider expanding signal patterns or using a different sourcing channel
- **Step 6 hits 3 sourcing passes without reaching N** → proceed with what we have; tell user the gap and propose either lowering N or adding paid enrichment
- **Step 9 hits Brevo 401/403** → STOP, tell user to verify sender at https://app.brevo.com/senders/list

## What this skill does NOT do

- It doesn't invent ICP rubrics — `icp-definition` does.
- It doesn't write copy — `outreach-copywriting` does.
- It doesn't run sourcing tools directly — `lead-sourcing-maps` and `lead-sourcing-web` do.
- It doesn't bypass any of steps 1–9, ever, even when the user asks it to ship faster. The route IS the speed — every shortcut taken in the past produced templated mass mail with low reply rates.

## Token budget summary

For a typical run of 100 qualified leads:

| Step | Mode | Per-lead cost | Run total |
|---|---|---|---|
| 1 — Bootstrap | Read vault files once | One-shot ~10K input | ~10K |
| 2 — Source | Python (filter_maps_leads.py) | 0 Claude tokens | 0 |
| 3 — Find email | Python (resolve_email.py) | 0 Claude tokens | 0 |
| 4a — Score (mechanical) | Python (score_leads.py) | 0 Claude tokens | 0 |
| 4b — Score (borderline) | Claude, batched 20 + cached | ~50 fresh / lead | ~1K (only ~20% of leads borderline) |
| 5 — Gap analysis | Python signal extract → rule-based gap → Claude only on edge cases | ~100 fresh / lead | ~10K (only ~30% need Claude) |
| 7a — Skeleton | Python (compose_email.py) | 0 Claude tokens | 0 |
| 7b — Paragraph 1 | Claude per-lead, system prompt cached | ~300 fresh / lead | ~30K |
| 9 — Send | Python (brevo-send) | 0 Claude tokens | 0 |
| **TOTAL** | | | **~50-60K Claude tokens for 100 leads** |

Without these optimizations the same run consumes ~500K-1M Claude tokens (10-20× more). Don't regress.

## What success looks like

A run that produces:

- N leads, each with a unique `dm_email` resolved
- N gap analyses, each pointing to a different concrete observation
- N emails, each opening with a sentence that could only have been written for THAT lead
- N entries in `sent-log.md` with real Brevo message IDs
- A reply rate > 5% (proper personalization benchmark)

If a run produces N templated emails sharing the same paragraph 1, the orchestrator failed. Don't ship that.
