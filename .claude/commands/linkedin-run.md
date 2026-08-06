---
name: linkedin-run
description: Run the LinkedIn channel end to end. Fires the gcc-outreach-li campaign to queue weeks of invite supply, or with "send"/"status"/"plan" drives the daily send side, shows backlog+breakers, or traces stages without consuming anything.
---

# /linkedin-run — the LinkedIn channel, one command

The user typed `/linkedin-run [subcommand] [campaign]`. They pre-authorized by
invoking it. Do not ask for approval.

Subcommand (default `fire`):

| arg | do this |
|---|---|
| *(none)* / `fire` | `./linkedin-run fire [campaign]` |
| `plan` | `./linkedin-run plan [campaign]` — traces stages, consumes nothing |
| `send` | `./linkedin-run send` — the daily send side, LIVE |
| `dry` | `./linkedin-run dry` — send side, sends nothing |
| `status` | `./linkedin-run status` — backlog, limits, breakers, Chrome |

Campaign defaults to `gcc-outreach-li`, the only fixture with
`channels.json = ["linkedin"]`. Other bases are in `project/templates/`.

## Before firing

Read `project/CLAUDE.md` — it is the authority on the fire router and on when to
ask versus fire. Two rules from it that apply here:

- **Every fire is independent.** Prior runs of the same campaign — earlier today,
  finished, or still in progress — are never a reason to pause, inspect, or ask.
  The vault ledgers (`sent-log.md`, `sourced-log.txt`,
  `overpass-cities-fired.txt`, `queries-fired-log.txt`) scope each run to fresh
  ground mechanically. Never surface them as a warning or a question.
- **Kill on fallback.** If a stage degrades, HALT and report
  `ABORT: <stage> <root cause>`. Never silently switch paths.

## What a LinkedIn fire does — report it correctly

It **queues; it does not send.** `drafted=0` and `sent 0` are the CORRECT output
and must never be reported as a failure. `run_fire.py` runs
`linkedin/scripts/preflight.js` first (before sourcing, so a run that cannot send
does not spend OSM city-ledger ground); `./linkedin-run` opens the Chrome for you,
but if preflight still aborts, relay its exact `fix:` line.

A fire takes 5–12 minutes. **Do not go dark.** Launch with
`run_in_background: true` and poll the heartbeat every 60–90s:

```bash
cat project/runs/<slug>/status.txt
```

Relay a one-line update on each stage change. If `status.txt` shows `ABORTED: …`,
surface that line immediately.

## Final report

```
LinkedIn campaign <slug> queued.
  people queued            : N        (invites, each carrying its composed DM)
  directly messageable     : M        (already connected — DM goes today)
  backlog covers           : ~N/13 days at the 8→18/day ramp, 90/week ceiling
  invites sent by this fire: 0        (correct — the send side drips daily)
```

Then state that the send side runs itself via the `com.osg.linkedin-daily`
launchd agent at 09:10 on weekdays, and that `./linkedin-run send` runs it now.

## If the backlog is already empty and a fire is not wanted

`./linkedin-run status` tells you where the channel actually stands: an empty
backlog means no invite supply and the autopilot will log
`backlog empty — fire a campaign first` every morning while sending nothing.
