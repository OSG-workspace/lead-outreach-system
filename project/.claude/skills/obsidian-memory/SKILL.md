---
name: obsidian-memory
description: Reads from and writes to the user's Obsidian vault via the MCPVault MCP server. Use at session start to load context (ICP, voice, sent-log), during runs to persist leads and gap analyses, and after sends to update statuses. Also runs the one-time bootstrap procedure that creates the vault folder structure on first use. All writes use Obsidian-native conventions: full frontmatter, wikilinks, callouts, and the lead/campaign templates at `_templates/`.
---

# Obsidian Memory

Your vault is the long-term memory of this system. Without it, every run starts from scratch. With it, every run gets smarter — fewer duplicate sends, better-tuned ICPs, sharper voice.

This skill does NOT generate filesystem-shaped Markdown. Every file it writes is **Obsidian-native**: complete YAML frontmatter, wikilinks for cross-references, callouts for important warnings, and conformance to the user's templates at `_templates/lead.md` and `_templates/campaign.md`. The vault uses Bases (`.base`) and Canvas (`.canvas`) — never write content that breaks them (e.g., wrong frontmatter property names).

## Vault folder structure

Everything lives under `lead-outreach/` in the user's vault root:

```
<vault-root>/
├── _templates/
│   ├── lead.md                        # Template for any lead note
│   └── campaign.md                    # Template for any campaign run note
└── lead-outreach/
    ├── _index.md                      # Hub page with navigation
    ├── SYSTEM.md                      # Master config (frontmatter holds defaults + last_run)
    ├── ICP-current.md                 # Active ICP — overwritten each run
    ├── ICP-archive/                   # Past ICPs (one per run)
    ├── voice.md                       # User's brand voice for emails
    ├── offer.md                       # User's services/case studies
    ├── compliance.md                  # Physical address + opt-out language
    ├── sent-log.md                    # Append-only log with wikilinks to lead notes
    ├── bounce-list.md                 # Permanent block list
    ├── run-log.md                     # One paragraph per run, newest at top
    ├── leads.base                     # Bases: filtered table/cards views of all leads
    ├── Pipeline.canvas                # Canvas: visual board of pipeline stages
    ├── leads/                         # One markdown note per lead
    │   ├── drs-karim-dental.md
    │   └── smile-studio-beirut.md
    └── follow-ups/                    # Scheduled follow-up emails
```

## Frontmatter conventions

Every file in `lead-outreach/` has a frontmatter block. The values drive `leads.base` filters and Obsidian's graph/search.

| Field | Where used | Example values |
|---|---|---|
| `type` | distinguishes file kinds | `meta`, `config`, `icp`, `lead`, `campaign`, `log` |
| `title` | Obsidian display name | `"Drs. Karim Dental Clinic"` |
| `tags` | Obsidian tag tree + Bases filters | `[lead-outreach, lead]` |
| `status` | lifecycle state for Bases | `seed`, `current`, `evergreen`, `archived`, `pending_send`, `sent`, `replied`, `bounced` |
| `related` | wikilink list of cross-references | `["[[ICP-current]]", "[[sent-log]]"]` |
| `created` / `updated` | ISO date strings | `2026-05-06` |

Lead-specific fields (`leads/*.md`):

| Field | Type | Required for | Drives |
|---|---|---|---|
| `lead_id` | string | dedup, never reuse | sent-log entries |
| `name` | string | display | Bases name column |
| `source` | enum | reporting | filter by `google_maps` / `web` |
| `email` | string | sending | dedup |
| `score`, `fit`, `reachability`, `gap_signal`, `bonus` | int | scoring | Bases sort + qualifying filter |
| `primary_gap` | string | copywriting | Bases groupBy |
| `status` | enum | pipeline state | `pending_send`, `sent`, `skipped`, `replied`, `bounced` |
| `sequence_step` | int | follow-ups | 0 = drafted, 1 = first send, 2+ = follow-ups |
| `discovered_in` | string | provenance | wikilink to campaign run |
| `sent_at` / `brevo_message_id` | string / string | auditing | filled at send time |

**Quoting rules** (MCPVault uses gray-matter):

1. Always close frontmatter with `---` on its own line.
2. Quote any string containing colons (URLs, timestamps): `url: "https://..."`.
3. List values use either inline (`tags: [lead, campaign]`) or block form — pick one and stick with it per file.
4. Wikilinks inside YAML strings always quoted: `related: ["[[SYSTEM]]"]`.

## Wikilinks

Use `[[wikilinks]]` for every cross-reference inside the vault. Never use absolute paths or markdown links for in-vault references — Obsidian tracks renames automatically through wikilinks.

| Reference | Write as |
|---|---|
| Sent-log entry → lead note | `[[drs-karim-dental]]` (Obsidian resolves by basename) |
| Run-log entry → archived ICP | `[[ICP-archive/2026-05-06-dentists-beirut]]` |
| ICP-current → SYSTEM | `[[SYSTEM]]` |
| Lead note → campaign | `[[../campaigns/2026-05-06-dentists-beirut]]` (relative paths if needed) |

## Callouts

Use Obsidian callouts where appropriate — they're visually distinct in reading view and they're part of the Obsidian-native style:

- `> [!warning]` — for things that block the run (stub voice/offer/compliance)
- `> [!danger]` — for legal/compliance issues
- `> [!info]` — for explanations of formats or schemas
- `> [!success]` — for confirmation of completed steps

## Bootstrap procedure (run once on first use)

Detect first use: `vault/lead-outreach/SYSTEM.md` does not exist.

When detected:

1. Tell the user: "First time using this system. I'm going to set up your `lead-outreach/` folder in your Obsidian vault. This takes ~30 seconds and creates 11 files — including a Bases database view and a Canvas pipeline board. None will overwrite anything you have."

2. Create the folder structure (use MCPVault's create-file equivalent). Create folders by writing a placeholder file in each, since MCPVault has no `mkdir`:
   - `lead-outreach/leads/.gitkeep`
   - `lead-outreach/follow-ups/.gitkeep`
   - `lead-outreach/ICP-archive/.gitkeep`

3. Write **`lead-outreach/_index.md`** — the hub page. Use the template in this skill's appendix, with current dates filled in.

4. Write **`lead-outreach/SYSTEM.md`** — frontmatter contains all defaults (`default_sources`, `qualifying_threshold`, `max_emails_per_run`, `approval_mode`, `last_run`) so they're queryable from Bases. Body explains layout.

5. Write **`lead-outreach/ICP-current.md`** — empty placeholder with full frontmatter (`type: icp`, `status: empty`).

6. Write **`lead-outreach/voice.md`**, **`offer.md`**, **`compliance.md`** — frontmatter `type: config`, `status: stub`. Bodies are template prompts. Each gets a `> [!warning]` callout reminding the user the pre-flight check refuses runs while these are stubs.

7. Write **`lead-outreach/sent-log.md`**, **`run-log.md`**, **`bounce-list.md`** — `type: log`, `status: evergreen`. Bodies define the entry format using a `> [!info]` callout.

8. Write **`lead-outreach/leads.base`** — see the appendix. Five views: All leads, Pending send, Sent (last 14 days), Disqualified, By gap.

9. Write **`lead-outreach/Pipeline.canvas`** — see the appendix. Six group nodes (Sourced → Scored → Gap → Drafted → Sent → Replied) with explanatory text nodes.

10. Write **`_templates/lead.md`** and **`_templates/campaign.md`** — these are vault-root templates so the Templates community plugin can pick them up.

11. Tell the user: "Setup complete. Three files need your input before the first run:
    - `lead-outreach/voice.md` — how your emails should sound
    - `lead-outreach/offer.md` — what you actually deliver
    - `lead-outreach/compliance.md` — your physical address (legally required)

    Once those are filled in, prompt me with: 'find me dentists in Beirut, pitch on bad websites' (or whatever)."

## Read protocol (every session)

At session start, the orchestrator (or you if no orchestrator runs) calls into this skill to load:

| File | Purpose |
|---|---|
| `_index.md` | Confirm vault is bootstrapped |
| `SYSTEM.md` | Read defaults from frontmatter, parse `last_run` |
| `ICP-current.md` | Active rubric (may be empty) |
| `voice.md` | Required for copywriting; if `status: stub` STOP and ask user to fill it |
| `offer.md` | Same |
| `compliance.md` | Same |
| `sent-log.md` | Required for dedup |
| `bounce-list.md` | Required for dedup |

If any required file is missing, tell the user EXACTLY which file to fill in. Don't guess content for them.

## Write protocol (during a run)

### Per lead

Create `lead-outreach/leads/<lead-slug>.md` using the **lead template shape** (the values, not the Templater syntax — that only works in Obsidian itself):

```markdown
---
type: lead
title: "Drs. Karim Dental Clinic"
lead_id: maps-drs-karim-dental
name: "Drs. Karim Dental Clinic"
source: google_maps
url: "https://drkarim.com"
email: "info@drkarim.com"
phone: "+961-1-739012"
location: "Hamra, Beirut"
score: 92
fit: 35
reachability: 25
gap_signal: 27
bonus: 5
primary_gap: bad_website
status: pending_send
sequence_step: 0
discovered_in: 2026-05-06-dentists-beirut
created: 2026-05-06
updated: 2026-05-06
tags:
  - lead-outreach
  - lead
related:
  - "[[../ICP-current]]"
  - "[[../leads.base]]"
sources: []
---

# Drs. Karim Dental Clinic

> [!info] At a glance
> **Score**: 92 / 100  ·  **Gap**: bad_website  ·  **Status**: pending_send

## Score breakdown

- **fit** — 35: Listed as Dentist on Maps, in Beirut, independent
- **reachability** — 25: public email + website
- **gap_signal** — 27: single-page site, last updated 2019, no booking
- **bonus** — +5: 4.7★ with 87 reviews

## Gap analysis

Static brochure site, phone-only booking, no mobile responsiveness. Strong reputation makes this high-intent.

## Pitch angle

"You already rank #1 in Hamra. The piece holding back online bookings is the website."

## Email drafted

**Subject**: drkarim.com — quick idea
**Body**:

```
<full body text including compliance footer>
```

## Sources

- [[../campaigns/2026-05-06-dentists-beirut]]

## Related

[[../sent-log]] · [[../leads.base]]
```

Use the lead's natural slug (kebab-case of the business name, deduplicated with `-2`, `-3` if collision).

### Per send

After send, update the same file's frontmatter (`status: sent`, `sent_at: 2026-05-06T13:42:00Z`, `brevo_message_id: <id>`). The pipeline is in **single-email mode** — there is no follow-up scheduling, so do NOT set `next_step_due` and leave `sequence_step` at `1` permanently.

Append to `sent-log.md` (use a wikilink to the lead note):

```
2026-05-06 | info@drkarim.com | [[drs-karim-dental]] | step 1 | 2026-05-06-dentists-beirut | <brevo_message_id>
```

### Per run

Append one paragraph to `run-log.md`. Newest entries go at the **top** (right after the frontmatter and the explanatory callout):

```markdown
## 2026-05-06 — Dentists in Beirut

Sourced 60 leads from Google Maps + web. 47 unique after dedup. 18 qualified (score ≥ 70). Top gap was outdated/static websites (12/18). Sent 16 emails, 2 skipped (invalid email format). Avg score 76. Notable: [[drs-karim-dental]] scored 92 — best fit for "bad website" pitch this week.

ICP: [[ICP-archive/2026-05-06-dentists-beirut]]
```

Update `SYSTEM.md` frontmatter `updated` and `last_run` fields.

Move the previous `ICP-current.md` body to `ICP-archive/<run-slug>.md` (preserve frontmatter, set `status: archived`).

## MCP tool usage

Use MCPVault's read/write tools. Common operations:

- **Read a file**: `obsidian:read-file` with path relative to vault root
- **Write/overwrite**: `obsidian:write-file`
- **Append**: `obsidian:append-to-file` (ideal for sent-log, run-log)
- **List a folder**: `obsidian:list-files` with path

Always use forward-slash paths relative to vault root (e.g., `lead-outreach/leads/drs-karim-dental.md`), never absolute paths.

## Failure modes

- **MCPVault not connected** → tell the user the MCP server isn't loaded; suggest running `/mcp` in Claude Code or checking `.mcp.json`
- **Vault path wrong** → MCPVault will return 0 files; verify `OBSIDIAN_VAULT_PATH` in `.env`. Note that vault paths can contain spaces; the MCP loader handles those, but shell-sourced `.env` does not.
- **File exists collision** → don't overwrite a lead note that has `status: sent`. Skip and log a warning.
- **Frontmatter parse error** → check the gray-matter quoting rules above. Don't ship broken YAML; it crashes Bases queries silently.
- **Bases view shows nothing** → verify the `lead_id`, `score`, `status` properties are present on lead notes (not just the title).
- **Canvas won't open** → JSON syntax error; validate with `python3 -c "import json,sys; json.load(open('Pipeline.canvas'))"`.
