# Lead Outreach System — repo root

This is the **repo root**, not the pipeline. The pipeline lives in `project/`.

## Read this first, every session

**`project/CLAUDE.md` is the authority** — it holds the natural-language fire
ROUTER (which phrase maps to which campaign fixture), the salutation contract,
the verify-against-the-last-run directive, and the approval modes. Read it before
acting on anything campaign-related. `project/ARCHITECTURE.md` is the file/agent
map; `project/.claude/commands/fire.md` is the executable playbook.

Nothing at this level overrides `project/CLAUDE.md`. This file exists only so a
session opened at the repo root knows where to look and can start a LinkedIn run
without hunting.

## Run a LinkedIn run

From this directory:

```bash
./linkedin-run fire            # queue weeks of invite supply (gcc-outreach-li)
./linkedin-run status          # backlog, limits, breakers, Chrome
./linkedin-run send            # the daily send side: invites + sweep + DMs
./linkedin-run plan            # trace every stage, consume nothing
```

`./linkedin-run` exists because a bare `fire_campaign.sh` from the wrong cwd, or
without the venv on PATH, fails in ways that look like pipeline bugs. It starts
the LinkedIn Chrome if needed, activates `project/tools/venv` (so `python3`
resolves to the interpreter that actually has `crawl4ai` + `ddgs`), then hands
off to the real orchestrator. See `project/linkedin/README.md` for the channel's
design.

**A LinkedIn fire QUEUES, it does not send.** `drafted=0` / `sent 0` is the
correct output. The channel drips 8→18 invites/day, 90/week, so one fire loads
roughly three to four weeks of supply. Invites carry no note (a free account gets
~5 notes/month); all personalisation lives in the DM sent after acceptance.

The send side also runs itself: `com.osg.linkedin-daily` (launchd) executes
`project/linkedin/scripts/autopilot.sh --live` at 09:10 on weekdays.

## Layout

```
.                              ← repo root (git repo, branch main)
├── linkedin-run               ← LinkedIn entry point (start here)
├── CLAUDE.md                  ← this file
├── .claude/agents/            ← the sub-agents /fire dispatches (name-finder,
│                                 li-finder, li-writer, lead-writer, gap-writer,
│                                 wa-writer). MUST stay at this level —
│                                 fire.md resolves them from the session root.
├── project/                   ← THE PIPELINE
│   ├── CLAUDE.md              ← the real operating doc (fire router)
│   ├── ARCHITECTURE.md        ← canonical file + agent map
│   ├── PIPELINE.md            ← per-stage rationale
│   ├── .claude/commands/fire.md
│   ├── templates/<base>/      ← one fire-ready fixture per campaign
│   │                            (gcc-outreach-li is the LinkedIn one)
│   ├── tools/scripts/         ← the deterministic stages (run_fire.py et al)
│   ├── tools/venv/            ← python deps (crawl4ai, ddgs) — needed by fires
│   ├── tools/google-maps-scraper
│   ├── linkedin/              ← the LinkedIn channel (Node + playwright-core)
│   │   ├── config/limits.json ← ramps + ceilings, re-derived by the senders
│   │   ├── state/             ← backlog, queue, alerts, idempotency
│   │   └── scripts/daily.sh   ← the whole send side, one command
│   ├── bridge/                ← WhatsApp channel (whatsapp-web.js)
│   ├── vault/lead-outreach/   ← local vault copy
│   └── runs/YYYY-MM-DD-<slug>/← per-run artifacts (gitignored, ~1.4 GB)
├── vapi/                      ← unrelated: Vapi voice agent
├── _archive-may-skeleton/     ← the 2026-05-06 Apify-based skeleton, superseded
├── _archive-post-move-dupes/  ← duplicates from the 2026-08-05 consolidation
└── _stray-outputs/            ← loose JSON that was sitting in the repo root
```

**A path without spaces matters.** If your checkout lives somewhere with a
space in the name, create a symlink that has none and address the system
through it — the launchd plist, `linkedin/cron.txt` and `tools/venv/bin/activate`
all need a space-free absolute path:

```bash
ln -s "/path/to/lead-outreach system" "$HOME/lead-outreach-system"
```

Deleting that symlink breaks the daily autopilot on such a checkout.
The launchd plist, `linkedin/cron.txt`, `tools/venv/bin/activate` and
`project/CLAUDE.md` all address the system through that path, and it has no space
in it. Deleting it breaks the daily autopilot.

## Vault

Canonical memory is the real Obsidian vault:
your Obsidian vault (`OBSIDIAN_VAULT_PATH`
in `.env`). `project/vault/lead-outreach/` is the copy the scripts read via the
relative `vault/...` path from the `project/` cwd. There is exactly one
`sent-log.md` that matters — never introduce a second.

## MCP + cost

`.mcp.json` declares **brevo** (free tier, 300/day) and **obsidian** (local)
only. Apify was removed on 2026-08-05: it is credit-metered and the user requires
a free-only stack. Sourcing is free by design — OSM/Overpass enumeration, the
`google-maps-scraper` Go binary, `ddgs`, and `crawl4ai`. Do not reintroduce a
paid sourcing API without asking.
