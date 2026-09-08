# LinkedIn channel

Low-volume / high-intent. Email stays the volume channel.

## The consent gate, and why this is a drip and not a fire

You cannot message a stranger on LinkedIn. Email has no such gate: draft, send,
done. Here the sequence is **invite → they accept → DM**, and the middle step is
someone else's decision, taken days or weeks later. So a `/fire` on this channel
does not send anything. It *queues* — and the sending plays out over weeks.

```
/fire (once)            sourcing → qualify → find the person → person gates
                        → li-writer composes ONE DM per person
                        → invites queued, each carrying its DM

daily (weeks)           sender.js      sends 8-18 invites, stores the DM
                        sweep_acceptance.js   who accepted?
                        messenger.js   DMs the ones who did
```

## Two ways to find the person, one destination

Sourcing is unchanged — companies come from the same deterministic OSM sweep as
every other campaign. Only the way we learn a person's *profile URL* varies:

1. **Company walk** (`walk_companies.js --companies`) — open the company's
   LinkedIn page, read its People tab. First-hand, no LLM, but it needs the
   company to have a LinkedIn presence.
2. **Profile find** (`resolve_li_profiles.py` + the `li-finder` agent) — for
   every company the walk produced nobody for, search the open web for the
   owner's profile. Plenty of real Gulf businesses have no LinkedIn company page
   while their owner personally does; without this they are sourced, qualified,
   and then silently unreachable.

Both feed the same `people-raw.json` and face the same gates — `qualify_people.py`
never learns which route a person came from, and must not. The finder is not a
softer path: it must have *seen* a real `linkedin.com/in/` profile with a source
tying that person to that company. Company pages, aggregator mirrors and
name-derived slug guesses are rejected at merge, because a wrong profile pitches
a stranger about someone else's business and burns them permanently.

**The invite carries no note.** A free account gets roughly five personalised
invitation notes per *month*; after that LinkedIn simply stops offering the "Add
a note" control. A pipeline that requires a note therefore works about five
times and then trips the missing-selector breaker daily. So invites go out bare
and every word of personalisation lives in the DM that follows acceptance, where
there is no monthly cap and 1900 characters to work with. `--with-notes` exists
for an account that has them (Premium / Sales Navigator).

**Throughput is the real constraint.** The ramp is 8→18 invites/day and 90/week,
so one fire's backlog of ~60 people takes roughly three to four weeks to drain,
and only the fraction who accept ever receive a message. Judge this channel on
replies per week, not per run.

## A third way in: the li-search handoff

`li-search/` (repo root) finds people from web indexes and never touches
linkedin.com. `./linkedin-run handoff <brief>` takes the accounts it has
delivered (`li-search/results/<brief>/owners.csv`), skips anyone already in
`state.json` or `backlog.json`, and runs the same batch → li-writer → merge →
`linkedin_queue.py` stages a fire runs, so those people land in the same
backlog and face the same limits. The writer's only first-hand hook is the
search snippet; a handoff never opens a profile. It queues, it does not send.

## After the first message: the journey

Acceptance is not the end of the sequence. Every lead — invited or directly
messageable — walks one schedule: outreach 2 days after acceptance, a video
follow-up 3 days later if they are silent, and 3 days after that the pipeline
stops messaging and emails David instead. Due dates are derived from stamps, so
a missed day produces an overdue step, never a lost one.

**See `JOURNEY.md`.** `daily.sh` runs it; `./linkedin-run journey plan` shows it.

## Daily commands

```bash
# once per campaign — queues weeks of invite supply into state/backlog.json
bash tools/scripts/fire_campaign.sh gcc-outreach-li

# then, every weekday — the ENTIRE send side (see cron.txt)
bash scripts/daily.sh --live       # omit --live for a dry run
```

`daily.sh` runs the five steps in order: top up today's invites from the
backlog → send them → sweep for acceptances → queue those people's DMs → send
them. It preflights once up front and exits 2 with an actionable message if the
browser is not ready, so a day where the laptop was shut simply skips rather
than half-sending. Every step is idempotent; running it twice is safe.

The sweep is not optional bookkeeping. It is the only thing that flips a lead to
`accepted`, and therefore the only thing that ever releases a DM. It also feeds
`trailingAcceptance`, so without it the acceptance-floor breaker eventually
reads 0% and halts new invites.

## What a fire does and does not do

`run_fire.py` runs `scripts/preflight.js` **first** on any LinkedIn run — before
sourcing — because sourcing permanently consumes OSM city-ledger ground and a
run that cannot send must not spend it. A fire then **queues and stops**:
`drafted=0` and "sent 0" are the correct, expected output on this channel.

Nothing in `project/tools/scripts/` is *specific* to this module; the shared
stages only learn that a run is LinkedIn-only (via `channels.json`) so they stop
applying email gates to it.

## Two processes, strictly separated

| | `queue/generate.js` | `send/sender.js` |
|---|---|---|
| role | reasoning | action |
| may be non-deterministic | yes | **no** — zero LLM calls |
| touches a browser | **never** | yes (attach only) |
| enforces the limits | no | **yes, all of them** |

The generator writes `state/queue.json` and stops. The sender re-derives every
limit from `config/limits.json` itself, so a bad model run is *physically*
incapable of exceeding the cap — the worst it can do is write a long queue that
the sender then truncates.

## Setup (once)

```bash
npm install                      # playwright-core only; ships no browsers
bash scripts/start_chrome.sh     # opens the LinkedIn-only Chrome on port 9222
                                 # first run: log in to your normal account
node scripts/whoami.js --save    # confirms WHICH account, and locks it in
```

`whoami.js --save` is not optional decoration. Until it has run there is no
`config/account.json`, so the senders can verify only that *some* session is
logged in — not that it is yours. Everything else in the channel is already
wired; this is the only step a human has to do.

**The Chrome profile is not the LinkedIn account.** `start_chrome.sh` opens a
separate Chrome profile (`~/chrome-linkedin`) — an empty cookie jar. You log
into your usual account in it, so LinkedIn sees the same member with the same
connections. What the separation buys is blast radius: the debug port has **no
authentication**, so anything local that reaches it can drive whatever the
attached browser can reach. Pointed at your everyday profile that is every
session you own; pointed here it is LinkedIn and nothing else. The script
refuses to start on the default profile even if you override the env var.

Chrome binds the port to `127.0.0.1` and validates the Host header, so it is not
reachable from the network or from a web page. Open the window to send, quit it
when you are done.

`whoami.js --save` records the account in `config/account.json`, after which
**both senders verify identity before every run** and exit 2 rather than send
from the wrong profile or a logged-out session. The check is selector-free — it
reads where `linkedin.com/in/me/` redirects to — so it cannot break on a
LinkedIn reskin.

## Chrome: attach, never launch

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-linkedin"
```

Log in to LinkedIn in that window once. The sender uses
`chromium.connectOverCDP('http://127.0.0.1:9222')` and `browser.contexts()[0]`
— the real, logged-in context. It never calls `browser.newContext()` (fresh,
logged-out session) and never `browser.close()` (that would kill your browser);
it closes the pages it opens. The dependency is `playwright-core`, which ships
no browsers, so there is nothing to `playwright install`. No anti-detect fork is
used, and `tests/tier2` T11 fails loudly if `navigator.webdriver` ever becomes
`true` — the signature of someone switching this to launch mode.

## Daily use

```bash
npm install
npm test                     # T1-T8, zero LinkedIn contact
npm run test:tier2           # T9-T12, read-only, needs Chrome on :9222
                             # T12 also needs LINKEDIN_TEST_PROFILE=<a profile url>

node queue/generate.js       # pulls the CRM via `claude -p`, writes state/queue.json
node queue/generate.js --leads-file leads.json   # offline: skip the CRM pull

node send/sender.js          # DRY RUN (the default): navigates, finds the button,
                             # logs what it would click, screenshots, exits
node send/sender.js --live   # actually sends
```

`--now` skips the schedule wait (dry runs only, in practice).

## Limits — what actually keeps the account safe

All in `config/limits.json`, all re-derived inside the senders, never in a
prompt. A bad queue can only ever be truncated, never obeyed.

| | ramp | daily ceiling | weekly ceiling |
|---|---|---|---|
| **invites** | 8 → 12 → 15 → 18/day | `absoluteDailyMax` 20 | `weeklyMax` 90 |
| **DMs** | 8 → 12 → 20/day | `followUpDmsPerDay` 20 | `dmWeeklyMax` 100 |

LinkedIn's free-account ceiling is roughly **100 invites/week**, and the weekly
number is the one that actually gets accounts restricted — 90 leaves headroom.
The ramps exist because a brand-new automated sender opening at its cap is the
loudest signal there is. `sender.js` refuses to start (exit 2) if any ramp step
exceeds its ceiling, and `t18-limits-sanity` fails the build if these drift.

Three gaps closed on 2026-08-03:

- **DMs had no ramp and no weekly ceiling** — a flat 25/day from a cold account,
  up to 125 messages in week one. Now ramped and capped like invites.
- **`maxOutstandingInvites` (120)** — nothing capped the unanswered-invite pile.
  A big pending stack is a mass-inviting signal, and it drags
  `trailingAcceptance` toward zero until the 25% floor halts sending for a
  bookkeeping reason rather than a real one. The sender exits 8 rather than add
  to it.
- **`withdrawPendingAfterDays` (21) is now real.** It used to appear only in the
  validator's required-keys list: nothing read it, nothing was ever withdrawn.
  `scripts/withdraw_pending.js` retires stale invites, as step 6 of `daily.sh`.

Pacing: gaps re-randomised per action, burst pauses every 3 actions, random
profile dwell before acting, spread across the whole 09:15–17:30 window,
weekdays only, one tab at a time, never parallel.

**Every per-person click is resolved BY NAME** (`lib/target.js`), never by
position. A profile page carries `Invite`/`Message` buttons for everyone in the
recommendations rail — measured: 7 on one page, none of them the page's owner —
so `.first()` would invite a stranger and mark your real lead as sent.

## Circuit breakers

Every one of these writes to `state/alerts.log` and halts:

| trip | effect | exit |
|---|---|---|
| 429 / 999 / `/checkpoint/` / challenge page | 48h `cooldownUntil`, hard stop | 10 |
| expected selector missing | stop, screenshot to `debug/`, **no guessed selector, no text-search fallback** | 11 |
| two consecutive failures | stop for the day | 12 |
| trailing-100 acceptance < 25% | stop new invites (DMs may continue) | 5 |
| weeklyMax (90) reached | stop until the rolling 7-day window clears | 4 |
| `dmWeeklyMax` (100) reached | stop DMs until the window clears | 4 |
| `maxOutstandingInvites` (120) pending | stop new invites; run the withdrawer | 8 |

Challenge detection matches the URL, the HTTP status, and whole phrases in the
page's **visible text**. It deliberately does *not* scan raw HTML for bare words:
`"captcha"` used to be on that list, LinkedIn embeds reCAPTCHA on ordinary
pages, and the first profile opened tripped a 48h cooldown on a healthy page.

## Idempotency

`state/state.json` — one JSON file, not SQLite. Justification: a single process
writes it, one action at a time, ~20 whole-object writes a day, with no
concurrency and no query load. Durability comes from an fsync'd
write-temp-then-rename, which is atomic on POSIX. It stays greppable and
hand-fixable at 2am with no binary to rebuild. Revisit if this ever grows to
multiple senders or six-figure rows.

- `"attempting"` is written **with a timestamp, before the click**.
- Confirmed success → `"sent"`.
- On restart, any `"attempting"` older than 5 minutes → `"unknown"`, moved to
  `state/review-required.json`, **never auto-retried** — the invite may have
  landed before the failure.
- Dedup is on the normalised LinkedIn profile URL, never on name.
- `../vault/lead-outreach/suppression.md` is checked before queueing and is
  **authoritative across both channels**: an email opt-out blocks LinkedIn.

## Never auto-retry

Any failed send goes to `state/review-required.json` for a human. There is no
retry path in the code.

## Cron

See `cron.txt` — written, not installed. Overlap is prevented by the senders'
own PID-checked lockfile (`lib/lock.js`), deliberately **not** by `flock(1)`:
macOS ships no flock, so the line that used to wrap the senders exited 127 and
— because the caller only checked for exit 1 — the entire send side ran zero
times while reporting nothing. The lock survives a crashed holder and an empty
or corrupt lockfile, and is released even on a hard `process.exit()`.

The generator line uses `claude -p` **without** `--bare` (which would skip MCP
discovery and drop the CRM connection).

## Heartbeat

`scripts/heartbeat.js` exits non-zero if a scheduled weekday produced zero
sends while no breaker fired — silent failure is the main risk on this channel.
