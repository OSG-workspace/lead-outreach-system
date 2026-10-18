# dm-setter: an AI Instagram DM setter for high-ticket coaches

It answers a coach's Instagram DMs in that coach's voice. It qualifies each lead, takes them through the setter
script and sends the booking link once the lead has earned it. A human closer then runs the call. It uses only
Meta's official Instagram API (Instagram Login) and is built to the October 2026 research brief. Every AI call
(Claude, the Jev guardrails, voice-note transcription) goes through **one OpenRouter key**. That means:
webhook → durable queue → one structured Claude call per turn → guardrails → human-timed sending, with
draft/autopilot modes, human takeover, evals, and one folder per coach.

It is a standalone product, built to be deployed for one online coach, who uses it to answer the leads who DM
them. It has no connection to the lead-outreach pipeline it currently sits next to: it imports nothing from it,
shares no data or keys with it, and the folder can be moved anywhere as it is. Packs for more coaches can be added
later (one folder each), but a single coach needs nothing beyond their own folder.

## Try it in two minutes (no Instagram needed)

```bash
cd dm-setter
cp .env.example .env          # set OPEN_ROUTER_API_KEY (the account needs credit)
./setter-run chat demo-coach  # you are the lead; "Marcus" (a fictional coach) answers
./setter-run eval demo-coach  # 9 simulated leads, graded (~$0.10-0.30, estimate)
./setter-run test             # 46 offline tests
```

## Demo account: let any coach try a setter built for them, inside Instagram

One Instagram account (e.g. @yourbranddemo, a Professional account whose bio says it's an AI setter demo) is connected
to the `demo-hub` pack. Each coach you pitch gets their own pack with a code, and they try it by DMing that account.

```bash
./setter-run demo-pack jane --code JANE7 --name "Jane Doe" --handle janedoe.coach \
    --url https://janedoe.com --url https://janedoe.com/program --notes ~/Desktop/jane-youtube-transcript.txt
#   -> coaches/jane/ drafted from her site + transcripts (~$0.04); facts it couldn't find are TODO lines
./setter-run chat jane          # review: talk to it as a lead until it sounds like her
./setter-run demo-codes         # the hub's account + every DEMO <code>
```

Then message her: "I built an AI setter trained on your offer. DM **DEMO JANE7** to @yourbranddemo and pretend
you're a lead." On the demo account:

| She sends | What happens |
|---|---|
| `DEMO JANE7` | The thread becomes Jane's setter and starts fresh. Her voice, script, qualifying questions and booking link, with typing dots and natural delays. |
| anything without a code | A fixed menu ("reply DEMO + the code you were sent"). No model call. |
| `RESET` | Back to the menu, history hidden. Send DEMO + a code to try again or try another setter. |
| a message that trips a guardrail | Instead of going quiet, a short `[demo note: in the live version this chat goes to Jane now: ...]`. That shows the safety layer. |

Demo threads always answer at once (no draft mode, active hours or nudges, 6 s debounce). They stop after
`demo.max_replies` bot turns, and each coach pack's `limits.daily_usd` caps what its demos can spend. "Comment DEMO"
on a reel sends the menu as a private DM (`comment_triggers` in `coaches/demo-hub/coach.yaml`). She always messages
first, so the 24 h window is never a problem. If she likes it, she connects her own account at `/connect/jane`
and the same pack runs on her real leads (start with `mode: draft`).

## How a message flows

```
Instagram ─webhook─► /webhooks/instagram   verify X-Hub-Signature-256, mirror to SQLite, ack in ms
                         │
                     jobs table            debounce 20 s per thread (a burst of 3 messages = 1 reply)
                         │                 one job per thread at a time; survives restarts
                         ▼
  reply_job: coach's active hours? → voice note → text, photo → one sentence (once, cheap model)
           → free exits ("thanks!" after booking = a heart, no model call)
           → daily $ cap → INBOUND GATES (patterns + Jev: bot question, self-harm, minor, health,
             complaint, opt-out, injection, ready-to-buy)
           → rolling summary if the thread is long → CLAUDE (one call, JSON: bubbles, stage, slots,
             confidence, handoff, send_link, heart)
           → stage machine validates the move → STYLE PASS (coach's casing, periods, emoji, no em dashes)
           → OUTBOUND GATES (price, guarantee, "I'm human", advice, stray URLs) → link gating
                         ▼
  decide:  DRY_RUN → "dry" draft · draft mode / any flag / low confidence / window closed → draft + ping
           otherwise → deliver: mark seen, heart, read pause, typing… per bubble, stop if the lead writes again
                         ▼
  echo of a message the bot didn't send = the coach typed in the app → bot steps back 12 h on that thread
  booking webhook (token in the link) → stage BOOKED, nudges cancelled, ping
  one context-aware nudge per 24 h window if the lead goes quiet, never more
```

## What makes it read like the coach

| Layer | What it does |
|---|---|
| **Coach pack** (`coaches/<slug>/`) | `voice.md` (how they text, in words), `script.md` (what to ask at each stage, objection answers), `offer.md` (the only facts it may state), `faq.md`, and `examples/` (real conversations that booked) |
| **`train`** | Reads the coach's Instagram data export. It measures casing, trailing periods, emoji rate and favourites, message length, messages per turn, openers, laugh and repeated phrases (`style.yaml`). It picks their best conversations that reached the booking link, anonymised (`examples/auto-*.txt`). `--draft-voice` has Claude write a first `voice.md` from 300 real messages |
| **Style pass** | Deterministic and costs no tokens. It enforces the measured habits on every bubble even when the model drifts: lowercase starts, no full stop at the end, emoji removed for coaches who never use them, no em dashes, semicolons or markdown, long messages split |
| **Timing** | 20 s debounce, mark-seen, a read pause sized to their message, a typing indicator for a time proportional to each bubble (≈6–9 characters/s), gaps between bubbles, and silence outside the coach's hours unless the 24 h window would close. If the lead writes mid-send, the bot stops and answers everything together |
| **Conversation craft** | One question per turn, mirrors the lead's length, reacts before asking, never repeats something the lead already said (known facts are in its context), hearts story replies and wins, follows the NEPQ-style stage flow, and invites rather than pushes |
| **Human takeover** | When the coach types in the Instagram app, the bot steps back on that thread for 12 h. Every reply can be approved, edited or written by hand in the dashboard |

To customise a coach: `./setter-run new-coach <slug>`, fill in `coach.yaml`, `offer.md` and `script.md`, then
`./setter-run train <slug> ~/Downloads/instagram-export.zip --draft-voice`. Review `voice.draft.md` and save it as
`voice.md`. Talk to it with `chat <slug>` until it sounds right, then run `eval <slug>`. Edits apply to the next
reply without a restart.

## Token economy

| Lever | Effect |
|---|---|
| Prompt caching, 3 breakpoints | The shared rules (identical for every coach), the coach pack, and the conversation up to the bot's last turn are all cache reads (0.1× price). Per turn, only the lead's new message and a short context note are billed at full price. Volatile values (time, stage, known facts) sit after the last breakpoint, in a system message that is never stored. A test pins this layout: `test_prefix_is_stable_between_turns` |
| Sonnet 5.5 at effort `low` | Chat doesn't need deep thinking. `REPLY_THINKING=off` removes thinking tokens entirely |
| Rolling summary | Past 36 live messages, the oldest are folded into a ≤120-word summary by Haiku. History stops growing, and the cache prefix changes only then |
| Debounce | A burst of messages costs one call, not one per message |
| Media once | Voice notes become text and photos become a sentence one time (cheap model), so later turns never re-pay image tokens |
| No-call paths | Acknowledgements after booking get a heart, opt-outs stop, handed-off threads and the coach's own replies trigger no model call |
| Jev for yes/no | The guardrail judgments cost ~$0.00004 a turn instead of a second LLM call |
| Spend tracking + cap | Every call logs input, cache write, cache read and output tokens with dollars. The dashboard shows spend and cache-hit rate per coach. `limits.daily_usd` stops spending and hands replies to a person |

**Measured once, on two turns** (2026-10-19, demo-coach through OpenRouter). The first turn wrote the 4,764-token
prefix to the 1 h cache for $0.021. The second turn read it back and cost **$0.0024**: 162 new input tokens, 4,764
cached, 148 output tokens. A Haiku call cost $0.000014. From those numbers, a 15-turn conversation should come to
about $0.04–0.06, plus one cache write ($0.02) each time a coach's prefix goes cold after an hour without chats. These
come from one test thread, so run `./setter-run stats` after the first real day to get real per-conversation figures.

**One key: OpenRouter.** Claude goes through OpenRouter's Anthropic-format endpoint (`/api/v1/messages`). Cache
breakpoints with the 1 h TTL, effort, JSON-schema output, thinking and the mid-conversation system note all pass
through as they are, and the measured turn above shows the cache working. `provider.order=anthropic` keeps Claude
served by Anthropic (Bedrock or Vertex only during an outage). `session_id` = coach:thread keeps each thread on one
provider, so its cache stays warm. If Claude declines or is down, the turn retries on `LLM_FALLBACK_MODEL` (Opus 4.8).
Voice notes go to OpenRouter's transcription endpoint (`openai/whisper-large-v3-turbo`), and Jev uses the same key.
The cost logged per call is the cost OpenRouter reports, not an estimate from a price table.

## Guardrails and honesty

- **It never quotes price, promises results or gives medical, legal or financial advice.** This is checked twice:
  code patterns, then Jev. Jev can only make a turn stricter. Any hit holds the reply for a person.
- **It writes as the coach, in first person, but never claims to be human and never denies being AI.** "Is this a
  bot?" goes to the coach (`bot_question: handoff`) or gets the coach's own honest line (`disclose`). The
  research brief is explicit: California SB 1001, Maine §1500-DD, Utah §13-77-103, EU AI Act Art. 50 and Meta's policy
  all require disclosure, and being caught lying kills the trust the closing call depends on. Disclosure is set
  per coach: a bio line (`profile`, and autopilot stays off until `profile_confirmed: true`) or a short line in the
  first automated message (`first_message`).
- Self-harm, minors, health conditions, complaints, injection attempts and voice notes it can't transcribe go to a
  person. Self-harm alerts are marked URGENT.
- Messages are sent only inside Meta's 24 h window. After it closes, only a human-edited reply can go out, through
  the HUMAN_AGENT tag (7 days, after App Review). The bot never uses that tag for its own text.
- `STOP` opts a lead out and `DELETE` erases their conversation (the privacy page promises this).
- Every Jev threshold in `setter/questions.py` is **provisional**. 18/18 hand-labelled samples were right on
  2026-10-18, but I wrote those labels myself, so they are not an independent test. Calibrate on 15–20 real labelled
  DMs per question before trusting autopilot (`data/jev-trail.jsonl` records every answer). Kill switches:
  `JEV=off`, `JEV_INBOUND|JEV_OUTBOUND=on|shadow|off`, `SETTER_ENABLED=0`.

## Going live

1. **Server**: a VPS with HTTPS. `deploy/Caddyfile` handles the certificate and `deploy/dm-setter.service` runs it. For
   local tests, `cloudflared tunnel --url http://localhost:8000` gives a public https URL.
2. **Meta app**: developers.facebook.com → Create app (Business) → add **Instagram** → *API setup with Instagram
   login*. Copy the Instagram app ID and secret into `.env`. Under Webhooks, set the callback to
   `https://<host>/webhooks/instagram` with your `IG_VERIFY_TOKEN`, and subscribe to `messages`,
   `messaging_postbacks`, `messaging_seen`, `message_reactions`, `messaging_referral`, `comments`. Add
   `https://<host>/oauth/callback` as the OAuth redirect URI, and `https://<host>/privacy` and `/data-deletion` as
   the policy URLs.
3. **The coach's Instagram**: it must be a Business or Creator account, with *Settings → Messages and story replies →
   Message controls → Connected tools → Allow access to messages* switched on.
4. **Connect**: open `https://<host>/connect/<slug>` (dashboard login), and the coach approves. Alternatively, generate a token
   in the App Dashboard and run `./setter-run connect-token <slug> <token>`. Tokens refresh themselves.
5. **Roll out**: `DRY_RUN=1` for a few days (every would-be reply appears as a dry draft) → `DRY_RUN=0` with
   `mode: draft` (the coach approves each reply, which also tells you whether `min_confidence` is right) →
   `mode: autopilot`.
6. **App Review** before serving accounts you don't own: Advanced Access for `instagram_business_basic`,
   `instagram_business_manage_messages`, `instagram_business_manage_comments` and Human Agent. Budget 2–4+ weeks. The
   screencast must show a real DM being received and answered through the API.
7. **Bookings**: in Calendly or Cal.com, add a webhook to `https://<host>/hooks/booking/<BOOKING_WEBHOOK_SECRET>`.
   The link the bot sends carries `utm_content=st_…`, which the webhook matches back to the conversation.

## Commands

| | |
|---|---|
| `./setter-run serve [--host 0.0.0.0 --port 8000]` | webhook server + worker + dashboard (`/admin`, user `admin`) |
| `./setter-run check` | what's configured, what's missing, which coaches are ready |
| `./setter-run new-coach <slug>` | a new pack from `coaches/_template` |
| `./setter-run train <slug> <export.zip> [--me "Name"] [--draft-voice]` | learn the voice from an Instagram export |
| `./setter-run connect-token <slug> <token>` | attach an account without OAuth |
| `./setter-run chat <slug>` | be the lead in the terminal (`/state`, `/new`, `/quit`) |
| `./setter-run eval <slug> [--only a,b] [--turns N]` | simulated leads (`evals/personas.yaml` + `coaches/<slug>/personas.yaml`) |
| `./setter-run prompt <slug> [--count]` | print the cached prefix and its token count |
| `./setter-run stats [--days N]` | spend, cache hit rate, $/conversation, bookings per coach |
| `./setter-run test` | the offline test suite |

## Files

```
setter/app.py        HTTP: webhooks, booking hook, Instagram Login connect, privacy pages
setter/engine.py     the loop: ingest, reply/nudge jobs, decide, deliver, takeover, bookings, comment→DM
setter/demo.py       demo account: DEMO <code> / RESET routing, menu, guardrail notes, per-run cap
setter/demopack.py   demo-pack: a coach pack drafted from their site and transcripts
setter/brain.py      prompt layout (cache breakpoints), context note, output parsing, rolling summary
setter/prompts.py    the setter rules every coach shares
setter/coach.py      coach packs: loading, the cached coach block, the output schema
setter/questions.py  ALL guardrail questions, patterns and thresholds (the file to review)
setter/gates.py      inbound/outbound gates (patterns + Jev, strict-only, trailed)
setter/humanize.py   style pass, typing/read timing, active hours
setter/stages.py     the stage machine and link gating
setter/llm.py        Claude calls through OpenRouter, fallbacks, spend accounting
setter/instagram.py  Graph API client (send, typing, react, private replies, profiles, OAuth, refresh)
setter/webhook.py    payload parsing + signature check
setter/media.py      voice notes → text (OpenRouter Whisper), photos → a sentence
setter/admin.py      the dashboard
setter/train.py      Instagram export → style.yaml + examples + voice draft
setter/evals.py      sandbox (chat/eval) and the simulated-lead grader
setter/worker.py     job loop, token refresh, comment reconciler
setter/jev_client.py vendored stdlib Jev client (cache, kill switches, fallback)
coaches/_template/   start here for a new coach   ·   coaches/demo-coach/  a fictional, filled example
data/                SQLite db, Jev cache + trail, eval reports, train scratch (gitignored: lead PII)
```

Not built yet (phase 2 in the brief): offering calendar slots directly ("Tue 2pm or Wed 10am?"), CRM sync
(GHL/HubSpot), cloned-voice replies, multi-tenant billing. Storage is SQLite with a DB-backed queue, which is plenty
for a handful of coaches on one VPS. Move to Postgres + Redis when one server stops being enough.
