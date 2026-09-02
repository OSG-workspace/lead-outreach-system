# Outreach Targeting Config

> Reference file for the cold outreach workflow. Defines WHO to target, WHERE, and HOW.
> Offer (2026-08-26): maintenance triage + vendor dispatch agent (runner-up:
> AP invoice-coding + owner reporting). FIXED copy: `pitch.json`.
> **Vertical qualification scope (read before qualifying leads):**
> `vault/lead-outreach/targeting/us-property-scope.md` — regions, sub-segments,
> signals checklist, tech-stack markers, offer angle for THIS vertical.
> Cross-vertical context: `vault/lead-outreach/targeting/us-playbook-2026.md`.
> Last updated: 2026-08-26

---

## 1. Region

**Target country: United States (primary).**

Rules:
- US only for the first campaigns. Most permissive market for B2B cold email (CAN-SPAM: no prior consent required).
- Every email MUST include: real sender name + info, a physical mailing address, an honest subject line, a working unsubscribe/opt-out.
- Do NOT target Germany (strictest GDPR enforcement on cold email).
- Secondary markets to expand into later, in order: UK, Canada (CASL — needs implied/express consent), Australia, Netherlands.

Timezone for send times and meeting slots: default to **ET** unless the prospect's location is known.

---

## 2. Target verticals (pick ONE per campaign)

Only target verticals that BOTH reply to cold email AND pay for automation. Ranked best-first per the 2026 playbook (whitespace x pain x feasibility x demoability, see us-playbook-2026.md):

| Rank | Vertical | The money leak | Agent to pitch |
|------|----------|----------------|----------------|
| 1 | **Healthcare staffing** | $6-8K/month lost per provider stuck in credentialing; half of contractors never redeployed | Credential monitoring (+ redeployment) |
| 2 | **Med spa / dental groups** | Half of high-ticket consults never convert; 48-hour dead zone after booking | Consult-conversion nurture (+ recall) |
| 3 | **Law firms** | 12-20% of billable revenue never invoiced (hourly firms); 1,000+ page records read by hand (PI) | Time-capture / billing leakage (PI: records) |
| 4 | **Property management** | 40% of the week on maintenance coordination; ~$10/invoice manual AP | Maintenance triage (+ AP / owner reporting) |

**DO NOT TARGET:** SaaS, tech, software (worst reply rates, saturated inboxes, skeptical buyers). Also avoid financial services.
**DO NOT PITCH (any vertical):** receptionist / intake / missed calls / leasing AI / candidate engagement. Those front-office niches are saturated for medium/large targets and mark the sender as uninformed, see each scope file's "Do NOT pitch" section.

---

## 3. Target person (decision-maker)

Email the person who personally feels the vertical's money leak (see the scope file), not a generic role:

- **Law firms:** managing partner, COO/executive director, or billing manager. 10-200 attorneys hourly-billing (PI records pitch: 5-50 attorneys).
- **Staffing:** agency owner, COO, or compliance/credentialing lead. $7.5M-$100M revenue, 20-150 internal staff, healthcare niche first.
- **Med spa / dental:** owner, COO, or director of operations. 2-20 location groups (dental: 5-100 locations).
- **Property mgmt:** owner, COO, or director of property management. Third-party managers, 1,000-50,000 units.

---

## 4. Firmographic filters (qualify before adding to list)

Do NOT add a contact just because the job title matches. Layer these filters:

- **Company size** in the range above for the vertical.
- **Buying signals (prioritize these):**
  - Actively hiring → overloaded, feels the pain
  - Multiple locations → coordination pain
  - Recently raised / expanded / opened a new office
- **Data quality:** email must be verified. Keep list bounce rate under 4%. Re-verify any contact older than 90 days.

---

## 5. Email rules (hard constraints)

**Length & structure**
- Under 125 words total. Ideal 50–125. Never exceed 150 (reply rate halves).
- One personalized first line (real, specific — see below).
- One clear value proposition (one sentence).
- ONE call to action only.
- Offer two specific meeting times, NOT a calendar link.
- Don't sell. State the pain + the fix, ask for a short yes.

**Personalization (first line)**
- Must reference a real, specific trigger tied to the gap found for that business (see section 6). "I see you work at [Company]" is banned (that's a mail merge).
- Good data sources, best-first: job postings, podcast appearances, case-study/news patterns, tech-stack changes.
- Avoid LinkedIn-post references (everyone scrapes them now — noisy, looks automated).

**Deliverability (do not skip)**
- Send from DEDICATED domains, never the main domain. Buy 2–3 secondary domains, warm them ~2 weeks before sending.
- NO links in the first email body (flagged as promotional, hurts deliverability). Exception: the user-approved fixed signature link (osgdev.com) in pitch.json. Other links only in follow-ups.
- A/B test 2–3 subject lines per campaign. Subject 3–7 words, lowercase, no salesy words, no "AI" buzzword.

**Sequence & volume**
- 4–7 emails per sequence (4 is a solid default).
- Send in SMALL batches: under 50 recipients per batch. Small + sharp beats high-volume blasts (5.8% vs 2.1% reply rate).
- Measure reply rate against a 5–10% benchmark. Below 3% = problem is relevance/targeting, not volume.

---

## 6. Gap-finding (core step — do this per business before writing)

> **2026-08-26: the US campaigns run draft_mode=template.** The email copy is
> FIXED per `pitch.json` in this folder (us-law-firms auto-routes its PI
> variant on the firm's own pages). This section applies ONLY if a fully
> custom run is explicitly requested, and the gap to research is then THIS
> vertical's money leak as defined in the scope file above (billing leakage /
> credentialing / consult conversion / maintenance+AP), never the retired
> inbox/scheduling angle.

For each business, research it first, identify the specific instance of the vertical's gap, then write the email around THAT gap and how an AI agent could fill it.

For each prospect, find and name:
1. **The gap** — a specific repetitive, manual email/scheduling task this business is doing by hand. Examples of what to look for: client/lead intake emails handled manually, consult or appointment booking done by back-and-forth, follow-up sequences sent one by one, quote/estimate replies, multi-timezone scheduling, reminder/no-show chasing, candidate or patient communication.
2. **The evidence** — the real signal that revealed the gap (job posting for an admin/coordinator/front-desk role, a shared inbox on their contact page, no online booking, a "we'll get back to you" form, growth/expansion news, etc.). This becomes the personalized first line.
3. **The fill** — how an AI consultant could remove that specific task with an inbox/scheduling automation (auto-triage incoming emails, auto-book into calendar, auto-send follow-ups in the background). Frame it as removing their repetitive work, not as "AI."

The email is then written FROM these three things — gap, evidence, fill — unique to that business. Do not reuse phrasing across prospects.

**Quality checklist for every generated email:** names a real gap specific to THAT business · opens with the real evidence/signal (not a mail-merge line) · states one fill/value prop · one CTA with two concrete time options · under 125 words · no link in first email · no "AI" in the subject · opt-out + physical address included.

---

## 7. Per-campaign workflow

1. Pick ONE vertical (default: law firms).
2. Confirm 2–3 warmed sending domains are ready.
3. Build a list of 50–100 of the exact right person, verified emails.
4. For EACH business: run gap-finding (section 6), then write a custom email + follow-ups around that gap. Generate 3 subject-line variants.
5. Send in batches under 50. Measure reply rate. Iterate on targeting and the gap/fill angle.
