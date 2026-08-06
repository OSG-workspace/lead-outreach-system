# Deliverability Status

> Skeleton. Track sending reputation here; the operator reads it before raising
> volume. Nothing automated depends on it.

## Warm-up State: cold_start

[cold_start | warming | steady_state]

## Domain Authentication

- SPF:   [pass/fail — configured?]
- DKIM:  [pass/fail]
- DMARC: [policy]
- MX on the sending domain: [needed if you want replies to land]

## Rules

- Daily cap: [start low. 20-30/day on a new domain.]
- Raise volume only after [N] days with bounce rate under [X]%.
- Hard bounces are synced to `bounce-list.md` and suppressed automatically.

## History

| Date | Sent | Bounced | Notes |
|---|---|---|---|
| | | | |
