# The vault — this system's long-term memory

**Nothing in this folder except this README is committed.** The vault holds
contact PII (who was emailed, when, and at what address) and high-churn machine
state that every run rewrites. Both belong on the operator's machine, not in a
shared repo.

This file documents the structure so the pipeline is still legible without it,
and so a fresh checkout can bootstrap an empty vault that works.

## Where it lives

The canonical vault is a real Obsidian vault, pointed at by `OBSIDIAN_VAULT_PATH`
in `project/.env`:

```
/Users/<you>/Documents/Obsidian Vault/lead-outreach
```

`project/vault/lead-outreach/` is the working copy the scripts read through the
relative path `vault/...` from the `project/` cwd. **There is exactly one
`sent-log.md` that matters** — never introduce a second.

## Layout

```
project/vault/
└── lead-outreach/
    ├── SYSTEM.md            how the system presents itself (sender identity, boundaries)
    ├── ICP-current.md       the live ideal-customer profile
    ├── offer.md             what is actually being sold, in the operator's words
    ├── voice.md             base writing voice for all copy
    ├── voice-us.md          US variant + the banned-word list the drafters enforce
    ├── voice-lb-wa.md       Lebanon WhatsApp variant
    ├── compliance.md        opt-out, GDPR/CAN-SPAM posture
    ├── deliverability.md    sending limits, warm-up notes, bounce policy
    ├── targeting/           per-campaign targeting docs and overrides
    │
    ├── sent-log.md          ← PII. every address ever contacted. THE dedup source of truth
    ├── bounce-list.md       ← PII. hard bounces, synced from Brevo
    ├── suppression.md       ← manual do-not-contact list
    ├── leads/               ← PII. per-lead records
    │
    ├── sourced-log.txt      ledger: every domain any run has ever sourced
    ├── disqualified-log.txt ledger: domains retired forever, with the reason
    │                        (`hotel-volume-too-low`, `freemail-only`,
    │                         `no-direct-email`, `dead-domain`)
    └── overpass-cities-fired.txt
                             ledger: `vertical|city` ground already swept, so
                             consecutive fires open fresh geography
```

## What each ledger is load-bearing for

| Ledger | Enforced at | Effect if missing |
|---|---|---|
| `sent-log.md` | Stage 3 merge, Stage 5 extract | the same business gets emailed twice |
| `disqualified-log.txt` | Stage 3 merge | proven-unqualified and proven-unreachable domains get re-sourced and re-enriched at full agent cost |
| `sourced-log.txt` | Stage 3 merge | already-seen domains resurface every run |
| `overpass-cities-fired.txt` | Stage 2 source | every fire sweeps the same cities and yields nothing new |

These are append-only. Nothing rewrites a row; a domain leaves circulation only
by being added to `sent-log.md` or `disqualified-log.txt`.

## Bootstrapping an empty vault

The pipeline needs the files to exist, not to have content. From `project/`:

```bash
mkdir -p vault/lead-outreach/{leads,targeting}
cd vault/lead-outreach
touch sent-log.md bounce-list.md suppression.md \
      sourced-log.txt disqualified-log.txt overpass-cities-fired.txt
```

Then write the copy specs — `SYSTEM.md`, `ICP-current.md`, `offer.md`,
`voice.md` — in your own words. The drafters read them verbatim, so they are
the single biggest lever on what the emails actually sound like. `fire.md`
aborts loudly if `sent-log.md` is missing, which is the intended signal that
the vault was never initialised.

## A note on the retired backlog

`qualified-pending.jsonl` used to live here: leads that qualified but were never
contacted, replayed on later fires. It was removed on 2026-08-05. Measured
across every run on disk, 92% of the leads it replayed (3,760 of 4,095) had a
findable decision-maker but no publishable direct email — a verdict that does
not change on retry, so the backlog re-researched permanently unreachable
businesses at full cost. Those domains are now written to
`disqualified-log.txt` at Stage 5.5 instead, and a campaign whose ground is
swept halts with a supply message rather than replaying dead leads.
