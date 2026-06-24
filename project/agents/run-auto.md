# Run Auto-Pilot — the Haiku-DDG GCC consumer-chains pipeline

This is the auto-fire variant of `run-kickoff.md`. The user invokes it via the `/fire` slash command. The contract is:

- **No approval prompts.** The user pre-authorized.
- **One sub-agent per query** (maximum parallelism). For ~130 queries this dispatches ~130 Haiku agents in a single message.
- **Multi-page HTML fetch** (homepage + /contact + /about + /team) for ~2-3× the email-extraction yield of homepage-only.
- **Brevo batch send** via `messageVersions` — one HTTP call per 1000 emails.
- **Halt on ANY fallback.** No "ship a degraded campaign". See `feedback_kill_on_fallback` + `no-path-swap-on-fallback`.

The full playbook lives in `.claude/commands/fire.md`. Read that file first.

## What `/fire` targets by default

- **Geography:** UAE, Saudi Arabia, Qatar, Bahrain, Kuwait
- **Verticals:** consumer chains — clinic, vet, optical, fitness, salon, spa, restaurant, cafe, bakery, pharmacy, retail (per `agents/source-agent.md`)
- **Size:** 5–30 branches (sweet-spot bonus in scoring)
- **Voice:** SMB voice.md (4 paragraphs, ≤120 body words, no em-dashes, `Regards / David / Automate, automatelb.com`)
- **Sourcing:** DDG via parallel Haiku sub-agents, **NOT** Google Maps
- **Drafting:** template-based via `draft_emails.py` (instant, voice.md-compliant)
- **Send:** `send_batch_brevo.py` (Brevo `messageVersions` batch endpoint)

If the user's brief is clearly something else (Lebanon partners, B2B SaaS, single-country professional services), use `run-kickoff.md` instead. `/fire` will refuse.

## When to use which orchestrator

| User said | Playbook | Approval gate? | Sourcing | Batch size |
|---|---|---|---|---|
| "find me consultants in Beirut", "partner-level outreach" | `agents/run-kickoff.md` | YES | DDG via Haiku sub-agents | typically 10/batch |
| `/fire`, "fire a GCC run", "send my standard campaign" | `.claude/commands/fire.md` | NO | DDG via Haiku sub-agents | **1 query per batch** |

## Hard-halt conditions (no degradation accepted)

| Stage | Halt trigger |
|---|---|
| Sub-agent dispatch | `WebSearch` not in `.claude/settings.json` permissions allow list |
| Sourcing | < 50 merged candidates after dedup |
| HTML fetch | < 40% of candidates yielded ≥1 fetched page |
| Extract | 0 leads in `leads-extracted.json` |
| Draft | 0 drafts after role-class ≥85 gate |
| Brevo send | any HTTP 4xx/5xx OR `result=failed` |

For each, the orchestrator prints `ABORT: <reason>` and exits. Do not persist partial sends to the vault sent-log — partial rows would poison the dedup index.

## Non-negotiable safety nets

1. **Dedup against `vault/lead-outreach/sent-log.md`** — enforced inside `merge_candidates.py` (Stage 3) AND `extract_leads.py` (Stage 5)
2. **Score threshold** — only leads with `score >= 70` reach the drafter (effectively enforced because every extracted lead scores ≥ 78 baseline)
3. **Email class filter** — `email_class: personal` dropped at draft time
4. **Generic inbox policy (role-class)** — `info@`/`contact@`/`hello@` etc. only allowed if score ≥ 85. Baked into `draft_emails.py`. With current scoring (role base 82, +5 for 5-15 branches), only chain-sized businesses clear this gate.
5. **`modern_booking` signal → skip** (gap already solved)
6. **Brevo plan cap** — 401/402/429 → ABORT (don't blindly retry; partial sends poison dedup)
7. **Send cap** — `MAX_EMAILS_PER_RUN` env, default 1000

## The whole flow in one breath

```
/fire <slug>
  → resolve $RUN (default: latest GCC folder)
  → require icp.yaml + queries.txt (refuse to invent them)
  → split -l 1 queries.txt → N batches (1 query per batch)
  → dispatch N Haiku sub-agents IN ONE MESSAGE (max parallelism)
  → merge_candidates.py (dedup against sent-log)
  → fetch_html.sh (homepage + /contact + /about + /team, 24 parallel curls)
  → extract_leads.py (scans all pages per domain, scoring with size bonuses)
  → draft_emails.py (role-class ≥85 gate baked in, voice.md template)
  → send_batch_brevo.py --send (1 HTTP call / 1000 emails)
  → persist_sent_log.py
  → final report (sub-agents dispatched, raw, merged, fetched, extracted, drafted, sent)
```

End-to-end wall-clock: **~5-10 minutes** (sub-agents 1-3 min, fetch 1-2 min, extract <5 sec, send <1 sec).

## Failure modes — kill, don't degrade

- **Sub-agents got WebSearch denied** → ABORT. Tell user to add `WebSearch` to `project/.claude/settings.json` permissions allow list, restart session.
- **< 50 merged candidates** → ABORT. Queries too narrow or too duplicative. Tell user to broaden `queries.txt`.
- **HTML fetch <40%** → ABORT. Network anti-bot OR mass DNS failure. Tell user to investigate.
- **0 extracted leads** → ABORT. Either fetch returned empty pages or extract regex needs tuning.
- **0 drafts after gate** → ABORT (or report). All leads were role-class with score <85, or all personal. Tell user — do not lower the gate silently.
- **Brevo 401/402/429** → ABORT. 401 = bad key, 402 = plan quota, 429 = rate limit. None auto-recover safely.
