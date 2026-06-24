---
name: outreach-copywriting
description: Drafts a single professional cold email per qualified lead based on the gap analysis and the voice file from the Obsidian vault. Use after business-gap-analysis. Outputs one ready-to-send email — no follow-up sequence. Enforces the strict need → custom-system → differentiator → CTA structure defined in voice.md.
---

# Outreach Copywriting

Turn each gap analysis into **one** cold email that actually gets replied to. The pipeline is in **single-email mode**: no follow-ups, no bumps, no breakup messages. One shot per lead.

## Haiku-mode contract (mandatory)

This skill runs on Haiku 4.5. You write ONLY paragraph 1 of each email. Paragraphs 2-4, the subject, the signoff, and the compliance footer are pre-templated by Python (`compose_email.py` / `build_outreach_draft`). Your single output per lead is the paragraph 1 string.

### Output schema (strict)

For each lead, output ONE JSON object on a single line:

```json
{"lead_id": "maps-al-madar", "paragraph_1": "Al Madar's open roles point to manual coordinator work. For a logistics business in the UAE, that workload is exactly where a custom AI receptionist quietly takes weekly hours off the front desk."}
```

No prose around the JSON. No markdown. No paragraph 2-4. ONLY paragraph 1.

### Paragraph 1 hard rules (verify each before output)

1. First word MUST be: the company short name, the recipient's last name, or a concrete noun about their operation. NEVER `I`, `We`, `My`, `Your`, `Hi`, `Hello`, or `Hope`.
2. MUST cite the lead's specific `evidence` field from the gap JSON verbatim or near-verbatim.
3. MUST be 1-2 sentences. ≤ 60 words.
4. NO em-dashes (`—`) or en-dashes (`–`). Use periods, commas, or "to" for ranges.
5. NO banned phrases: `transform your business`, `AI revolution`, `leverage`, `synergy`, `circle back`, `cutting-edge`, `next-gen`, `disruptive`, `game-changing`, `10x`, `100x`.
6. NO `Dear`, `Hi`, `Hey`, `Hope this finds you well`, `Quick question`, `Quick chat`.
7. NO fabricated facts. Every claim must trace to `lead.signals` or `gap.evidence`.

### Worked examples per `primary_gap`

These are the canonical openers per signal type. Use them as anchors — vary the names and details, keep the structure.

**`primary_gap: mode_phone`**

GOOD: `Drs. Karim Dental Clinic's public booking path runs through phone only. For a clinic in Lebanon, that is exactly the kind of repeated intake work a custom AI system can take off the front desk.`

BAD (templated, no evidence): `I noticed your clinic could benefit from AI automation.`

**`primary_gap: mode_whatsapp`**

GOOD: `Al Salam Realty's customer path routes through WhatsApp. For a brokerage in Dubai, that usually means intake, follow-up, and handoffs are still sitting with staff.`

BAD (too long, lecturing): `Many businesses in the UAE use WhatsApp for customer contact, but this approach has several limitations that AI can address...`

**`primary_gap: mode_form`**

GOOD: `Almadar Logistics's contact-form workflow creates a manual back-and-forth after every inquiry. At your branch count, that compounds into hours of operations time every week.`

BAD (vague): `Forms can be slow. We can help speed things up.`

**`primary_gap: branches`**

GOOD: `Gulf Medical's multi-location footprint shows up clearly on the public site. Across branches in Saudi Arabia, reporting and intake handoffs tend to become manual work before leadership sees the cost.`

BAD (generic flattery): `Impressive growth across multiple locations! AI can help you scale even further.`

**`primary_gap: hiring_manual`**

GOOD: `Al Madar's open roles point to manual coordinator work. That is usually the cleanest first workflow for an owned AI system, with clear weekly time recovery to measure against.`

BAD (criticizing): `Hiring more humans isn't the answer. You need AI.`

**`primary_gap: ai_unshipped`**

GOOD: `Salam Realty's site mentions AI, but the customer operations layer still reads as a place where a shipped system could remove manual work. The gap is usually scoping, not capability.`

BAD (snarky): `You talk about AI but you don't use any.`

**`primary_gap: saas_no_ai`**

GOOD: `Al Madar already shows signs of a Zoho or Hubspot stack. The useful gap is usually the AI layer that turns those tools into end-to-end operational workflows.`

BAD (sales-y): `Your CRM is great, but you need our AI to unlock its full potential.`

**`primary_gap: pdf_menu`**

GOOD: `Drs. Karim's services are still exposed through static PDF documents. That usually mirrors manual internal handoffs and slow updates.`

BAD (assumption): `Your PDF menu is hurting your conversion rate.`

### Self-validation before writing the JSON output

Before emitting the JSON line, silently verify ALL of the following. If any check fails, rewrite paragraph 1 and re-check.

- [ ] First word is company name, last name, or concrete noun (NOT a pronoun or greeting)
- [ ] Paragraph references the EXACT signal from `gap.evidence`
- [ ] ≤ 60 words, 1-2 sentences
- [ ] No em-dash, no en-dash
- [ ] No banned phrases from rule 5/6
- [ ] No fabricated facts

If the gap JSON has `qualifies: false`, output `{"lead_id": "...", "paragraph_1": null, "skip_reason": "<gap.reason>"}` and move on. Do NOT draft.

The legacy long-form copywriting instructions below remain authoritative for the SUBJECT, paragraphs 2-4, P.S., and pre-flight checklist — but those are produced by Python (`compose_email.py`), not by Haiku. Read the rest of this file only for reference; do not produce that content.

## The most important rule

**Generic AI personalization tanks campaigns.** Saying "I noticed your company helps customers with [AI-generated description]" performs 4x worse than no personalization at all (71% poor rate vs ~18%).

What works: **value-focused personalization** that names a specific, observed gap and explains how Automate's custom AI system fills it.

Do this:
> "Your team's recent job posting for an Operations Coordinator references manual entry of purchase orders from email into Zoho. At your scale that workflow typically consumes 30 to 60 hours of operations time per week. Automate builds custom systems that solve exactly this..."

Not this:
> "I noticed Al Madar Logistics provides excellent freight services in the UAE and I wanted to reach out about how AI can transform your business..."

The first names a specific observation, quantifies the impact, and offers a concrete custom-built solution. The second describes the prospect back to them in AI-flavored prose. First wins; second loses every time.

## Inputs

- The gap analysis markdown file: `runs/<run-slug>/gaps/<lead-id>.md`
- The user's voice file from the vault: `vault/lead-outreach/voice.md` — **read this every time**
- The user's offer file: `vault/lead-outreach/offer.md`
- The user's compliance footer: `vault/lead-outreach/compliance.md`
- The lead record (recipient name, last name, title, email, company)

## Output

Append to `runs/<run-slug>/emails-drafted.json`:

```json
{
  "lead_id": "maps-al-madar-logistics",
  "to_email": "rami.khoury@almadar.ae",
  "to_name": "Rami Khoury",
  "to_salutation": "Mr. Khoury",
  "from_email": "<BREVO_SENDER_EMAIL>",
  "from_name": "<BREVO_SENDER_NAME>",
  "subject": "almadar.ae — automating manual order entry into Zoho",
  "body_text": "...",
  "body_html": "<p>...</p>"
}
```

**No `sequence` array.** Single-email mode. Each lead produces exactly one email object.

## Required body structure (enforced by pre-flight check)

```
[Salutation — "Mr. Khoury," or "Ms. Khoury," — NO "Dear", on its own line]

[Paragraph 1 — OBSERVATION. Lead with a specific fact about *their* company.
The first word is the company name, the recipient's last name, or a concrete
noun about their operation. Never starts with "I", "We", "My", "Your" + softener.
2 sentences max.]

[Paragraph 2 — WHO WE ARE + WHAT WE'D BUILD. The first sentence MUST explicitly
state that Automate is an AI consulting firm that designs custom AI systems.
The next sentence describes the specific AI system we'd build for this lead's
gap. Include: tailored to their existing tools, owned by them, ships in 2-3
weeks. End with: "Not a chatbot — a custom AI tool that quietly takes work
off your team." 3-4 sentences.]

[Paragraph 3 — DIFFERENTIATOR + RISK REVERSAL. Combined into one sentence.
"A fraction of what the global consulting firms charge" + "we walk if there
is no clear ROI in the first 20 minutes." 1 sentence.]

[Paragraph 4 — CTA. One short reversible ask. No double-CTA. No calendar link.]

Regards,
David
Automate — automatelb.com

[Optional P.S. — one sharp line, ≤25 words. A defensible name-drop, a sharp
question, or a concrete claim. Skip if there's nothing defensible to put.]

[Compliance footer auto-appended by brevo-send]
```

**Body word cap: 120. Optional P.S. word cap: 25. Total cap: 145 including P.S.**

## Salutation rules — formal, modern, NO "Dear"

| Lead has | Salutation |
|---|---|
| Last name + male presenting first name | `Mr. <last name>,` |
| Last name + female presenting first name | `Ms. <last name>,` (default) or `Mrs. <last name>,` if married/title known |
| Last name + ambiguous first name | `Ms. <last name>,` (default to Ms.) |
| Compound or particled last name (`Al Khoury`, `Bin Saleh`) | preserve full form: `Mr. Al Khoury,` |
| **Only a generic inbox**, no resolvable person | **SKIP THE LEAD** — do not draft. Mark `email_status: insufficient_seniority` and move on. |

Never `Hey`, `Hi`, `Dear`, `Dear Mr/Ms`, `Dear Sir/Madam`, `To whom it may concern`, or first-name-only. The word `Dear` is forbidden — it reads as templated and old-fashioned to modern C-suite buyers.

## Subject line rules — value-anchored or specific-signal

Two formats win. Pick the first if a defensible number is available; fall back to the second otherwise.

1. **Value-anchored** (preferred): `[Company] — [specific value back to them]`
   - `Al Madar — 30 hours per week back to your team`
   - `Gulf Medical — 4-day patient approval cycle, automated`

2. **Specific signal**: `[domain] — [specific process or signal]`
   - `almadar.ae — the purchase-order entry workflow`
   - `salam-realty.qa — the CRM reconciliation question`

Rules for both:
- 5–8 words, sentence case
- Reference one concrete signal (domain, company, specific process, specific number)
- No `[brackets]`, no `!`, no ALL CAPS, no emoji
- Never "Quick question" / "Quick chat" — both rejected by inbox filters as templated

Examples that lose:
- `Partnership opportunity for your business` (generic)
- `[IMPORTANT] AI Solutions for Al Madar` (spammy)
- `Hi Rami!` (subject reads like a casual chat, breaks formal salutation rule)

## Plain-language enforcement

The voice file requires plain language. Reject any draft that contains industry shorthand a smart outsider wouldn't grok. Common offenders and substitutions:

| Reject | Use instead |
|---|---|
| freight forwarder | logistics company / shipping company |
| RPA, RPA bots | automation / automated workflow |
| RFx, RFP, RFQ | tender / quote request |
| OKR, KPI | targets / key results |
| EBITDA, runway, burn | describe plainly: "annual operating costs", "monthly costs vs. revenue" |
| churn | customer drop-off / cancellations |
| onboarding | new-customer setup |
| ticket, case (support) | customer request |

The full substitution table lives in `voice.md` under "Plain-language substitutions."

## CTA rules — one ask, reversible, low-friction

Pick ONE:

- "Worth a brief call this week?"
- "Would Tuesday or Wednesday afternoon work for 20 minutes?"
- "Is this worth 20 minutes of your time this week?"

Never combine two CTAs. The "should I send a one-page summary" wording is removed — it splits focus and lowers conversion.

Calendar links and Calendly URLs do NOT go in cold emails — they trigger spam filters and signal "templated outreach." The calendar link goes in the reply once they say yes.

## P.S. — optional second hook

The P.S. is the second-most-read line after the subject. Use it for **one** of:

1. **Defensible name-drop** (only if present in `offer.md`):
   `P.S. We've built similar systems for [industry] in [GCC country] — the technical work is straightforward, the value is in scoping it tight.`

2. **Sharp question** (when the gap might not be the right one):
   `P.S. If purchase-order entry isn't the priority, customs paperwork usually is — happy to start there instead.`

3. **Result claim** (only if defensible):
   `P.S. Most first builds we ship pay for themselves in operations time within 90 days.`

Skip the P.S. entirely if there is no defensible content. Generic P.S. ("Looking forward to hearing back!") is worse than none.

P.S. word cap: 25.

## Voice and offer

Read `vault/lead-outreach/voice.md` for tone, phrasing rules, banned words, and salutation requirements. Read `vault/lead-outreach/offer.md` for the exact services Automate delivers — never invent capabilities. Read `vault/lead-outreach/target-profile.md` for the standing audience context.

## Sender identity

Always sign as the person/business in the user's voice file. Defaults from current vault config:

- `from_name`: `David`
- `from_email`: `BREVO_SENDER_EMAIL` (set in `.env`)
- Sign-off: `Regards, / David / Automate — automatelb.com`

## Spam-trigger filter (deliverability)

Reject any draft (subject OR body OR P.S.) that contains any of:

| Category | Patterns |
|---|---|
| Money / urgency | `free`, `guaranteed`, `act now`, `limited time`, `risk-free`, `100%`, `no obligation`, `winner`, `congratulations`, multiple `$`, `cash`, `bonus`, `prize`, `claim now` |
| AI-spam triggers | `transform your business`, `AI revolution`, `unlock potential`, `next-gen`, `cutting-edge`, `paradigm shift`, `10x`, `100x`, `disruptive`, `game-changing` |
| Templated openers | `I hope this email finds you well`, `I wanted to reach out`, `Quick question`, `Quick chat`, `Hi there`, `To whom it may concern`, `Dear Sir/Madam`, `Dear Mr` (the word `Dear` is forbidden anywhere in the email) |
| Subject-line spam | ALL CAPS in any word ≥ 3 chars, multiple `!`, `[Important]`, `[Action Required]`, `[Urgent]`, emoji, `Re:` or `Fwd:` prefix on first contact |
| Format triggers | More than 1 link in body (compliance footer counts as separate, allowed), any image / inline image, any attachment, any URL shortener (`bit.ly`, `tinyurl.com`, `t.co`, `lnkd.in`, `goo.gl`), tracking pixel `<img>` tags |

If any trigger fires, regenerate the affected section. Don't ship.

## Pre-flight checklist before output

For every drafted email, verify:

- [ ] Salutation is `Mr.` / `Ms.` / `Mrs.` + last name + comma + linebreak — **NO `Dear`**, no first-name-only, no `Hi`/`Hey`
- [ ] Subject is value-anchored OR specific-signal, 5–8 words, no brackets
- [ ] Paragraph 1 starts with the company name, recipient's last name, or a concrete noun about their business — NEVER with `I`, `We`, `My`, or `Your team's`
- [ ] Body follows the four-paragraph structure: observation → what we'd build → differentiator + risk reversal → CTA
- [ ] Body word count ≤ 120 (excluding P.S.)
- [ ] If P.S. present, it has defensible content and is ≤ 25 words
- [ ] Total length including P.S. ≤ 145 words
- [ ] Paragraph 2 first sentence explicitly states Automate is an AI consulting firm that designs custom AI systems
- [ ] Includes "custom AI system" (the words "AI" and "system" together) at least once
- [ ] Includes the line "Not a chatbot — a custom AI tool that quietly takes work off your team."
- [ ] Includes ownership phrasing ("owned by Company") once
- [ ] Includes the big-firm-alternative + risk-reversal sentence
- [ ] CTA is exactly one of the three approved wordings
- [ ] No calendar link, no Calendly URL
- [ ] No banned words from voice.md ("Dear", "transform", "revolution", "leverage", "circle back", "synergy", "hey", etc.)
- [ ] **NO em-dashes (`—`) or en-dashes (`–`) anywhere** — top AI-text tell. Use periods, commas, colons, parens, or "to" for ranges. Reject any draft containing `—` or `–` in subject, body, or P.S.
- [ ] No industry jargon — every sentence readable by a smart outsider
- [ ] No fabricated facts about the lead — every claim came from the gap analysis
- [ ] No fabricated case studies — references must come from `offer.md`
- [ ] Sign-off is exactly: `Regards, / David / Automate — automatelb.com`
- [ ] No `sequence` array — single-email mode

If any check fails, fix and re-verify. Never ship a draft that fails the checklist.

## What this skill does NOT do anymore

The pipeline used to draft a 3- or 4-step sequence (initial + 2-3 follow-ups). **That feature is removed.** This skill produces exactly one email per qualified lead. The orchestrator no longer schedules, queues, or sends follow-ups, and the bridge does not surface follow-up state. If a recipient does not reply, the lead is marked complete after the single send.

This is a deliberate product choice from the user — single-shot, professional, no nudging.
