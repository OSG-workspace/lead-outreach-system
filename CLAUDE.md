# Lead Outreach System — repo root

This is the **repo root**, not the pipeline. The pipeline lives in `project/`.

## Read this first, every session

**`project/CLAUDE.md` is the authority** — the natural-language fire ROUTER
(phrase → campaign fixture), the salutation contract, the verify-against-the-
last-run directive and the approval modes. Read it before acting on anything
campaign-related. `project/ARCHITECTURE.md` is the file/agent/stage map.
`run_fire.py` is the only orchestrator; `project/.claude/commands/fire.md` is a
note for debugging one stage by hand. Nothing at this level overrides
`project/CLAUDE.md`.

## Run a LinkedIn run

From this directory:

```bash
./linkedin-run fire            # queue weeks of invite supply (gcc-outreach-li)
./linkedin-run status          # backlog, limits, breakers, Chrome
./linkedin-run send            # the daily send side: invites + sweep + DMs
./linkedin-run plan            # trace every stage, consume nothing
./linkedin-run handoff <brief> # li-search's delivered pool → custom DMs → backlog
```

`./linkedin-run` exists because a bare `fire_campaign.sh` from the wrong cwd, or
without the venv on PATH, fails in ways that look like pipeline bugs. It starts
the LinkedIn Chrome if needed, activates `project/tools/venv` (so `python3`
resolves to the interpreter that has `crawl4ai` + `ddgs`), then hands off to the
real orchestrator. See `project/linkedin/README.md`.

**A LinkedIn fire QUEUES, it does not send.** `drafted=0` / `sent 0` is the
correct output. The channel drips 8→18 invites/day, 90/week, so one fire loads
three to four weeks of supply. Invites carry no note; all personalisation lives
in the DM sent after acceptance. The send side also runs itself:
`com.osg.linkedin-daily` (launchd) executes
`project/linkedin/scripts/autopilot.sh --live` at 09:10 on weekdays.

## Layout

```
.                              ← repo root (git repo)
├── linkedin-run               ← LinkedIn entry point
├── CLAUDE.md                  ← this file
├── .claude/agents/            ← the 6 sub-agents run_fire.py dispatches headlessly
│                                 (name-finder, lead-writer, gap-writer, wa-writer,
│                                 li-finder, li-writer). MUST stay at this level.
├── .claude/commands/          ← li-fire, li-outreach, linkedin-run
├── .fire-work/<slug>/         ← per-agent batch/out files during a fire (gitignored,
│                                 removed at run end; kept OUT of project/ so no
│                                 CLAUDE.md is attached to sub-agents as memory)
├── li-search/                 ← separate tool: LinkedIn people search (own README)
├── project/                   ← THE PIPELINE
│   ├── CLAUDE.md              ← the real operating doc (fire router)
│   ├── ARCHITECTURE.md        ← file + agent + stage map
│   ├── .claude/commands/fire.md
│   ├── templates/<base>/      ← one fire-ready fixture per campaign
│   ├── tools/scripts/         ← the deterministic stages (run_fire.py et al)
│   ├── tools/venv/            ← python deps — every stage runs on its python3
│   ├── tools/google-maps-scraper  ← primary Stage-2 source binary
│   ├── linkedin/              ← LinkedIn channel (Node + playwright-core)
│   ├── bridge/                ← WhatsApp channel (whatsapp-web.js)
│   ├── vault/lead-outreach/   ← local vault copy (gitignored)
│   └── runs/YYYY-MM-DD-<slug>/← per-run artifacts (gitignored, ~135 MB)
├── tools/git-hooks/           ← pre-commit guard for secrets + PII
└── vapi/                      ← unrelated: Vapi voice agent
```

**A path without spaces matters.** This checkout lives in a directory with a
space in its name, so it is addressed through the symlink
`~/lead-outreach-system` — the launchd plist, `tools/venv/bin/activate` and the
scheduled jobs all use that path. Deleting the symlink breaks the daily
autopilot. Recreate it with:

```bash
ln -s "/path/to/lead-outreach-system 2" "$HOME/lead-outreach-system"
```

## Vault

Canonical memory is the real Obsidian vault (`OBSIDIAN_VAULT_PATH` in
`project/.env`). `project/vault/lead-outreach/` is the copy the scripts read via
the relative `vault/...` path from the `project/` cwd. There is exactly one
`sent-log.md` that matters — never introduce a second.

## Integrations + cost

There is no `.mcp.json`. Brevo (free tier, 300/day) is reached over REST from
`send_batch_brevo.py` / `sync_brevo_events.py` with `BREVO_MCP_TOKEN` from
`project/.env`; Obsidian is just the vault path. Apify was removed 2026-08-05:
the user requires a free-only stack. Sourcing is free by design — the
`google-maps-scraper` Go binary, Overture via DuckDB, OSM/Overpass, `ddgs`,
`crawl4ai`. Do not reintroduce a paid sourcing API without asking.
