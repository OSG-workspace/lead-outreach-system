---
name: fire
description: Debugging note for ONE stage of an EXISTING run. Not an orchestrator — a fire phrase or `fire_campaign.sh <base>` launches run_fire.py, which runs every stage headlessly. Use /fire <slug> only to re-run a single stage by hand.
---

# /fire <slug> — re-run one stage of an existing run

**The orchestrator is `tools/scripts/run_fire.py`**, launched by
`bash tools/scripts/fire_campaign.sh <base> [--dry-run|--plan] [--target-leads N]`
from `project/`. It clones the fixture, runs every stage, dispatches sub-agents
headlessly and cleans up. Routing and contracts: `CLAUDE.md`; map: `ARCHITECTURE.md`.

**The in-session Agent-tool fan-out was retired on 2026-09-02.** It loaded
`name-finder.md` whole plus nested `CLAUDE.md` into each child and shared one
~200-search budget across the session (a 250-lead fan-out exhausted it). Use
`run_fire.py`; never dispatch `name-finder`/`lead-writer`/`gap-writer`/`wa-writer`
from a session.

## Trace before touching anything

```bash
cd project && bash tools/scripts/fire_campaign.sh <base> --plan   # prints every stage + real args, executes nothing
cat runs/<slug>/status.txt                                        # where an existing run stopped
```

## Re-run one stage by hand (cwd = `project/`, `PY=tools/venv/bin/python3`)

Set `KEEP_RUN_ARTIFACTS=1` on the fire first — a normal exit deletes `raw_html/`
and `.fire-work/<slug>/`, and the enrich/draft preps refuse to run without them.

| Stage | Command |
|---|---|
| 2 source | `$PY tools/scripts/source_gmaps.py --run-dir runs/<slug> --source '<json>' --batch-prefix 01` (same shape: `source_overture.py`, `source_overpass.py`, `source_places.py`, `source_directory.py`) |
| 2.5 resolve | `$PY tools/scripts/resolve_domains.py --run-dir runs/<slug> --config '<sourcing json>'` |
| 3 merge | `$PY tools/scripts/merge_candidates.py --run-dir runs/<slug> --sent-log vault/lead-outreach/sent-log.md` |
| 4 fetch | `bash tools/scripts/fetch_html.sh runs/<slug>` |
| 5 extract | `$PY tools/scripts/extract_leads.py --run-dir runs/<slug> --sent-log vault/lead-outreach/sent-log.md` |
| 5.3 qualify | `$PY tools/scripts/qualify_leads.py --run-dir runs/<slug> [--cap N]` |
| 5.5 enrich | `$PY tools/scripts/enrich_contact_person.py --phase prep --run-dir runs/<slug> --work-dir ../.fire-work/<slug> [--enrich-phone]` → fan-out via `run_fire.py` only → `--phase merge --run-dir runs/<slug> --work-dir ../.fire-work/<slug>` (`--work-dir` is where the per-agent batch/out files live; omit it and they land in the run dir, which pulls `project/CLAUDE.md` into every agent) |
| 6 draft | `$PY tools/scripts/draft_emails.py --run-dir runs/<slug>` · custom: `draft_custom.py` / `draft_lead_custom.py --phase prep|merge --run-dir runs/<slug> --work-dir ../.fire-work/<slug>` |
| 7 send | `$PY tools/scripts/sync_brevo_events.py && $PY tools/scripts/send_batch_brevo.py --run-dir runs/<slug> --send --cap N` |
| 8 persist | `$PY tools/scripts/persist_sent_log.py --run-dir runs/<slug> --sent-log vault/lead-outreach/sent-log.md` |
| 8.5 WhatsApp | `draft_whatsapp_custom.py --phase prep|merge --run-dir … --work-dir …` / `draft_whatsapp.py --run-dir …` → `node bridge/send_campaign.js --run-dir <abs> --send --cap N` |

## Halt table (what `run_fire.py` does, so you can recognise it)

| Stage | Halt condition |
|---|---|
| Pre-flight | headless `claude -p` probe fails; a foreign `CLAUDE.md` in a parent dir (`ALLOW_FOREIGN_CLAUDE_MD=1` overrides); LinkedIn `preflight.js` aborts |
| Sourcing — no fresh ground | a source whose ledgered ground is swept, or a `places` source with no key, exits **8** — expected, not a halt; status carries `[no-fresh-ground: <vertical>]` |
| Sourcing — 0 candidates | halts at the Stage 3 zero gate naming the supply fix (`places.txt` rows, wider selector, another source). No backlog rescues this by design |
| Fetch | never halts; WARN below 40% domain yield |
| Extract / Qualify | 0 leads (`leads-extracted.json` / `leads-qualified.json` empty, exit 7) |
| Enrich | 0 leads with name + direct email; agent completion < 60% (`ENRICH_MIN_COMPLETION`) |
| Draft | 0 drafts after the contract gate (exit 5) |
| Brevo send | any 4xx/5xx → persist what sent, then exit 6 |
| WhatsApp | 0 drafts after the gate, or `send_campaign.js` fails / 0 `result=sent` |
