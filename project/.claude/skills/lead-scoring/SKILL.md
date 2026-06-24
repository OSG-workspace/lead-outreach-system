---
name: lead-scoring
description: Scores each sourced lead against the active ICP rubric, producing a 0-100 score, qualification status (qualifies/disqualifies), and reasoning. Use after sourcing and dedup, called by pipeline-orchestrator. Also use standalone when the user asks "score these leads" or "why didn't this lead qualify".
---

# Lead Scoring

Apply the run's ICP rubric to every lead and produce a score with reasoning. The scoring is deterministic where possible: each criterion in the rubric either matches a lead's data or doesn't.

## Haiku-mode contract (mandatory)

This skill runs on Haiku 4.5. You do NOT score every lead — Python does that. Your role is narrow.

### Two-tier gate
1. **Tier 1 (Python, automatic):** `tools/scripts/score_leads.py` already mechanically scores every lead using the rubric in `icp.yaml`. Output: `runs/<slug>/leads-scored.json`. No Claude involvement.
2. **Tier 2 (Haiku, only on borderline):** For leads with `score` within ±5 of `threshold_qualify` (typically 65-74 when threshold=70), you adjudicate ONE binary decision: should this borderline lead be promoted to `send_ready` or dropped?

### Input you receive (per borderline lead)

A single JSON record with these fields ONLY:
```json
{
  "lead_id": "maps-al-madar",
  "name": "Al Madar Logistics",
  "score": 67,
  "threshold": 70,
  "score_breakdown": {"fit": 21, "size": 11, "reachability": 12, "gap_signal": 20, "buyer": 4},
  "signals": {"mode": "phone", "hiring_manual": true},
  "vertical": "logistics",
  "country": "AE",
  "email_class": "role"
}
```

### Required output (per borderline lead)

ONE JSON line. No prose:
```json
{"lead_id": "maps-al-madar", "decision": "promote", "reason": "strong_gap_signal+role_email_acceptable"}
```

`decision` MUST be one of: `promote` (treat as send_ready), `keep_borderline` (let signal-rescue path handle it), or `drop` (disqualify).

### Decision rules — apply IN ORDER, first match wins

1. If `score_breakdown.gap_signal >= 20` AND `email_class in {person, personal}` → `promote`
2. If `score_breakdown.gap_signal >= 20` AND `score >= 65` → `promote`
3. If `email_class == role` AND `score < 80` → `drop` (role inbox needs 85+ in this system)
4. If `signals` has any concrete key (`mode`, `hiring_manual`, `branches`, `ai_mentioned`, `saas`, `pdf_menu`) → `keep_borderline`
5. Otherwise → `drop`

These rules are exhaustive. Do NOT invent a sixth rule. Do NOT score on intuition. The rubric weights are the rubric weights.

### What Haiku must NOT do
- Do not re-score the lead. Trust the Python score.
- Do not fetch URLs or read external context.
- Do not write a markdown explanation. ONE JSON line per lead.
- Do not "give the lead the benefit of the doubt" outside the 5 rules above.

## Inputs

- `runs/<run-slug>/icp.yaml` — the rubric (from `icp-definition`)
- A **single batch file** like `runs/<run-slug>/batches/batch-001.jsonl` (default ~20 leads). The orchestrator pre-splits `leads-deduped.json` via `tools/scripts/batch_leads.py` so this skill never has to load the whole population at once. If invoked standalone on a small set (<30 leads), reading `leads-deduped.json` directly is fine.

## Working-set discipline

- Score one batch at a time.
- After scoring a batch, append the resulting JSONL lines to `runs/<run-slug>/leads-scored.json` and forget the batch contents before loading the next.
- Carry forward only `{id, score, qualifies, primary_gap}` summaries between batches — the full `raw` blocks belong on disk, not in context.
- This is what keeps a 200-lead run from blowing through tokens.

## Output

`runs/<run-slug>/leads-scored.json` (JSONL, one lead per line, appended across batches). Each lead gets:

```json
{
  "id": "...",
  "name": "...",
  "source": "...",
  "url": "...",
  "email": "...",
  "phone": "...",
  "location": "...",
  "raw": { ... },
  "score": 78,
  "score_breakdown": {
    "fit": 35,
    "reachability": 20,
    "gap_signal": 23
  },
  "matched_criteria": [
    "fit:+15: Listed as 'Dentist' on Google Maps",
    "fit:+10: Located in Beirut governorate",
    "fit:+10: Independent (single location)",
    "reachability:+15: Public email visible",
    "reachability:+10: Has a website",
    "gap_signal:+15: Website is single-page template",
    "gap_signal:+10: Last-updated > 3 years ago (footer says © 2019)",
    "bonus:+5: 4.7★ with 34 reviews"
  ],
  "qualifies": true,
  "primary_gap": "bad_website",
  "secondary_gap": "low_seo"
}
```

## Scoring procedure

For each lead:

1. **Run hard filters first.** If any hard filter from the rubric matches (e.g., "exclude chains"), set `score: 0`, `qualifies: false`, and add `disqualified_by: <filter>` to the breakdown. Skip the rest.

2. **Score the `fit` block.** Walk each `fit_criteria` item. If the lead's `raw` data matches the signal, add the points. Be generous on fuzzy matches — "Listed as 'Dentist'" should match "Cosmetic Dentist", "Pediatric Dentist", etc. Cap fit at the rubric's `weights.fit` value.

3. **Score the `reachability` block.** Same logic. A lead with `email != null` matches the email criterion. A lead with `url != null` matches the website criterion.

4. **Score the `gap_signal` block.** This is the most important section because it predicts whether the pitch will land. Be strict here — only award points if there's actual evidence of the gap, not absence of evidence.

5. **Apply bonuses and penalties.** These can push a lead above 100 or below 0; clamp to 0-100.

6. **Decide qualification.** `qualifies: score >= rubric.scoring.threshold_qualify`.

7. **Identify the primary gap.** Look at which `gap_signal_criteria` matched. The one with the highest points is `primary_gap`. If two are tied, pick whichever the user emphasized in the brief.

## Important: be explainable

Every score must come with `matched_criteria` listing exactly which rubric items contributed and how many points each. The user should be able to read the JSON and understand why a lead got 78 vs 62.

## Handling missing data

If a lead is missing the data needed to evaluate a criterion (e.g., website tech stack wasn't captured during sourcing), do NOT award the points. Flag it in `score_breakdown.unknown` so the user can see what data was missing:

```json
"score_breakdown": {
  "fit": 35,
  "reachability": 20,
  "gap_signal": 15,
  "unknown": ["could not determine if website was last updated >3 years ago"]
}
```

For high-potential leads (fit + reachability ≥ 50) that are missing only gap_signal data, the orchestrator can re-source/enrich them from `lead-sourcing-web` before final scoring. Tell the orchestrator about these in your output.

## Anti-patterns to avoid

- **Don't double-count.** If a lead has both "no website" AND "no SSL", award only "no website" — they're not independent.
- **Don't reward sophistication.** A lead with a good website and active marketing is harder to convince, not easier. The penalty in the rubric handles this; don't override.
- **Don't score on intuition.** Stick to the rubric. If the rubric is wrong, the user updates the rubric — not the scoring logic.

## Output for the orchestrator

After writing `leads-scored.json`, print a quick summary:

```
Scored 47 leads:
  Qualified (≥70):    18  (38%)
  Borderline (60-69):  9  (19%)
  Disqualified:       20  (43%)

Top 5:
  92  Drs. Karim Dental Clinic — primary gap: bad_website
  88  Smile Studio Beirut     — primary gap: bad_website
  85  Dental Care Hamra       — primary gap: low_seo
  82  Beirut Dental Group     — primary gap: weak_social
  80  Dr. Maya Said Clinic    — primary gap: bad_website

Avg fit: 32/40   Avg reachability: 18/25   Avg gap_signal: 19/35
```
