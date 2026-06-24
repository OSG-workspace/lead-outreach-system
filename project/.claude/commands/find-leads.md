---
description: Run the full lead outreach pipeline for a target. Pass target and gap focus as arguments, e.g. /find-leads "dentists in Beirut, pitch on bad websites"
---

# /find-leads

Run the full pipeline for a new campaign.

## How to use

```
/find-leads "<target description>, pitch on <gap>"
```

Examples:

```
/find-leads "independent dentists in Beirut, pitch on bad websites"
/find-leads "Shopify pet stores under 10k followers, pitch on weak email marketing"
/find-leads "marketing agencies in Lebanon with 5-20 employees, pitch on outdated portfolio sites"
```

## What this does

Invokes the `pipeline-orchestrator` skill with your prompt. The orchestrator handles the full flow: ICP definition, sourcing across Maps + web, scoring, gap analysis, copywriting, and sending via Brevo.

By default:

- Sources at most 60 raw leads
- Sends at most 30 emails (cap from `MAX_EMAILS_PER_RUN`)
- Operates in full-auto mode — no per-email approval

To override volume or cap, pass extra context:

```
/find-leads "find 200 dentists in Beirut, send to top 50, pitch on bad websites"
```

## Instructions to Claude

The user's argument is the brief. Pass the entire string verbatim to `pipeline-orchestrator` as the brief input. Do not summarize or rewrite the user's intent.

If the brief is missing a target or gap, ask ONE clarifying question, then proceed.

If the vault is not bootstrapped (no `lead-outreach/SYSTEM.md`), run the `obsidian-memory` bootstrap procedure first, then return here.
