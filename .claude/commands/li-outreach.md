---
description: Hand a li-search brief to the LinkedIn outreach channel — one custom DM per person, queued into the invite drip
---

# OUTREACH THE LINKEDIN RUN

Trigger phrases (all equivalent): "OUTREACH THE LINKEDIN RUN for <brief>",
"/li-outreach <brief>", "message the linkedin leads for <brief>", "start
reaching out to the <brief> people on linkedin".

This is the bridge between the two LinkedIn systems:

- `li-search/` **researches** (never touches linkedin.com, never a session).
- `project/linkedin/` **sends** (drives the operator's own logged-in Chrome,
  invite → accept → DM, 8→18 invites/day, 90/week).

The handoff copies people from li-search's delivered pool into a normal run
folder and finishes it with the same stages a LinkedIn fire uses. li-search is
not modified and does not learn that outreach exists.

## Steps

1. **Make sure the brief has delivered accounts.** The pool is
   `li-search/results/<brief>/owners.csv`. If it is missing or short:
   ```bash
   cd li-search && ./li-search fire <slug> -q
   ./li-search export <brief> --audiences <slug1,slug2,…> --take 50
   ```
   (`/li-fire` covers this.) A handoff only ever takes people that are in
   `owners.csv` and NOT already in the channel's `state.json` or `backlog.json`,
   so re-running it picks up the NEXT people, never the same ones.

2. **Check the pitch.** `project/templates/li-handoff/<brief>.pitch.json`
   gives li-writer the angle, proof link and CTA. If it does not exist the
   handoff falls back to `gcc-outreach-li/pitch.json`, which sells outreach
   automation — usually wrong for a new brief. Write one first.

3. **Trace it, then run it.**
   ```bash
   ./linkedin-run handoff <brief> --plan          # every stage, consumes nothing
   ./linkedin-run handoff <brief> --take 60       # the real thing
   ```
   What runs, in order: `import_lisearch.py` → `draft_linkedin.py --phase batch`
   → **li-writer** (headless `claude -p`, one per person, parallel) →
   `--phase merge` → `linkedin_queue.py` (merges into
   `linkedin/state/backlog.json`) → `queue/generate.js` (today's cap).
   About 60 people is three to four weeks of invite supply; more only makes
   the writer draft messages that sit unsent for months.

4. **Report it the way a LinkedIn fire is reported.** A handoff **QUEUES, it
   does not send.** Relay: pool size, people taken, skip reasons (from
   `handoff.json`), messages written vs missing (from the merge line), and the
   backlog total. `sent 0` is the correct output. The daily autopilot
   (`com.osg.linkedin-daily`, 09:10 weekdays) sends the invites and, weeks
   later, each person's DM once they accept. `./linkedin-run status` shows the
   backlog, breakers and Chrome.

## What the writer has to work with

li-search never opens a profile, so there are no mutuals, no recent posts, no
shared school. The li-batch file carries the person's name, title, company,
the audience's country, and the **search-result snippet** (verbatim text from
their public page) as the only first-hand hook. li-writer is told to open on
that or on something company-specific, and to set `hook_used: "thin"` rather
than invent a fact. `generate.js` still rejects the batch if two messages come
out near-identical, so a templated batch is caught before an invite is spent.

## Do not

- Run `./linkedin-run fire` for this — that sources companies from OSM and
  would ignore the li-search pool entirely.
- Add a session cookie, a community LinkedIn MCP server, or any linkedin.com
  fetch to `li-search/`. `lisearch/compliance.py` refuses it, and it is the
  pattern that gets the operator's account restricted. Sending stays in
  `project/linkedin/`, which already does it with ramps and breakers.
