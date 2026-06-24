---
description: Score and gap-analyze a single lead by URL or handle, without running a full campaign. Useful for testing the system or evaluating a specific prospect.
---

# /analyze-lead

Score and gap-analyze a single lead. Doesn't send anything — just produces the analysis.

## How to use

```
/analyze-lead <url-or-handle>
```

Examples:

```
/analyze-lead https://drkarim.com
/analyze-lead "Drs. Karim Dental Clinic, Beirut"
```

## What this does

1. If no active ICP is loaded, asks for one before scoring (otherwise scoring has no rubric to apply)
2. Runs the appropriate sourcing skill on just this lead (Maps lookup or web scrape based on input format)
3. Runs `lead-scoring` against the active ICP
4. If qualifies, runs `business-gap-analysis`
5. Drafts an email via `outreach-copywriting` (does NOT send)
6. Returns the full analysis in chat

## Output

A summary in chat plus a written file at `runs/single-lead-<date>-<slug>/`. Nothing is written to the vault unless the user asks.

## Instructions to Claude

Treat this as a dry-run path. Even in full-auto mode, this command never sends emails. The output is for the user to inspect the system's reasoning before committing to a full campaign.

If the lead would qualify, end the response with: "This lead would be sent in a normal run. Want me to add it to the next campaign?"
