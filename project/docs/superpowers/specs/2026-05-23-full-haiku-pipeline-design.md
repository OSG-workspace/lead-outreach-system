# Full-Haiku Pipeline Design

**Date:** 2026-05-23  
**Status:** Approved  
**Goal:** Replace Opus with Haiku for the entire lead outreach chain at identical quality and lower cost, by splitting sourcing into parallel bounded extraction tasks and making all downstream stages deterministic Python scripts.

---

## Problem

In the 2026-05-23 run, Opus generated all 445 candidates by iterating through `queries.txt` one query at a time, running DDG searches, and writing `domain|name|country|vertical|branches` lines. This was the only model-dependent step — the entire rest of the pipeline (HTML fetch, email extraction, drafting, sending) was already deterministic Python. Haiku handled only the send step.

The fix: replace that one Opus sourcing session with ~14 parallel Haiku agents, each handling a bounded batch of 10 queries with a rigid extraction format.

---

## Architecture

Eight stages. Only Stages 1 and 2 use Haiku; the other six are pure Python/bash.

| Stage | Name | Model | Input | Output |
|---|---|---|---|---|
| 1 | KICKOFF | Haiku | PIPELINE.md, run-config.md, icp.yaml, queries.txt | dispatches Stage 2 agents |
| 2 | SOURCE | ~14× Haiku (parallel) | source-agent.md, icp.yaml, 10 queries each | candidates-batch-N.txt |
| 3 | MERGE | Python | candidates-batch-*.txt + sent-log.md | candidates-all.txt |
| 4 | FETCH | bash | candidates-all.txt | raw_html/<domain>.html |
| 5 | EXTRACT | Python | raw_html/, candidates-all.txt | leads-extracted.json |
| 6 | DRAFT | Python | leads-extracted.json | emails-drafted.json |
| 7 | SEND | Python | emails-drafted.json | emails-sent.jsonl, send-log.txt |
| 8 | PERSIST | Python | emails-sent.jsonl | vault/lead-outreach/sent-log.md |

---

## File Structure

```
project/
  PIPELINE.md                        ← master pipeline contract (Haiku reads first)
  CLAUDE.md                          ← updated to point to PIPELINE.md

  agents/
    run-kickoff.md                   ← orchestrator memory
    source-agent.md                  ← sourcing agent memory (DDG + extraction rules)

  tools/scripts/
    merge_candidates.py              ← Stage 3 (new)
    fetch_html.sh                    ← Stage 4 (new)
    extract_leads.py                 ← Stage 5 (refactored: --run-dir arg)
    draft_emails.py                  ← Stage 6 (refactored: --run-dir arg)
    send.py                          ← Stage 7 (refactored: --run-dir arg)
    persist_sent_log.py              ← Stage 8 (new)

  runs/
    YYYY-MM-DD-<slug>/
      run-config.md                  ← run-specific context (10 lines max)
      icp.yaml
      queries.txt
      candidates-batch-N.txt         ← one per sourcing agent
      candidates-all.txt
      raw_html/
      leads-extracted.json
      emails-drafted.json
      emails-sent.jsonl
      send-log.txt

  vault/lead-outreach/
    sent-log.md                      ← permanent dedup log
    SYSTEM.md
    voice.md
```

---

## Memory Files

### `PIPELINE.md`
The single source of truth Haiku reads at the start of every run. Contains all 8 stages, what file to check at each stage, what script to run, and what a successful output looks like. Haiku never reasons about what to do next — it follows the contract.

### `agents/source-agent.md`
The sourcing agent's complete instruction set. Self-contained: a fresh Haiku instance with no prior context reads this and executes. Contains:
- DDG search process (one search per query, scan top 8–10 results)
- Exact output format: `domain|name|country|vertical|branches` (raw lines, no markdown)
- Extraction rules (branch estimate logic, country detection, vertical mapping)
- Skip rules (social media URLs, directories, single locations, outside GCC)
- Target: 8–15 candidates per query

**Branch estimate rules:**
- Number in name or snippet ("8 Locations") → use it
- "branches"/"outlets"/"clinics" count visible → use it
- Chain-sounding name + GCC city → default to `5`
- Ambiguous or solo listing → skip

**Country detection:**
- TLD: `.ae→AE`, `.sa→SA`, `.qa→QA`, `.bh→BH`, `.kw→KW`
- City in address/snippet: Dubai/Abu Dhabi/Sharjah→AE, Riyadh/Jeddah→SA, Doha→QA, Manama→BH, Kuwait City→KW
- Cannot determine → skip

**Vertical mapping (must match exactly):**
`clinic | vet | optical | fitness | salon | spa | restaurant | cafe | cloud_kitchen | bakery | pharmacy | retail`

### `agents/run-kickoff.md`
Orchestrator playbook: read `run-config.md`, split `queries.txt` into batches of 10, dispatch parallel sourcing agents via `dispatching-parallel-agents` skill, then run merge.

### `runs/<slug>/run-config.md`
10 lines max. Slug, brief, send cap, model, ICP pointer. Haiku reads this to orient itself for the specific run.

---

## Sourcing Agent Detail

Each of the ~14 parallel Haiku instances receives:
- `agents/source-agent.md` (full instruction set)
- `icp.yaml` (hard filters)
- Its 10-query batch
- Output path: `candidates-batch-N.txt`

Per query workflow:
1. `WebSearch` the query
2. Scan top 8–10 results
3. For each matching company: write one pipe-delimited line
4. Skip: social URLs, directories/aggregators, single locations, outside GCC, duplicates within batch

Output example:
```
thewarehousegym.com|The Warehouse Gym|AE|fitness|18
noyaclinic.sa|Noya Clinic|SA|clinic|6
zawyacoffee.qa|Zawya Coffee|QA|cafe|12
```

---

## New Scripts

### `merge_candidates.py`
```
Args:   --run-dir, --sent-log
Input:  candidates-batch-*.txt
Logic:  deduplicate by domain, strip already-sent domains (from sent-log.md),
        apply ICP hard filters (country in GCC set, branches field present)
Output: candidates-all.txt
```

### `fetch_html.sh`
```
Args:   <run-dir>
Input:  candidates-all.txt (col 1 = domain)
Logic:  parallel curl, timeout=10s, 4 concurrent, spoofed User-Agent
Output: raw_html/<domain>.html
```

### `persist_sent_log.py`
```
Args:   --run-dir, --sent-log
Input:  emails-sent.jsonl
Logic:  append one markdown table row per sent email,
        idempotent (skips message_ids already present in log)
Output: vault/lead-outreach/sent-log.md
```

### Refactored scripts (logic unchanged)
`extract_leads.py`, `draft_emails.py`, `send.py` — replace hardcoded `ROOT = Path("...")` with `argparse --run-dir`. Move from run folder into `tools/scripts/`.

---

## Per-Run Startup Flow

```
User: "run a campaign — GCC consumer chains, 70 sends"

Haiku:
1.  Create runs/<slug>/run-config.md
2.  Copy icp.yaml + queries.txt from last run (or generate new if brief differs)
3.  Read agents/run-kickoff.md
4.  Split queries.txt into 13 batches of 10
5.  Invoke dispatching-parallel-agents → ~14 Haiku sourcing agents run simultaneously
6.  python tools/scripts/merge_candidates.py --run-dir <run> --sent-log vault/...
7.  bash tools/scripts/fetch_html.sh <run-dir>
8.  python tools/scripts/extract_leads.py --run-dir <run>
9.  python tools/scripts/draft_emails.py --run-dir <run>
10. Show user: "N emails ready. Preview first 3?"
11. User approves → python tools/scripts/send.py --run-dir <run> --send --cap 70
12. python tools/scripts/persist_sent_log.py --run-dir <run> --sent-log vault/...
```

---

## Timing

| Stage | Duration |
|---|---|
| Stage 2 — parallel sourcing (~14 agents × 10 queries) | ~3–4 min |
| Stage 4 — HTML fetch (168 domains, 4 concurrent) | ~2–3 min |
| Stages 5–6 — extract + draft | <1 min |
| Stage 7 — send (Brevo pacing, 70 emails × 30s) | ~37 min |
| **Total** | **~43 min, 100% Haiku** |

---

## What Does NOT Change

- `icp.yaml` format and scoring logic
- `extract_leads.py` logic (email classification, signal detection, scoring)
- `draft_emails.py` logic (SIGNAL_OPENERS, name_short, template)
- `send.py` logic (Brevo API, pacing, dedup, logging)
- `vault/lead-outreach/sent-log.md` format
- Brevo sender config in `.env`
