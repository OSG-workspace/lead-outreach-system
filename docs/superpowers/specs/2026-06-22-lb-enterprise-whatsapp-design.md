# lb-enterprise — WhatsApp custom-consultant run (design)

Date: 2026-06-22
Status: approved (structure + positioning locked by user)
Author: David Geha / Claude

## Goal

A NEW, self-contained pipeline that WhatsApps the **biggest Lebanese companies**
(Elie Saab, Aishti, etc.). Per company it (1) analyzes the company's workflow gaps
and repetitive tasks, (2) frames how David Geha and his team would help with custom
AI, and (3) forms a tailored WhatsApp message out of those two. It sources the same
way every other chain does (parallel DDG `source-agent` fan-out), resolves the
CEO/decision-maker's direct mobile, and auto-sends over the existing WhatsApp
bridge. Triggered by "fire lebanese run". Must not interfere with any existing run.

## Non-goals

- No email channel for this run (WhatsApp only).
- No runtime approval gate (auto-send like `/fire`). The user pre-approved the
  message *structure* once, up front (this doc); runs fire without prompting.
- No changes to GCC / US / lb-receptionist / worldwide behavior.

## Positioning (locked)

- Identity in the body: **"David Geha, a third-year engineering student at AUB"** —
  NOT "from Automate".
- Social proof: **"I work with a team building custom AI systems for clients across
  Lebanon, the GCC, and India."** (Do NOT say "small team".)
- **No money talk** (no price/fee/commission/"free"/"$"), like the US custom rule.
- Personal salutation contract still applies: `Hello Mr. <Surname>,` / `Hello Mrs.
  <Surname>,`.
- No em-dashes / en-dashes anywhere.
- **No `automatelb.com` link and no "Automate" mention.** Signature is just
  `David Geha`. (This run is the one exception to the system-wide automatelb.com
  rule, by user request.)

## Locked message structure

```
Hello Mr./Mrs. <Surname>,

<1-2 sentences: this company's specific manual workflow / repetitive-task gap,
 proving we actually looked>

I'm David Geha, a third-year engineering student at AUB. I work with a team building
custom AI systems for clients across Lebanon, the GCC, and India, and we'd take that
kind of repetitive <intake/follow-up/...> off your team so they get their time back
and customers get instant answers around the clock. You own the system, no platform
lock-in.

Worth a short call this week to show you what it would look like for <Company>?

David Geha
```

The two analysis paragraphs (workflow gaps; how-we-help) are produced and stored as
reviewable metadata; only the message above is sent.

## Architecture (mirrors existing patterns)

New, additive only. Each existing run is untouched because it keeps `"email"` in its
`channels.json` and uses its own `source_agent.txt` / `draft_mode.txt`.

1. **`.claude/agents/source-agent-lb-enterprise.md`** — clone of `source-agent-lb`,
   retargeted to the *biggest* Lebanese companies & brands (fashion/couture houses,
   luxury retail groups, banks, FMCG/industrial groups, conglomerates, hospitality
   groups). Same tools (WebSearch + Write), same pipe-delimited output
   `domain|BusinessName|LB|vertical|branches`, country always `LB`.

2. **`.claude/agents/wa-writer.md`** — new Haiku agent, the WhatsApp analog of
   `gap-writer`. Tools: Read, Write, WebSearch, WebFetch. For ONE company:
   - reads `voice-lb-wa.md` (REQUIRED first),
   - reads 2-4 already-scraped HtmlFiles,
   - writes `workflow_gaps` (4-5 sentence paragraph),
   - writes `how_we_help` (4-5 sentence paragraph),
   - forms `body_text` (the locked-structure WhatsApp message),
   - writes one JSON to its OutputFile.
   Hard guards re-checked before Write: salutation, zero money words, zero em/en
   dashes, **no `automatelb.com` / no "Automate" mention**, signature is exactly
   `David Geha`, identity line is the AUB-student line, opener names something real
   about THIS company (not category-level).

3. **`project/vault/lead-outreach/voice-lb-wa.md`** — new voice spec the `wa-writer`
   reads. Encodes the positioning + locked structure above.

4. **`name-finder`** — reused unchanged. Already resolves CEO/decision-maker direct
   mobile and validates Lebanon +961 mobile prefixes. **CEO direct number required;
   company dropped if unresolved** (kill-on-fallback). No generic/company numbers.

5. **`project/tools/scripts/draft_whatsapp_custom.py`** — new, `--phase prep|merge`
   exactly like `draft_custom.py` but for WhatsApp:
   - `prep`: reads `leads-with-contact.json`, writes per-company `wa-batch-NNN.txt`
     dispatch files (LeadId, Business, Vertical, Website, Contact Mr./Mrs.+name,
     Phone, HtmlFiles, OutputFile).
   - `merge`: collects `wa-out-*.json`, enforces the contract (drops any draft
     missing salutation / containing a money word / em-dash / any `automatelb.com`
     or "Automate" mention / missing valid mobile), and writes `whatsapp-drafted.json`
     in the exact schema
     `bridge/send_campaign.js` already consumes (`to_jid`, `to_phone`, `body_text`,
     `lead_id`, `to_name`, `salutation`, `score`, `vertical`, `country_code`, `tags`,
     plus `workflow_gaps` / `how_we_help` for review).
   - Same cross-run dedup against `sent-log.md` (phone / slug / domain / email) that
     `draft_whatsapp.py` does.

6. **`project/.claude/commands/fire.md`** — add a **WhatsApp-only + custom branch**:
   - Detect `EMAIL_ENABLED` = `channels.json` contains `"email"` (default true when
     `channels.json` is absent, preserving every current run).
   - When `EMAIL_ENABLED=0`: skip Step 7 (email draft) and Step 8 (Brevo) entirely.
   - When `draft_mode=custom` AND WhatsApp enabled: in Step 8.5, route to
     `draft_whatsapp_custom.py` (prep -> `wa-writer` foreground fan-out, ≤50/msg,
     never `run_in_background` -> merge) instead of `draft_whatsapp.py`.
   - Bridge is started by `send_campaign.js` itself (reuses `.wwebjs_auth`, no QR).
   - Halt table extended: 0 surviving wa-writer drafts -> ABORT (kill-on-fallback).

7. **`runs/2026-06-22-lb-enterprise/`** template control files:
   - `icp.yaml` — biggest-LB-companies ICP, WhatsApp-only, consultant pitch.
   - `queries.txt` — DDG queries for biggest Lebanese companies/brands.
   - `countries.txt` = `LB`
   - `source_agent.txt` = `source-agent-lb-enterprise`
   - `channels.json` = `["whatsapp"]`
   - `draft_mode.txt` = `custom`
   - `vertical.txt` = `enterprise`
   - `brief.md` — the user's brief.

8. **`project/CLAUDE.md` router** — add to the Step-1 table:
   `"lebanese" / "lebanese run" / "biggest lebanese companies" -> lb-enterprise`.
   `lb-receptionist` keeps firing only on explicit "lb receptionist" / "receptionist
   lebanon". Bare "lebanon"/"lb" -> ASK which of the two (no silent default).

## Data flow

```
fire lebanese run
  -> bootstrap runs/<date>-lb-enterprise from template (clone control files)
  -> /fire <slug>
     Step 0-2  pre-flight, resolve source-agent-lb-enterprise, split queries
     Step 3    parallel source-agent-lb-enterprise fan-out (biggest LB companies)
     Step 4    merge + dedup vs sent-log.md
     Step 5    multi-page HTML fetch
     Step 6    extract leads
     Step 6.5  name-finder fan-out -> CEO direct mobile (LB +961) or DROP
     [Step 7/8 SKIPPED: email disabled]
     Step 8.5  draft_whatsapp_custom.py prep
               -> wa-writer fan-out (¶1 gaps, ¶2 help, message) per company
               -> merge -> whatsapp-drafted.json (contract-enforced)
               -> node bridge/send_campaign.js --send (auto-send, paced)
     Step 9    persist both-channel sent-log (wa:<phone> tokens)
     Step 10   report
```

## Risk / edge cases

- CEO mobile numbers for large companies are hard to find; expect a low survival
  rate after Stage 6.5. That is acceptable per kill-on-fallback (fewer, correct
  sends > misdirected blasts). Report the funnel honestly.
- Big companies may already use AI/chatbots — `wa-writer` should Skip if the gap is
  visibly already solved.
- WhatsApp pacing: keep `send_campaign.js` default delay; modest `WHATSAPP_MAX_PER_RUN`.

## Verification

- `fire lb receptionist run` still routes to `lb-receptionist` (regression).
- A GCC/US dry run still drafts + would-send email (regression: email-always-on
  preserved when `channels.json` has `"email"` or is absent).
- `lb-enterprise` dry run: sources, resolves a CEO mobile, produces a contract-valid
  custom WhatsApp message, and `send_campaign.js` dry-run prints it.
</content>
</invoke>
