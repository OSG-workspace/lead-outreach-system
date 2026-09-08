---
description: Fire a stored li-search audience and output leads with their LinkedIn account names
---

# FIRE THE LINKEDIN RUN

Trigger phrases (all equivalent): "FIRE THE LINKEDIN RUN for <audience>",
"/li-fire <audience>", "fire the linkedin search for <audience>".

**This is `li-search/`, NOT the `project/` fire chain.** Do not run
`./linkedin-run`, `fire_campaign.sh`, or anything under `project/` for this.
They are unrelated systems: `project/linkedin/` sends invites from the operator's
own logged-in account; `li-search/` only reads licensed people-data APIs.

## Steps

1. **Resolve the audience.** `cd li-search && ./li-search list`.
   - Named audience matches a stored slug → use it.
   - No match, but the user described a target audience → `./li-search define`
     it first (see below), confirm the stored definition back to them, then fire.
   - Ambiguous between two stored audiences → ask which, do not guess. Firing
     the wrong audience wastes provider credits.

2. **Fire it — quiet mode.**
   ```bash
   cd li-search && ./li-search fire <slug> -q
   ```
   `-q` prints exactly two lines: the `DONE:` line and the run dir. That is
   the whole of what enters your context. The tool itself spends zero LLM
   tokens (no agents, pure Python); the only token cost of a LinkedIn run is
   what YOU read back, so read back little.
   With no API keys the default already runs the two free providers (`ddgs`,
   `openweb`) — no flag needed. Add `--dry-run` first if they asked what it
   would do. A fire is cumulative: prior runs of the audience are folded in,
   so re-firing only ever grows the pool.

3. **Report — same token discipline as `/fire`.** Relay the `DONE:` line
   (kept / qualified / new / per-provider counts), then a preview of at most
   10 rows taken from `summary.json` (`top`: account, name, title, company,
   match), then the run dir and the file names. Every previewed row carries
   the **LinkedIn account name** — that is the requested output; the full list
   is `accounts-qualified.txt` (meets the full spec) and `accounts.txt` (all).
   State plainly if a provider was `skip` or `STOPPED` (throttled), and the
   `eu_records` count from `summary.json`. A STOPPED free engine is not an
   empty market — say so.

   **Never read into your context:** `leads.json` (~1 KB per lead, 700 KB on
   a 700-lead run), `leads.csv`, or a full `accounts*.txt`. If the user wants
   more names than the preview, give them the path; if they insist on seeing
   them inline, `head -n 50 accounts-qualified.txt` is the ceiling per message.
   `summary.json` and `status.txt` exist precisely so you never need the big
   files. If a fire is still running in another session, `cat status.txt` is
   the one-line heartbeat.

4. **Deliver into the results folder, never only into chat.** Every account
   handed to the user for a brief goes through
   ```bash
   ./li-search export <brief> --audiences <slug1,slug2,…> --take 50 --table
   ```
   which appends the next batch to `li-search/results/<brief>/owners.csv`
   (cumulative, numbered, never repeated), writes `batch-NN.csv`, and prints
   the batch as a table. The user asked (2026-09-02) that everything be stored
   in one findable place — that folder is it. For Shughol Lebanon the brief
   name is `shughol-lebanon` and the audiences are the four `lb-*` slugs.

   Add `--verify` when the batch is going to be MESSAGED rather than just
   listed. It checks each candidate against the brief before delivering it
   (perplexity/sonar, ~$0.0056/row, hard-capped by `--verify-max`), refuses only
   a confident off-spec answer, and records refusals in `rejected.csv`. It is
   PAID and off by default, so ask the user before spending — a few cents buys
   precision on the ~50 rows the 90-invites-a-week channel can actually send,
   which is the scarce good. See li-search/README.md "the delivery gate".

5. **Outreach is a separate command.** When the user wants these people
   *messaged*, that is `/li-outreach <brief>` (`./linkedin-run handoff <brief>`),
   which takes the delivered pool into `project/linkedin/`'s invite → accept →
   DM drip with one custom DM per person. Never bolt sending onto li-search.

## Defining a new audience

```bash
./li-search define "<name>" \
  --industry "<what the business is>" \
  --seniority owner \
  --countries AE,SA \
  --cities "Dubai,Riyadh" \
  --keywords "<comma list>" \
  --exclude-titles "Agent,Consultant"
```

Cities are far more selective than countries in a neural index — always ask for
or infer them. If the user's brief NAMES firms or founders, pass them as
`--companies` and `--people "Name @ Firm,…"` — those become the first, most
precise queries. A named person qualifies when the row corroborates the seed
(the firm, a title, or the industry); a bare namesake shows as `person?=`. Leave generic-word firm names
(Maze, Ethos, Spirit) out of `--companies`; use the founder's name instead.
Use `--notes` to carry the tier and the pitch the brief assigns to that segment. `--seniority owner` expands to the owner-equivalent title set,
including the General Manager / Managing Director forms Gulf and Levant SMEs use
where the West says CEO.

## Never

- Never scrape linkedin.com or use a logged-in session to satisfy this command.
  `lisearch/compliance.py` refuses it, and working around that guard is out of
  scope for any request.
- Never add a provider key on the user's behalf. Spend is theirs to opt into.
