# US custom gap emails — design

**Date:** 2026-06-14
**Status:** approved (pending written-spec review)
**Scope:** the US `draft_mode=custom` path only (us-law-firms, us-staffing, us-clinics, us-property). GCC / LB / worldwide template runs are untouched.

## Problem

US custom-draft emails are supposed to be unique per business. In practice they
converge to one identical skeleton: surface details (firm name, form fields)
change, but the gap, the pitch, and the CTA are verbatim-identical. Recipients
get what reads as the same generic "an AI that handles your inbox" email.

### Evidence (measured across the last 4 US runs)

| Shared phrase | law-firms 06-13 (n=32) | property 06-13 (n=145) | clinics 06-09 (n=27) | law-firms 06-08 (n=71) |
|---|---|---|---|---|
| CTA "Tuesday or Thursday … 15-minute call" | 100% | 99% | 96% | 100% |
| "Not a chatbot…" closer | 87% | 91% | 85% | 92% |
| "We set up an assistant that reads…" | 84% | 66% | 51% | 71% |
| "…repetitive part so your team/staff doesn't" | 71% | 62% | 70% | 71% |
| gap is "contact form" | 71% | 66% | 29% | 77% |

### Root causes

1. **The voice spec hands the agent a script to copy.** `voice-us.md:131-151`
   is a full example email; lines 92 and 94 give the literal "Not a chatbot…"
   closer and the exact CTA. Haiku is small — a concrete example *is* a template,
   and "never paste" does not override it.
2. **The gap collapses to the one signal every business shares: the contact
   form.** It is the most visible signal, so every agent picks it — even though
   `gap-writer.md:63` already bans it. Nothing enforces the ban, and the example
   itself is built on a contact-form gap.
3. **The merge anti-repetition gate is mis-calibrated.** `draft_custom.py:235-263`
   only drops emails with >0.85 *character-trigram* overlap (near-verbatim dupes).
   Two emails sharing an identical pitch skeleton but different firm names land
   well under 0.85, so they pass. The gate enforces *format*, never *distinct gap*.

## Goal

Every US email must attack a **distinct, business-specific gap tied to that
vertical's real workflow**, not the universal contact form. The pitch and CTA
must vary per business. If no firm-specific gap can be found, the lead is
**skipped**, not filled with a generic one (kill-on-fallback).

## Design: split "find the gap" from "write the email"

Today one Haiku agent does both and shortcuts to the contact form + a copied
example. Split it into two single-purpose stages.

### Stage 5.6 (NEW) — Gap enrichment

One `gap-finder` Haiku agent per business. Mirrors the existing `name-finder`
enrichment pattern (Stage 5.5). Its **only** job is intelligence — no email
writing.

- **Input:** lead + contact fields + absolute paths to that domain's scraped
  `raw_html` pages + an OutputFile path.
- **Behaviour:** reads 2-4 scraped pages (optional one WebFetch + one WebSearch
  for a hiring/news signal), then names the firm-specific workflow gap.
- **Output (`gap-out-NNN.json`):**

```json
{
  "lead_id": "...",
  "gap_type": "deadline-driven new-matter intake triage",
  "workflow_step": "new-matter intake before the consult is booked",
  "evidence": "one sentence: the real signal, with where on the site",
  "evidence_quote": "verbatim string copied from their page",
  "fill": "one sentence: how an assistant removes THAT step",
  "specificity_check": "would this be true of the firm next door? NO because ..."
}
```

  or `{ "skip": true, "reason": "..." }`.

- **Hard rule (the fix for cause 2):** the contact form / shared inbox /
  `info@`/`office@` / "we'll get back to you" / phone-only / no-online-booking
  signals are **valid as `evidence` but never as the `gap_type`.** `gap_type`
  must name the *downstream workflow consequence* for this vertical:
  - **law:** intake → conflict/deadline triage, consult scheduling back-and-forth,
    matter-type routing.
  - **staffing:** candidate submission → interview coordination, status updates,
    redeployment follow-ups.
  - **clinics:** new-patient intake → insurance verification, recall/reactivation,
    no-show chasing.
  - **property:** maintenance request → vendor dispatch, showing coordination,
    owner/tenant status.
  - `specificity_check` must explain why the gap is *not* true of the firm next
    door. If it can't, the agent returns `skip`.
- **Merged output:** `enrich_gap.py --phase merge` collects all `gap-out-*.json`
  into `gaps-enriched.json` (one record per surviving business; skips dropped).

### Stage 6 (REWORKED) — Template-edit draft

A base template lives in a file. It is a **skeleton, not boilerplate prose** —
it fixes only what should stay constant; the gap paragraphs are rewritten per
business from the enrichment record. The writer agent never sees a full example
email to copy.

**Base template** (`vault/lead-outreach/email-template-us.md`):

```
Subject: <3-7 words, lowercase, names THIS firm's gap, no "AI">

Hello <Mr./Mrs.> <Surname>,

<PARA 1 — EVIDENCE: open on the specific manual task, grounded in a verbatim
detail from their site (use evidence_quote). Name the workflow step. Must not be
true of the firm next door.>

<PARA 2 — FILL: how a quiet assistant removes THAT specific step, framed in their
workflow (not "reads your inbox"). Vary the verb and framing per business.>

<CTA — one ask, two loose time options, no calendar link. Phrase fresh; do not
reuse a stock sentence.>

David Geha
Automate, automatelb.com
```

| Part | Fixed or per-business |
|---|---|
| Salutation `Hello Mr./Mrs. <Surname>,` | Fixed pattern, variable name |
| Subject | Rewritten from the gap |
| Para 1 — manual task + verbatim evidence | Rewritten from enrichment |
| Para 2 — how the assistant removes *that* workflow step (the offer line) | **Rewritten** from enrichment (per user: both offer + CTA vary) |
| CTA | **Rewritten** per business |
| Sign-off `David Geha / Automate, automatelb.com` | Fixed |

- **`gap-writer` reworked:** input is lead + contact + that business's gap record
  (from Stage 5.6) + the base template + OutputFile. It does **not** re-derive
  the gap; it edits the template around the given record. Tools narrow to
  **Read, Write** only (it no longer searches — the gap-finder already did),
  which mechanically prevents it from re-deriving a generic gap.
- **`voice-us.md` reworked (the fix for cause 1):** remove the full mini-example
  email (131-151) and the verbatim stock lines (the "Not a chatbot…" closer in
  Beat 2 and the fixed CTA in Beat 3). Replace with principles + the template
  structure + an explicit "do not reuse any sentence across firms; the offer and
  CTA are rewritten per business" rule. Keep all the hard rules (salutation,
  no-money, link, no-em-dash, no-"AI"-in-subject, under 125 words).

### Merge gate (REWORKED) — `draft_custom.py`

Keep all format gates (salutation, money, link, dash, direct-email). Replace the
anti-repetition gate (the fix for cause 3):

1. **Word-5-gram similarity** instead of character-trigram. Compute word-level
   5-gram Jaccard over the body core (salutation + signature stripped). This is
   far more sensitive to shared *sentences* than char-trigrams. Drop a draft if
   it exceeds the threshold against any kept draft. Threshold calibrated during
   validation so no single pitch/CTA phrase survives in more than ~30% of emails.
2. **Lazy-gap drop:** drop any draft whose `gap`/`gap_type` is the bare contact
   form / shared inbox signal with no firm-specific noun.
3. **Stock-phrase report:** at merge, print the top repeated word-7-grams across
   surviving drafts (a visibility check so future regressions are obvious).

Keep the existing zero-drafts halt (`ABORT … exit 5`).

## Pipeline wiring

- **`.claude/commands/fire.md`:** insert Stage 5.6 between Stage 5.5 (contact
  enrichment merge) and Stage 6. The orchestrator dispatches `gap-finder` agents
  exactly like the other fan-outs — foreground, ≤50 Agent calls per message.
  Then Stage 6 reads `gaps-enriched.json`.
- **`project/PIPELINE.md`:** document the new stage in the stage-by-stage contract.
- **`CLAUDE.md` (both root and `project/`):** update the pipeline diagram and the
  "Draft modes" section to describe the gap-enrichment → template-edit split.

This doubles the draft-side fan-out (one gap-finder + one writer per business),
on top of the existing name-finder enrichment. Accepted by the user as the cost
of genuinely distinct gaps. Dispatch rules (foreground, ≤50/message) are unchanged.

## Files touched

| File | Change |
|---|---|
| `.claude/agents/gap-finder.md` | NEW — intelligence-only gap enrichment agent |
| `.claude/agents/gap-writer.md` | REWORK — consumes gap record + template; Read/Write only; no full example |
| `project/vault/lead-outreach/voice-us.md` | REWORK — remove example + stock lines; add structure + per-business offer/CTA rule |
| `project/vault/lead-outreach/email-template-us.md` | NEW — base template skeleton |
| `project/tools/scripts/enrich_gap.py` | NEW — prep/merge for gap enrichment |
| `project/tools/scripts/draft_custom.py` | REWORK — read gap records; word-5-gram + lazy-gap gate; stock-phrase report |
| `project/.claude/commands/fire.md` | REWORK — insert Stage 5.6 dispatch |
| `project/PIPELINE.md` | REWORK — document new stage |
| `CLAUDE.md` | REWORK — pipeline diagram + draft modes |
| `project/tests/` | NEW tests — enrich_gap merge; recalibrated draft_custom gate; lazy-gap drop |

## Validation (before any real send)

Reuse an existing run's `leads-with-contact.json` + `raw_html` (no new sourcing
or scraping). Run the new Stage 5.6 + reworked Stage 6 against it, then re-run the
repetition metrics from the evidence table. Success criteria:

- No email opens on "contact form"; `gap_type` distribution is diverse (no single
  gap_type > ~40%).
- CTA reuse and offer-line reuse each under ~30% (down from 87-100%).
- A manual read of 10 random emails: each attacks a recognisably different,
  workflow-specific gap.

## Halt conditions

- Stage 5.6: if fewer than a floor of businesses yield a concrete gap (e.g. < 40%
  produce a non-skip record), ABORT (kill-on-fallback) rather than ship generic.
- Stage 6 merge: zero surviving drafts → existing `ABORT … exit 5`.

## Out of scope

- GCC / LB / worldwide template runs (unchanged).
- Upgrading the writer off Haiku, or a self-critique rewrite loop (was option C;
  not chosen — revisit only if validation still shows convergence).
- Sourcing, scraping, name-finder enrichment, Brevo send, sent-log persistence.

## Open questions

None. Two-stage split and per-business offer + CTA both confirmed by the user.
