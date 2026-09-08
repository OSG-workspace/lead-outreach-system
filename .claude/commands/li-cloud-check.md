---
name: li-cloud-check
description: The cloud supervisor for the LinkedIn journey. Reads the committed digest, decides whether the drip is running, and emails David when it has stalled or a lead has gone cold. Sends nothing to LinkedIn.
---

# /li-cloud-check — the cloud half of the LinkedIn journey

You are running **in the cloud**, on a clone of this repo. You have no browser,
no `state/`, and no way to touch LinkedIn — and you must not try. Sending needs
the logged-in Chrome on David's Mac. Your entire job is to answer one question
from `project/linkedin/ops/journey-digest.json`:

> Is the drip running, and does David need to do something today?

## Do exactly this

```bash
cd project/linkedin
node scripts/journey_digest.js --check      # prints staleness; exit 1 = stale
node scripts/notify_journey.js --from-digest --send
```

`notify_journey.js --from-digest` makes the decision itself and sends at most one
email to david@osgdev.com:

| digest says | it sends |
|---|---|
| last run < 36h ago, nothing cold | **nothing** — a quiet day is the healthy day |
| last run < 36h, cold leads present | the cold-lead digest: who took the outreach and the video and said nothing |
| last run > 36h ago | the stalled-channel email: how long, and which steps are waiting |

It needs `BREVO_MCP_TOKEN` and `BREVO_SENDER_EMAIL` in the environment (the Mac
reads them from `project/.env`, which does not exist here). If they are missing
the script exits 1 and says so — **report that plainly, do not work around it**
and do not paste a key anywhere.

## What to report back

One or two sentences. Whether the drip is running, how long since the Mac last
ran, how many steps are overdue, and whether an email went out. If nothing was
wrong, say so in one line — quiet is the expected outcome most days.

## Never

- Never open, fetch, or automate linkedin.com from here.
- Never edit `state/`, `config/limits.json` or the journey config to "unstick"
  anything. A stalled channel is a laptop that was off, not a bug to route around.
- Never send a lead a message from this session. The cloud notifies **David**;
  only the Mac messages leads.
