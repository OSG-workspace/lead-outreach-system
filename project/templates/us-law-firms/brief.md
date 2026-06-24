# Brief — US Small Law Firms (Inbox & Scheduling Automation)

**Date:** 2026-06-08
**Status:** SET UP, NOT FIRED. David fires manually.

## Target
Small US law firms (2 to 20 attorneys). Decision-maker: managing partner or
office/operations manager. US only.

## Offer (for the call, NOT for the email copy)
David Geha / Automate (automatelb.com) sets up an AI assistant that quietly
handles a firm's repetitive inbox + scheduling work: triaging new-client intake
emails, booking consults into the calendar, sending routine follow-ups.

## How this run differs from the standard GCC chain
- **Same pipeline chain**, different region (US), different leads (law firms),
  and a **different drafting mode**.
- **draft_mode = custom**: no template. The `gap-writer` Haiku agent writes a
  unique email per firm built on the specific manual task it finds for that firm.
- **No money talk** anywhere in the email.
- **Keep** the `David Geha / Automate, automatelb.com` signature + link in every email.
- **Salutation**: `Hello Mr./Mrs. <Surname>,` (existing contract; leads without a
  resolved name + gender are dropped).

## Authoritative references
- Targeting: `vault/lead-outreach/targeting/us-inbox-scheduling.md`
- Chat overrides: `vault/lead-outreach/targeting/us-inbox-scheduling.OVERRIDES.md`
- Email voice: `vault/lead-outreach/voice-us.md`

## Control files in this folder
- `countries.txt` = US
- `source_agent.txt` = source-agent-us
- `vertical.txt` = law
- `draft_mode.txt` = custom
- `channels.json` = ["email"]
- `icp.yaml`, `queries.txt`, `targeting.md`

## To fire
`/fire 2026-06-08-us-law-firms`
