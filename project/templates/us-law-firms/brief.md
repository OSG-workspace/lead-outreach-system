# Brief — US Law Firms (Time-Capture / Billing-Leakage Agent)

**Date:** 2026-08-26 (offer + copy replaced; original 2026-06-08 inbox/scheduling brief retired)
**Status:** SET UP. David fires manually.

## Target
US law firms, two sub-segments that must NEVER receive each other's pitch:
- **Hourly-billing firms** (litigation, insurance defense, corporate mid-law,
  family law), 10 to 200 attorneys → template 1A (billing leakage).
- **Plaintiff-PI firms** (contingency), 5 to 50 attorneys → template 1B
  (medical-records chronologies). Auto-routed by pitch.json variant keywords.

Decision-maker: managing partner, COO/executive director, or billing manager.
US only.

## Offer (for the call, NOT beyond the fixed copy)
An agent that reconstructs unbilled time from calendar/email/documents and
drafts pre-bills (firms invoice only ~88% of work done); for PI, an agent that
turns 1,000+ page medical records into chronologies and demand-package inputs.
**Never pitch intake/receptionist/missed calls** — saturated since 1965.

## How this run works
- **Same pipeline chain**, US region, law vertical.
- **draft_mode = template**: pitch.json holds the user's exact copy (v2,
  2026-08-26). 1A is the default; 1B fires when the firm's scraped pages match
  PI/contingency markers (draft_emails.py `variants`).
- **qualify.json keep_signals**: `modern_booking` is NOT a dead signal here —
  the offer has nothing to do with booking widgets.
- **Salutation**: `Hi {salutation}` renders `Hi Mr./Mrs. <Surname>,` or
  `Hi <First>,` per the salutation contract; leads with no resolved name are
  dropped as always.

## Authoritative references
- Qualification scope (2025-26 playbook, corrected): `vault/lead-outreach/targeting/us-law-firms-scope.md` (+ `us-playbook-2026.md`)
- Fixed copy: `pitch.json` in this folder (1A default + 1B `pi` variant)
- Legacy targeting thesis (old inbox/scheduling offer, superseded): `vault/lead-outreach/targeting/us-inbox-scheduling.md`

## To fire
"fire us law firms run" → `bash tools/scripts/fire_campaign.sh us-law-firms`
