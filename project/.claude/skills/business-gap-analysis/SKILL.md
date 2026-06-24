---
name: business-gap-analysis
description: Performs deep research on a qualified lead to identify and document the specific business gap to pitch. Visits their website, checks social media, reads recent reviews, and produces a concrete, evidence-based gap report. Use only on leads that scored ≥70 in lead-scoring (gap analysis is expensive — don't waste it on disqualified leads).
---

# Business Gap Analysis

Turn each qualified lead into a concrete pitch angle. The output of this skill drives email personalization — generic personalization converts 4x worse than no personalization at all, so the analysis here directly determines reply rates.

## Haiku-mode contract (mandatory)

This skill runs on Haiku 4.5. You do NOT do open-ended research. You pick from a closed set of pre-classified signals, and you produce a strict JSON output. No essays, no opinions, no markdown narratives.

### Hard rules
1. The signals JSON at `runs/<slug>/signals/<lead-id>.json` is the ONLY input. Do NOT crawl, do NOT websearch, do NOT use webfetch. Python already did the crawling.
2. The Python signal-classifier (`classify_signal` in run_campaign.py) already populated `lead.signals` with one or more of: `mode` (booking_widget|contact_form|phone|whatsapp), `ai_mentioned`, `saas`, `branches`, `hiring_manual`, `pdf_menu`. Your job is to PICK THE BEST and write a structured pitch object.
3. Output is JSON only. The exact schema is below. No prose. No `# headings`. No "I think" or "It seems."
4. If no signal in the lead's record is concrete (all fields empty/null/false), output `{"qualifies": false, "reason": "no_concrete_signal"}` and stop. Do NOT invent a gap.

### Closed-set signal selection table

For each lead, the input has `lead.signals` like `{"mode": "phone", "hiring_manual": true}`. Pick ONE primary signal in this priority order:

| Priority | Signal key | Trigger | primary_gap label |
|---|---|---|---|
| 1 | `hiring_manual` (when true) | They're hiring receptionists/coordinators/data-entry → manual ops gap | `hiring_manual` |
| 2 | `branches` (when ≥3) | Multi-location → coordination overhead | `branches` |
| 3 | `mode` (when `phone` or `whatsapp`) | Customer path is phone/WhatsApp → intake bottleneck | `mode_phone` or `mode_whatsapp` |
| 4 | `mode` (when `contact_form`) | Static intake → manual response | `mode_form` |
| 5 | `pdf_menu` (when true) | Static documents drive customer info | `pdf_menu` |
| 6 | `ai_mentioned` (when true) | They talk about AI but ship none | `ai_unshipped` |
| 7 | `saas` (when true) | Has Hubspot/Salesforce/Zoho but no AI layer | `saas_no_ai` |

If multiple apply, pick the highest-priority one. Tie-break: if score_breakdown.gap_signal already named a primary, keep that.

### Required JSON output

Write to `runs/<slug>/gaps/<lead-id>.json` (NOT markdown — JSON):

```json
{
  "lead_id": "maps-drs-karim-dental",
  "qualifies": true,
  "primary_gap": "mode_phone",
  "evidence": "lead.signals.mode == 'phone' and lead.has_contact_form == false",
  "pitch_angle": "Their customer intake runs through phone only. A custom AI receptionist captures after-hours leads.",
  "do_not_say": "Don't critique their website design — only flag the intake mode.",
  "severity": "high"
}
```

Field rules:
- `primary_gap` MUST be one of the labels in the table above. No new labels.
- `evidence` MUST cite the specific field+value from `lead.signals`. No vague claims.
- `pitch_angle` MUST be 1-2 sentences. No paragraphs. No marketing language.
- `do_not_say` MUST be 1 sentence. What to AVOID, not what to do.
- `severity` MUST be `critical|high|medium`. Never `low` (low = drop the lead via `qualifies: false`).

### Worked example A — hiring signal

Input lead record:
```json
{
  "id": "maps-al-madar",
  "name": "Al Madar Logistics",
  "signals": {"mode": "phone", "hiring_manual": true, "saas": true}
}
```

Correct output:
```json
{
  "lead_id": "maps-al-madar",
  "qualifies": true,
  "primary_gap": "hiring_manual",
  "evidence": "lead.signals.hiring_manual == true",
  "pitch_angle": "Their open roles point to manual coordinator work. A custom AI workflow takes that load off the front desk.",
  "do_not_say": "Don't pitch SaaS replacement — they already use a stack and don't need another tool.",
  "severity": "high"
}
```

### Worked example B — no concrete signal

Input lead record:
```json
{
  "id": "maps-empty",
  "name": "Some Company",
  "signals": {"emails": ["info@some.ae"]}
}
```

Correct output:
```json
{
  "lead_id": "maps-empty",
  "qualifies": false,
  "reason": "no_concrete_signal"
}
```

(The `emails` field doesn't count as a real signal. Only the keys in the table count.)

### What Haiku must NOT do
- Do not fetch URLs. The crawl already happened in Python.
- Do not write a long markdown report. JSON only.
- Do not interpret "between the lines" — if the signal field is empty, the gap is absent. Drop the lead.
- Do not blend signals into a paragraph ("Their phone-only intake combined with hiring activity suggests..."). Pick ONE primary_gap.

The legacy markdown report format described in the rest of this file is deprecated. Only the JSON contract above is current.

## Input

A single lead from `leads-scored.json` with `qualifies: true`. Note the `primary_gap` from scoring — that's your starting hypothesis, but verify it against fresh research.

## Output

One markdown file per lead at `runs/<run-slug>/gaps/<lead-id>.md`:

```markdown
---
lead_id: maps-drs-karim-dental
name: Drs. Karim Dental Clinic
score: 92
primary_gap: bad_website
gap_severity: high
pitch_angle: "Convert their static brochure site into a booking-enabled clinic site"
specific_observations:
  - "Website (drkarim.com) last updated 2019 per footer copyright"
  - "No online booking — patients must call"
  - "Site is not mobile-responsive (tested via DOM render)"
  - "4.7★ with 87 reviews on Google but no reviews shown on website"
estimated_value:
  - "Booking widget could capture ~30% of after-hours leads currently lost to voicemail"
  - "Mobile responsiveness — 60%+ of dental searches are mobile"
proof_links:
  - "https://drkarim.com (homepage screenshot)"
  - "https://www.google.com/maps/place/... (Google reviews)"
do_not_say:
  - "Don't claim their website 'looks bad' — it's clean, just outdated functionally"
  - "Don't pitch SEO — they already rank #1 for 'dentist Beirut Hamra'"
---

# Drs. Karim Dental Clinic — Gap Analysis

## What they do well
- Strong reputation: 4.7★ with 87 Google reviews
- Long-running practice (founded 2008 per About page)
- Already ranks #1 in their neighborhood for branded queries

## Where the gap is
The website is a static brochure built around 2019. Phone is the only contact mechanism. Mobile rendering breaks below 480px. The contact form on /contact returns a Mailto: link, not a server-side form.

The Google Business Profile is well-managed (recent photos, replies to reviews) — they clearly care about online presence, just don't have a modern web partner. This is high-intent.

## Pitch angle
"You already rank #1 in Hamra and have 87 five-star reviews. The piece holding back online bookings is just the website — let's add a booking widget and make it mobile-friendly. I built one for [similar clinic] last month, happy to show you."

## What NOT to say
- Don't critique the design — they likely picked it themselves and are proud of it
- Don't mention SEO — they're already winning there
- Don't mention social media — they don't have an Instagram presence and probably never will
```

## Research procedure

For each lead, do these in order, stopping early if you have enough evidence:

### 1. Visit their website (if any) — TOKEN-CHEAP MODE

**Do NOT pull full crawl4ai markdown into context.** A homepage in markdown is typically 5-20KB. With 25 qualified leads, that's 250KB+ of pure context bloat. Instead, use the signal extractor which returns ~500-1500 bytes per page with everything you need for gap identification.

For a single lead:

```bash
./tools/run.sh tools/scripts/extract_signals.py \
  --url "<lead.url>" \
  --output runs/<run-slug>/signals/<lead-id>.json
```

For a batch (preferred — runs concurrent crawl4ai sessions):

```bash
# build the URL list once
jq -r 'select(.qualifies==true) | {url, id} | @json' \
  runs/<run-slug>/leads-scored.json \
  > runs/<run-slug>/qualified-urls.jsonl

./tools/run.sh tools/scripts/extract_signals.py \
  --batch runs/<run-slug>/qualified-urls.jsonl \
  --out-dir runs/<run-slug>/signals/ \
  --concurrency 3
```

The script returns a structured JSON per lead containing only:

- `title`, `meta_description`, `language`, `ssl`
- `headings.h1`, `headings.h2`, `headings.h3` (the structural skeleton, capped at 8 each)
- `emails` (quality-ranked, same-domain only)
- `phones`, `social_links` (instagram/facebook/linkedin/twitter/tiktok/youtube)
- `copyright_year` — the staleness signal
- `tech_stack` — `["wordpress", "shopify", "wix", "squarespace", "webflow", "framer", "next.js", "react"]`
- `tracking_pixels` — `["google_analytics", "facebook_pixel", "hotjar", "intercom"]`
- `has_booking_widget`, `has_contact_form`, `has_mobile_viewport` — booleans
- `word_count` — under 200 = parking page / under construction
- `about_excerpt` — first 240 chars of any "about" / "who we are" / "our story" section

This is everything you need to decide:

- Is the site stale? → `copyright_year`
- Mobile-friendly? → `has_mobile_viewport`
- Booking-enabled? → `has_booking_widget`
- Sophisticated marketing? → `tracking_pixels` length
- Tech stack? → `tech_stack`
- Active social? → `social_links` (then check those handles separately)

Read **only the signals JSON** into context, never the full page. If a specific lead genuinely needs a deeper read, pass `--save-full-markdown` and pull the saved `.md` on demand for that one lead.

### 2. Check their primary social channel

For local businesses → Google Business Profile (already partly captured by Maps sourcing)
For other targets → fetch the social-channel link off their website via crawl4ai and read public-facing content only.

Direct Instagram and LinkedIn scraping is disabled in this stack. If a lead's only signal lives on those platforms, mark `gap_signal: insufficient` and skip — don't try to log in or use removed skills.

You don't need full scrape — a quick fetch via web search is enough to count posts in last 90 days, follower count, response patterns.

### 3. Sample recent reviews

For Maps leads, look at the 3 most recent reviews. Are they responded to? Are there complaints that the business could be addressing? Patterns in reviews are gold for pitching.

### 4. Identify the strongest gap

Even when scoring picked a `primary_gap`, the research might surface a different/bigger gap. Override `primary_gap` if needed and note why.

### 5. Find a "do not say"

This is critical. Identify 1-2 things the email should AVOID — areas where the lead is already strong, sensitive topics, or things they've clearly chosen on purpose. Skipping this step produces emails that insult the prospect's existing work.

## Severity scale

- **Critical**: gap is so severe the business is losing customers daily (e.g., no website at all, broken contact info, abandoned social with bad reviews)
- **High**: gap is real and the fix has clear ROI (e.g., outdated site, no booking, weak SEO with proven demand)
- **Medium**: gap exists but isn't urgent (e.g., site is fine but social is weak; reviews are good but few)
- **Low**: gap is theoretical — skip this lead. If gap_severity is low, set `qualifies: false` in the gap file and the orchestrator will skip outreach.

## Cost & time

Sourcing tools are free (crawl4ai is local). The token cost lives in what you read into context. Using `extract_signals.py` instead of raw markdown drops per-lead context cost by ~10x (from ~10KB → ~1KB per page). Hard cap: process at most `MAX_EMAILS_PER_RUN` leads (default 30). For runs where >30 leads qualify, process the top 30 by score.

## Failure modes

- Website returns 5xx → that's information. Note "site is broken/down" as a critical-severity gap and proceed.
- All "gaps" you'd flag are subjective opinion (no concrete evidence) → write `qualifies: false` and `reason: insufficient_objective_gap` in the file. Don't pitch on "I think your site could look nicer" — that's exactly the generic personalization that fails.
- Lead has no public web footprint at all → genuinely impressive in 2026 but unreachable; mark `qualifies: false` with `reason: unreachable`.
