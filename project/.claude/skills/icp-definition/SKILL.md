---
name: icp-definition
description: Converts the user's per-run brief (e.g., "find dentists in Beirut, pitch on bad websites") into a structured ICP and scoring rubric YAML file. Use at the start of every campaign run, called by pipeline-orchestrator. Also use when the user says "update my ICP" or "let's redefine the target".
---

# ICP Definition

Your job is to turn a free-form user brief into a precise, machine-readable scoring rubric. The rubric is what `lead-scoring` uses to grade each lead 0-100.

## Haiku-mode contract (mandatory)

This skill runs on Haiku 4.5. You do TWO things:
1. Write `runs/<slug>/icp.yaml` matching the schema below exactly.
2. Write `runs/<slug>/queries.txt` — one search query per line, ready to feed gosom.

Do not add prose. Do not write a markdown report. Do not "ask for clarification" — if the brief is silent on a field, inherit from `vault/lead-outreach/target-profile.md` or use the default in the schema.

### Worked example A — short brief

**Brief:** `find me independent dentists in Beirut, pitch on outdated websites`

**`icp.yaml` output:**
```yaml
target:
  description: "Independent dental clinics in Beirut, Lebanon"
  industries:
    - dental
  countries:
    - LB
  size_signal: "1-3 practitioners (independent, not chain)"
sources:
  - google_maps
  - web
hard_filters:
  - exclude_chains: true
  - min_rating: 3.0
  - require_website: false
scoring:
  threshold_qualify: 70
  threshold_send: 70
  hard_floor: 60
  qualified_target: 30
  send_cap: 30
  weights:
    fit: 25
    size: 20
    reachability: 20
    gap_signal: 25
    buyer: 10
  primary_gap_focus: bad_website
```

**`queries.txt` output:**
```
dentist Beirut
dental clinic Beirut
dental practice Beirut Hamra
dental clinic Ashrafieh Beirut
independent dentist Beirut Lebanon
```

### Worked example B — broad B2B brief

**Brief:** `find me 100 mid-sized logistics companies in UAE and Saudi, pitch on manual operations`

**`icp.yaml` output:**
```yaml
target:
  description: "Mid-market logistics companies in UAE and Saudi Arabia"
  industries:
    - logistics
    - freight
    - transport
    - forwarding
  countries:
    - AE
    - SA
  size_signal: "10-200 employees, multi-branch operations"
sources:
  - google_maps
  - web
hard_filters:
  - exclude_chains: false
  - min_rating: 3.0
  - require_website: true
scoring:
  threshold_qualify: 70
  threshold_send: 70
  hard_floor: 60
  qualified_target: 100
  send_cap: 100
  weights:
    fit: 25
    size: 20
    reachability: 20
    gap_signal: 25
    buyer: 10
  primary_gap_focus: manual_ops
```

**`queries.txt` output:**
```
logistics company Dubai
freight forwarder Dubai
transport company Abu Dhabi
logistics company Riyadh
freight forwarder Jeddah
shipping company Sharjah
freight company Dammam
logistics services Saudi Arabia
freight forwarder UAE
transport company UAE
```

After writing both files, print ONE summary line: `ICP locked: <target> | <qualified_target> qualified target | threshold <threshold_qualify>`. Nothing else.

## Step 0 — Read the standing target profile FIRST

Before parsing the user's per-run brief, read `vault/lead-outreach/target-profile.md`. That file holds the user's standing ICP context — geography, company size, industry priorities, gap signals, scoring weights, qualifying threshold. It is the **default frame** every run inherits.

The per-run brief overlays on top of this file. Resolution rules:

1. If the brief contradicts the target profile → **brief wins** for this run only (don't edit the file).
2. If the brief is silent on a dimension → **inherit from target-profile**.
3. If both are silent on something critical (e.g., scoring weight for an unusual gap) → use sensible defaults and note the choice in `ICP-current.md`.

If `target-profile.md` doesn't exist, fall back to running purely from the brief and tell the user the standing profile is missing.

## Inputs you need

From the user's brief, extract or ask one focused question to learn:

1. **Target description** — what kind of business (overlay on target-profile.industries)
2. **Location / channel** — geo (overlay on target-profile.geography)
3. **Gap focus** — what weakness will be pitched (drives `gap_signal` weight)
4. **Hard filters** — disqualifiers (e.g., "skip chains", "skip <5 reviews")
5. **Reachability requirements** — does the lead need a public email? a website? a contact form?

If the user is vague on the gap focus, default to "mixed" and let `business-gap-analysis` pick per lead.

## The rubric format

Write to `runs/<run-slug>/icp.yaml` AND mirror to `vault/lead-outreach/ICP-current.md` (as readable markdown with a YAML code block).

```yaml
target:
  description: "Independent dental clinics in Beirut, Lebanon"
  industry: "healthcare / dentistry"
  geo: "Beirut, Lebanon"
  size_signal: "1-3 practitioners (independent, not chain)"

sources:
  - google_maps
  - web

hard_filters:
  - "exclude chain dentists (more than 3 locations)"
  - "exclude leads with no Google Maps listing"
  - "must have at least 1 review"

scoring:
  threshold_qualify: 70
  threshold_send: 70
  weights:
    fit: 40           # how well they match the target description
    reachability: 25  # email/contact form available, responsive online
    gap_signal: 35    # presence of the gap we pitch on

  fit_criteria:
    - { points: 15, signal: "Listed as 'Dentist' or 'Dental Clinic' on Google Maps" }
    - { points: 10, signal: "Located in Beirut governorate" }
    - { points: 10, signal: "Independent (not a chain)" }
    - { points: 5,  signal: "Active in last 12 months (recent reviews)" }

  reachability_criteria:
    - { points: 15, signal: "Public email visible on Maps or website" }
    - { points: 10, signal: "Has a website (any quality)" }

  gap_signal_criteria:
    # tuned to the chosen gap focus — example for "bad website":
    - { points: 20, signal: "No website at all" }
    - { points: 15, signal: "Website is a single page or template-only" }
    - { points: 10, signal: "Website is HTTP (no SSL) or last-updated >3 years ago" }
    - { points: 5,  signal: "Site present but no booking/contact mechanism" }

  bonus:
    - { points: 5, signal: "4.5★+ rating with 20+ reviews (proven business, just under-presented online)" }

  penalties:
    - { points: -10, signal: "Has a clearly modern, well-maintained website (no gap to pitch)" }
    - { points: -20, signal: "Already runs paid ads (sophisticated, harder to convince)" }
```

## How to tune `gap_signal_criteria` per run

The criteria above are an example for "bad website". For other gap focuses:

- **Weak social media** → reward: <500 followers, <1 post/month, no link in bio, no profile photo
- **Low Google reviews** → reward: <10 reviews, no responses to existing reviews, no recent review
- **Bad SEO** → reward: not in top 20 Google results for obvious local query, no schema, no GMB optimization
- **Mixed** → split the gap_signal weight evenly across all four sub-categories; let gap-analysis pick the worst one per lead

When the user says "mixed", use this gap_signal block:

```yaml
gap_signal_criteria:
  - { points: 8, signal: "Bad or no website" }
  - { points: 8, signal: "Weak social media presence" }
  - { points: 8, signal: "Low or unmanaged Google reviews" }
  - { points: 8, signal: "Poor SEO / not ranking" }
  - { points: 3, signal: "BONUS: at least one of the above is severe (worst-gap pitch)" }
```

## Output requirements

After writing both files (`icp.yaml` and the vault mirror), print to the user a concise 5-line summary:

```
ICP locked in:
  Target:    Independent dental clinics in Beirut
  Sources:   Google Maps + web
  Threshold: 70/100 to qualify
  Pitch:     Bad website
  Estimated qualifying rate: ~30% of sourced leads
```

## What this skill does NOT do

- It does not source leads. Hand off to the sourcing skills.
- It does not invent fields the user didn't imply. If the user didn't specify hard filters, leave them empty.
- It does not edit the user's `voice.md` — that's a separate vault file the user maintains manually.
