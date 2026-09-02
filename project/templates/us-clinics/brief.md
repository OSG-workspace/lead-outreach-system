# Brief — US med spas / clinics / dental groups (Consult-Conversion Agent)

**Date:** 2026-08-26
**Status:** SET UP.

One of four US verticals. Same chain, US region, **draft_mode=template**:
pitch.json holds the user's exact copy (v2, 2026-08-26) — the
consult-conversion / 48-hour nurture pitch (leads acquired at $150-350 each;
close to half of consults never turn into treatment). Decision-maker: owner,
COO, or director of operations. Target: multi-location (2-20) med spa /
aesthetic groups and mid-size dental groups (5-100 locations).

- **Never pitch a generic receptionist/booking agent** — Weave runs 40,000+ locations.
- HIPAA: offer a BAA up front; never reference specific patient data.
- qualify.json keep_signals: modern_booking is not a dead signal here — the
  offer starts AFTER the booking exists.
- Salutation via `{salutation}` per the contract; no em-dashes; osgdev.com in signature.
- References: vault/lead-outreach/targeting/us-clinics-scope.md (+ us-playbook-2026.md); pitch.json (fixed copy)

To fire: "fire us clinics run" → `bash tools/scripts/fire_campaign.sh us-clinics`
