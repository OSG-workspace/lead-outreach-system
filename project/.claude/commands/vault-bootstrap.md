---
description: Bootstrap the Obsidian vault folder structure. Run this once on first use, or to repair a damaged vault. Creates lead-outreach/ folder with all required files.
---

# /vault-bootstrap

Sets up the `lead-outreach/` folder in your Obsidian vault. Idempotent — safe to run multiple times. Won't overwrite files that already have content.

## How to use

```
/vault-bootstrap
```

## What this does

Invokes the `obsidian-memory` skill's bootstrap procedure. Creates:

- `lead-outreach/SYSTEM.md` — system config
- `lead-outreach/ICP-current.md` — placeholder for active ICP
- `lead-outreach/voice.md` — template for your brand voice (you fill in)
- `lead-outreach/offer.md` — template for your services (you fill in)
- `lead-outreach/compliance.md` — physical address + opt-out (you fill in — REQUIRED before sending)
- `lead-outreach/sent-log.md` — empty append-only log
- `lead-outreach/bounce-list.md` — empty exclusion list
- `lead-outreach/run-log.md` — empty run history

After this, edit `voice.md`, `offer.md`, and `compliance.md` in Obsidian, then run `/find-leads "..."`.

## Instructions to Claude

If files already exist with non-template content (i.e., user has already filled them in), do NOT overwrite. Just confirm what's present and report any missing pieces.

Output a checklist at the end:

```
✓ SYSTEM.md
✓ ICP-current.md
☐ voice.md         — needs your input
☐ offer.md         — needs your input
☐ compliance.md    — REQUIRED before sending
✓ sent-log.md
✓ bounce-list.md
✓ run-log.md

Next: edit the 3 files marked ☐, then run /find-leads
```
