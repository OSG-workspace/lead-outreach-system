# The lead journey

Every lead follows one schedule, whichever door they came in through. The
schedule is data (`config/journey.json`), the interpreter is pure
(`lib/journey.js`), and the only thing that can put a message on LinkedIn is
still `send/messenger.js` under `config/limits.json`.

```
        invited                                   direct
  invite → they accept                    already 1st-degree / Open Profile
        │                                          │
        │ acceptedAt                               │ enteredAt
        └──────────────┬───────────────────────────┘
                       ▼
                 wait 2 days            (direct: waitDays 0)
                       ▼
                  OUTREACH  ── the DM li-writer composed for this person
                       ▼
                 wait 3 days            ── cancelled the moment they reply
                       ▼
                   VIDEO    ── one fixed operator message + the video link
                       ▼
                 wait 3 days            ── cancelled the moment they reply
                       ▼
                   NOTIFY   ── Brevo email to david@osgdev.com. No more messages.
```

A reply at any point ends the journey. So does suppression, and so does
LinkedIn telling us the person is not messageable.

## The one idea worth knowing

**Due dates are derived, never stored.** Each completed step stamps a timestamp
in `lead.journey.history`, and a step is due when
`now >= history[after.from] + after.days`. Three consequences, all of them load-
bearing:

- Editing `journey.json` reschedules everyone already mid-journey.
- A tick that never ran (laptop shut, a week away) leaves an **overdue** step,
  not a lost one — nothing silently skips.
- Running the tick twice in a day is a no-op the second time.

That is what makes it safe to hang off a schedule that may fire late or twice.

## Does the Mac have to be on?

**Yes.** Sending requires the logged-in LinkedIn Chrome on this machine —
there is no LinkedIn API that sends DMs, and a cloud agent cannot reach a
browser sitting on your desk. `launchd` fires `autopilot.sh` at 09:10 on
weekdays and simply does not run while the laptop is asleep or shut.

What that costs you is *timing*, not leads: a day the Mac was off produces
overdue steps that go out on the next run, in due-date order. A week away means
follow-ups land a week late, never that they are skipped. If a lead must not be
messaged after the delay has stretched, mark them replied or suppress them.

## Where each piece runs

| piece | where | when |
|---|---|---|
| `scripts/journey_tick.js` | local, no browser, no LLM | inside `daily.sh` step 4 |
| `queue/generate-dm.js` | local | step 4, on the tick's leads-file |
| `send/messenger.js` | local Chrome | step 5 |
| `journey_tick.js --reconcile` | local | step 5b, stamps what actually sent |
| `scripts/notify_journey.js` | local → Brevo REST | step 5c |

## Commands

```bash
./linkedin-run journey plan            # who is due for what, changes nothing
./linkedin-run journey commit          # queue today's due steps
./linkedin-run journey cold            # preview the cold-lead digest
./linkedin-run journey replied <url>   # they answered — stop the sequence
```

## Two things you still owe it

1. **The video message** — `config/journey-video.md` holds only a comment. Until
   real text lands there the video step is *blocked, not sent*: `journey_tick.js`
   prints `no video message configured` and moves on. Paste David's exact words,
   with the video link in the text; `{name}` is the only substitution.
2. **Reply detection** — the engine stops on `lead.repliedAt`, and nothing sets
   it automatically yet. Today that is `./linkedin-run journey replied <url>`,
   run by a human who saw the reply. Until an inbox sweep sets it, someone who
   answers the outreach will still receive the video. This is the single most
   valuable next piece of work on this channel.

## Why the step gate, not `dmStatus`

`dmStatus: 'sent'` was the old "don't DM twice" guard, and a follow-up is by
definition a second DM to someone already marked sent. A journey entry carries
`journeyStep`, and `generate-dm.js` gates it on *that step* not being stamped in
the lead's history. The no-double-send guarantee is preserved per step; only the
blanket one was too coarse to allow a sequence.

## Why stamping happens after the send

`journey_tick.js --commit` writes `journey.pending = {stepId, queuedAt}` and
nothing more. The step is stamped only by `--reconcile`, keyed off messenger's
own `dmSentAt`. A step stamped at queue time would be skipped forever if the
send failed — the lead would drop out of the sequence with no trace.
