# lb-enterprise WhatsApp custom-consultant run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire a new WhatsApp-only pipeline that messages the biggest Lebanese companies with a fully custom, per-company AI-consultant pitch, fired by "fire lebanese run", without touching any existing run.

**Architecture:** Additive clone of the existing custom-draft (`gap-writer`) + WhatsApp (`draft_whatsapp.py` + `send_campaign.js`) patterns. A new sourcing agent, a new WhatsApp writer agent, a new voice spec, a new `draft_whatsapp_custom.py`, a WhatsApp-only/custom branch in `fire.md`, a run-folder template, and a router line in `CLAUDE.md`. Existing runs are untouched because they keep `"email"` in `channels.json` and their own `source_agent.txt`/`draft_mode.txt`.

**Tech Stack:** Python 3 (stdlib only), Markdown agent/voice specs, Node `whatsapp-web.js` bridge (unchanged), `/fire` orchestrator playbook.

## Global Constraints

- Repo root: `<repo-root>`. Scripts run from `project/` cwd; `python3 tools/scripts/...`.
- New sourcing agent country tag is always `LB`.
- Message identity: `David Geha, a third-year engineering student at AUB`. NOT "from Automate".
- Social proof line: `I work with a team building custom AI systems for clients across Lebanon, the GCC, and India.` Never "small team".
- Salutation contract: body starts with `Hello Mr. <Surname>,` or `Hello Mrs. <Surname>,`.
- No money words anywhere (no price/fee/commission/"free"/"$"/retainer/cost).
- No em-dashes or en-dashes (`—`, `–`) anywhere; use commas / "to".
- **No `automatelb.com` link and no "Automate" mention** (this run is the exception). Signature is exactly `David Geha`.
- CEO/decision-maker direct mobile REQUIRED; company dropped if unresolved (kill-on-fallback). No generic/company numbers.
- WhatsApp-only: this run sends NO email.
- Fan-out dispatch is FOREGROUND, ≤50 Agent calls per assistant message, never `run_in_background`.

---

### Task 1: WhatsApp consultant voice spec

**Files:**
- Create: `project/vault/lead-outreach/voice-lb-wa.md`

**Interfaces:**
- Produces: the canonical voice spec the `wa-writer` agent reads first (Task 3 consumes it by absolute path `<repo-root>/project/vault/lead-outreach/voice-lb-wa.md`).

- [ ] **Step 1: Write the voice spec file**

Create `project/vault/lead-outreach/voice-lb-wa.md` with this content:

```markdown
---
type: config
title: "Voice (LB enterprise WhatsApp) — custom consultant pitch"
status: current
created: 2026-06-22
tags: [lead-outreach, config, voice, lb, whatsapp]
related: ["[[voice]]", "[[voice-us]]"]
---

# Voice (LB enterprise, WhatsApp) — canonical spec for the `wa-writer` agent

Single source of truth for the lb-enterprise WhatsApp run. The `wa-writer` Haiku
agent reads THIS file before writing. There is no template; every message is built
from scratch for one specific company, on the gap that agent finds.

## Who is sending (identity — locked)
- `David Geha, a third-year engineering student at AUB`. Do NOT say "from Automate".
- Social proof: `I work with a team building custom AI systems for clients across
  Lebanon, the GCC, and India.` Never "small team".
- **No `automatelb.com`, no "Automate" anywhere.** Signature is exactly `David Geha`.

## The two analysis paragraphs you produce first (then form the message from them)
1. `workflow_gaps` — 4 to 5 sentences naming this company's specific manual workflow
   and repetitive tasks (intake, customer questions across phone/WhatsApp/Instagram,
   order status, clienteling, back-office reconciliation, manual reporting). Prove
   you looked at THIS company, not the category.
2. `how_we_help` — 4 to 5 sentences on how David Geha and the team, as AI builders,
   would map those flows and build a custom AI layer on the tools they already use
   (not a chatbot), what the first one or two systems would do, and that they own
   the system with no platform lock-in. No money talk.

## The WhatsApp message (what is actually sent — locked structure)
```
Hello Mr./Mrs. <Surname>,

<1 to 2 sentences: this company's specific manual workflow / repetitive-task gap,
 proving you looked>

I'm David Geha, a third-year engineering student at AUB. I work with a team building
custom AI systems for clients across Lebanon, the GCC, and India, and we'd take that
kind of repetitive <intake/follow-up/...> off your team so they get their time back
and customers get instant answers around the clock. You own the system, no platform
lock-in.

Worth a short call this week to show you what it would look like for <Company>?

David Geha
```

## Hard rules (re-check before Write)
- Salutation exactly `Hello Mr. <Surname>,` or `Hello Mrs. <Surname>,` on line 1.
- First gap line names something real about THIS company (test: "would it read
  identically for a competitor?" if yes, Skip).
- Zero money words: no price, fee, retainer, commission, cost, "free", "$".
- Zero em/en dashes. Use commas or "to".
- No `automatelb.com`, no "Automate" string anywhere.
- Signature is exactly `David Geha` (last line). No title, no link, no phone.
- Under ~110 words in the sent message. Plain, human, short sentences. No markdown.
- Banned words: transform, revolution, unlock, leverage, synergy, streamline,
  cutting-edge, seamless, robust, elevate, empower, 10x, game-changer.

## Skip when
The company already markets an AI assistant / chatbot that closes the gap, or you
cannot name a concrete company-specific gap from its pages or one web search.
```

- [ ] **Step 2: Verify the file exists and forbids the link**

Run: `grep -c "automatelb.com" project/vault/lead-outreach/voice-lb-wa.md`
Expected: prints a number ≥ 2 (the rule mentions it only to FORBID it) and the file exists.

- [ ] **Step 3: Commit**

```bash
git add project/vault/lead-outreach/voice-lb-wa.md
git commit -m "feat(lb-enterprise): add WhatsApp consultant voice spec"
```

---

### Task 2: Sourcing agent for biggest Lebanese companies

**Files:**
- Create: `.claude/agents/source-agent-lb-enterprise.md`

**Interfaces:**
- Produces: a subagent named `source-agent-lb-enterprise` (tools: WebSearch, Write) that writes pipe-delimited `domain|BusinessName|LB|vertical|branches` to its OutputFile. Task 6's `source_agent.txt` selects it; `fire.md` (Task 5) dispatches it.

- [ ] **Step 1: Write the agent file** (clone of `source-agent-lb.md`, retargeted)

Create `.claude/agents/source-agent-lb-enterprise.md`:

```markdown
---
name: source-agent-lb-enterprise
description: Source the BIGGEST Lebanese companies and brands for ONE DuckDuckGo query (couture/fashion houses, luxury retail groups, banks, FMCG/industrial groups, conglomerates, hospitality groups). Targets large enterprises with real corporate websites for a custom-AI consulting pitch over WhatsApp. Uses WebSearch only, writes pipe-delimited results. Dispatched in parallel (one per query) by the /fire orchestrator for the lb-enterprise run. Never uses Bash, Python, ddgs, crawl4ai — only WebSearch + Write.
model: haiku
tools: WebSearch, Write
---

# Source Agent (Lebanon — Enterprise) — Single-Query Sourcing

You source the LARGEST, most prominent Lebanese companies for a custom-AI consulting
pitch sent over WhatsApp. The orchestrator gave you exactly one DuckDuckGo query and
an output file path. Run WebSearch, extract big Lebanon-based companies, write them
pipe-delimited to the output file.

## What we are selling (so you can judge fit)
A consultant (David Geha, an AUB engineering student, and his team) who builds custom
AI systems that remove a company's repetitive workflows (customer questions, intake,
order status, clienteling, reporting). Best fit: large, well-known Lebanese companies
and brands with a real operation and enough manual workflow to automate.

We ARE looking for big names: couture / fashion houses (e.g. Elie Saab), luxury and
department retail (e.g. Aishti), banks, FMCG / industrial / trading groups,
conglomerates, large hospitality / F&B groups, telecoms, large real-estate developers.

We are NOT looking for tiny single-location shops, solo practitioners, or directories.

## CRITICAL — tools
- WebSearch — the ONLY discovery tool.
- Write — for the output file.
You do NOT use Bash, Python, ddgs, or crawl4ai.

## Input (from orchestrator)
```
Query: <one search query string>
OutputFile: <absolute path to candidates-batch-XX.txt>
```

## Workflow (one pass)
1. WebSearch the query exactly as given.
2. Scan the top 10 results.
3. For each result matching the inclusion rules, build one pipe-delimited line.
4. Write all lines to OutputFile.
5. Reply: `Done: N candidates written to <OutputFile>`.
If zero usable results, Write an empty string and report `Done: 0 candidates ...`.

## Output format
One line per company. No headers, no markdown:
```
domain|BusinessName|LB|vertical|branches
```
Country code always `LB`. Example:
```
eliesaab.com|Elie Saab|LB|fashion|0
examplebrand.com|Aishti|LB|retail|0
bankaudi.com.lb|Bank Audi|LB|bank|0
maliagroup.com|Malia Group|LB|fmcg|0
```

## Inclusion (ALL true)
- Real corporate website (domain in the result URL, not a social profile).
- Operates primarily in Lebanon (`.lb` TLD, or Lebanon HQ/address in the snippet),
  including large Lebanese brands that also export.
- Is a LARGE / prominent company or well-known brand (not a small local shop).
- A real operating company, not a directory / news article / aggregator / ranking list.

## Exclusion
- Aggregators / directories / ranking listicles (use them only to discover names,
  never output the listicle's own domain).
- Social profiles (instagram/facebook/tiktok/x/linkedin/linktr.ee).
- News articles, press releases, job boards (bayt, hirelebanese).
- Government, embassies, NGOs.
- Small single-location businesses with no real corporate operation.
- Duplicate domains within this batch.

## Vertical mapping (use EXACTLY one)
`fashion` (couture/fashion house), `retail` (luxury/department/specialty retail),
`bank` (bank/financial), `fmcg` (food/consumer goods/trading), `industrial`
(manufacturing/industrial group), `conglomerate` (diversified holding/group),
`hospitality` (hotel/F&B group), `realestate` (developer/large brokerage),
`telecom`, `pharma` (pharma/healthcare group). If none fits cleanly, skip.

## Domain format
Root domain only, lowercase, no `www.`, no path, no trailing slash.

## Output expectations
- Aim 6 to 12 per query. Quality over quantity. Zero is acceptable for a dead query.

## You do not
Validate emails/phones, score, decide who to contact, or fetch HTML — downstream
stages do that. Scope: one DDG query → pipe-delimited big-LB-company results → done.
```

- [ ] **Step 2: Verify front-matter name + tools**

Run: `head -6 .claude/agents/source-agent-lb-enterprise.md`
Expected: `name: source-agent-lb-enterprise`, `tools: WebSearch, Write`.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/source-agent-lb-enterprise.md
git commit -m "feat(lb-enterprise): add biggest-LB-companies source agent"
```

---

### Task 3: wa-writer agent (custom WhatsApp message writer)

**Files:**
- Create: `.claude/agents/wa-writer.md`

**Interfaces:**
- Consumes: `voice-lb-wa.md` (Task 1); per-lead batch text written by Task 4 prep.
- Produces: a subagent named `wa-writer` (tools: Read, Write, WebSearch, WebFetch) that writes one JSON object to its OutputFile with keys `lead_id`, `lead_slug`, `body_text`, `workflow_gaps`, `how_we_help` (or `{"skip": true, "reason": ...}`). Task 4 merge consumes these.

- [ ] **Step 1: Write the agent file**

Create `.claude/agents/wa-writer.md`:

```markdown
---
name: wa-writer
description: Writes ONE fully custom cold WhatsApp message for ONE big Lebanese company, built on the specific manual workflow gap found for it. Reads the company's already-scraped pages (and may run one web search for a signal), writes two analysis paragraphs (workflow gaps; how we help), then forms the WhatsApp message per voice-lb-wa.md. David Geha presents as an AUB engineering student (no "Automate", no automatelb.com). Dispatched in parallel (one per company) by the /fire orchestrator on the lb-enterprise WhatsApp run. Uses Read, Write, WebSearch, WebFetch only.
model: haiku
tools: Read, Write, WebSearch, WebFetch
---

# wa-writer — one company, one custom WhatsApp message

You write ONE WhatsApp message for ONE big Lebanese company and exit. There is no
template. Every message is built on the specific repetitive workflow this company
does by hand.

## Step 0 — read the voice spec (REQUIRED, first)
`Read` and follow exactly:
`<repo-root>/project/vault/lead-outreach/voice-lb-wa.md`
If it is missing, abort and report. Do not improvise voice.

## Input (from orchestrator)
```
LeadId, LeadSlug, Business, Vertical, Website
Contact: Mr./Mrs. <Surname>  (first=… last=…)
HtmlFiles:  <abs paths to already-scraped raw_html pages>
OutputFile: <abs path to wa-out-NNN.json>
```

## Workflow
1. Read voice-lb-wa.md.
2. Read 2 to 4 HtmlFiles (prioritize home, about, contact, services). Find the real
   manual workflow: customer questions across phone/WhatsApp/Instagram, order status,
   reservations/clienteling, intake, manual reporting/reconciliation.
3. If the pages did not yield a concrete company-specific hook, run ONE WebSearch:
   `"<Business>" Lebanon customer service OR careers OR ecommerce` and scan for a
   specific, real signal. Only after that yields nothing may you Skip.
4. Write `workflow_gaps` (4 to 5 sentences) and `how_we_help` (4 to 5 sentences).
5. Form `body_text` (the WhatsApp message) from those two, per the locked structure
   in voice-lb-wa.md.
6. Write the OutputFile (schema below).
7. Reply one line: `Done: wrote <OutputFile>` (or `Skip: <reason>`).

## Hard guards (re-check before Write)
- Salutation exactly `Hello Mr. <Surname>,` or `Hello Mrs. <Surname>,` on line 1.
- Identity line is the AUB-student line; social proof is the Lebanon/GCC/India line.
- Zero money words. Zero em/en dashes. No `automatelb.com`, no "Automate" anywhere.
- Signature is exactly `David Geha` (final line).
- The first gap line names something real about THIS company (not its category).
  If you cannot, write `{"skip": true, "reason": "..."}`.

## Output schema (write EXACTLY this; no extra keys)
```json
{
  "lead_id": "web-aishti-com",
  "lead_slug": "aishti-com",
  "workflow_gaps": "<4-5 sentences>",
  "how_we_help": "<4-5 sentences>",
  "body_text": "Hello Mr. <Surname>,\n\n<gap line>\n\nI'm David Geha, a third-year engineering student at AUB. I work with a team building custom AI systems for clients across Lebanon, the GCC, and India, and we'd take that kind of repetitive intake and follow-up off your team so they get their time back and customers get instant answers around the clock. You own the system, no platform lock-in.\n\nWorth a short call this week to show you what it would look like for <Company>?\n\nDavid Geha"
}
```
Skipping: write `{"skip": true, "reason": "<why>"}` instead.

## Don'ts
Don't invent facts (use only the pages or your one search); don't reuse phrasing
across companies; don't mention Automate or automatelb.com; don't add a subject (this
is WhatsApp); don't call any tool other than Read, Write, WebSearch, WebFetch.
```

- [ ] **Step 2: Verify name + tools**

Run: `head -6 .claude/agents/wa-writer.md`
Expected: `name: wa-writer`, `tools: Read, Write, WebSearch, WebFetch`.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/wa-writer.md
git commit -m "feat(lb-enterprise): add wa-writer custom WhatsApp message agent"
```

---

### Task 4: draft_whatsapp_custom.py (prep + merge)

**Files:**
- Create: `project/tools/scripts/draft_whatsapp_custom.py`
- Test: `project/tests/test_draft_whatsapp_custom.py`

**Interfaces:**
- Consumes: `runs/<slug>/leads-with-contact.json` (Stage 5.5 output), `runs/<slug>/raw_html/`, `wa-out-*.json` (from `wa-writer`).
- Produces: in `prep`, `wa-batch-NNN.txt` files; in `merge`, `runs/<slug>/whatsapp-drafted.json` (one JSON per line) in the schema `bridge/send_campaign.js` consumes: keys `lead_id`, `lead_slug`, `to_phone`, `to_jid`, `to_name`, `salutation`, `body_text`, `score`, `vertical`, `country_code`, `tags`, plus `workflow_gaps`, `how_we_help`. Exits non-zero (5) if zero drafts survive.

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_draft_whatsapp_custom.py`:

```python
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "scripts" / "draft_whatsapp_custom.py"


def _run(run_dir, phase):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--phase", phase, "--run-dir", str(run_dir)],
        capture_output=True, text=True,
    )


def _lead(**over):
    base = {
        "lead_id": "web-aishti-com", "lead_slug": "aishti-com", "name": "Aishti",
        "website": "examplebrand.com", "vertical": "retail", "country_code": "LB",
        "score": 90, "contact_first_name": "Tony", "contact_last_name": "Salame",
        "contact_title": "Mr.", "contact_phone": "+96170000000",
        "contact_email": "founder@examplebrand.com",
    }
    base.update(over)
    return base


def _good_body():
    return (
        "Hello Mr. Salame,\n\n"
        "Aishti routes a lot of stock and order questions through your team by hand.\n\n"
        "I'm David Geha, a third-year engineering student at AUB. I work with a team "
        "building custom AI systems for clients across Lebanon, the GCC, and India, and "
        "we'd take that repetitive work off your team. You own the system, no platform "
        "lock-in.\n\n"
        "Worth a short call this week?\n\n"
        "David Geha"
    )


def _setup(tmp_path, leads, outs):
    run = tmp_path / "runs" / "2026-06-22-lb-enterprise"
    run.mkdir(parents=True)
    (run.parent.parent / "vault" / "lead-outreach").mkdir(parents=True)
    (run.parent.parent / "vault" / "lead-outreach" / "sent-log.md").write_text("")
    (run / "leads-with-contact.json").write_text(
        "\n".join(json.dumps(l) for l in leads) + "\n")
    for i, o in enumerate(outs, 1):
        (run / f"wa-out-{i:03d}.json").write_text(json.dumps(o))
    return run


def test_merge_keeps_valid_draft(tmp_path):
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": _good_body()}])
    r = _run(run, "merge")
    assert r.returncode == 0, r.stderr + r.stdout
    rows = [json.loads(x) for x in (run / "whatsapp-drafted.json").read_text().splitlines() if x.strip()]
    assert len(rows) == 1
    d = rows[0]
    assert d["to_jid"] == "96170000000@c.us"
    assert d["salutation"] == "Mr. Salame"
    assert "automatelb" not in d["body_text"].lower()


def test_merge_drops_automate_mention(tmp_path):
    body = _good_body().replace("David Geha", "David Geha\nAutomate, automatelb.com")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5  # zero survive -> abort


def test_merge_drops_money_talk(tmp_path):
    body = _good_body().replace("no platform lock-in.", "no platform lock-in. It is free.")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5


def test_merge_drops_bad_salutation(tmp_path):
    body = _good_body().replace("Hello Mr. Salame,", "Hi there,")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5


def test_prep_skips_lead_without_mobile(tmp_path):
    run = _setup(tmp_path, [_lead(contact_phone="")], [])
    # add raw_html dir so prep doesn't choke
    (run / "raw_html").mkdir()
    r = _run(run, "prep")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not list(run.glob("wa-batch-*.txt"))  # no mobile -> not prepped
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd project && python3 -m pytest tests/test_draft_whatsapp_custom.py -q`
Expected: FAIL (script does not exist yet).

- [ ] **Step 3: Write the implementation**

Create `project/tools/scripts/draft_whatsapp_custom.py`:

```python
#!/usr/bin/env python3
"""Stage 8.5 (CUSTOM WhatsApp draft) — per-company custom WhatsApp drafting.

WhatsApp analog of draft_custom.py. Used when a run has draft_mode=custom AND
channels.json contains "whatsapp" but NOT "email" (the lb-enterprise run). One
Haiku `wa-writer` sub-agent writes a fully custom WhatsApp message per company,
built on the specific manual workflow gap it finds. Two phases:

  prep   reads leads-with-contact.json, keeps only leads with a valid mobile
         (CEO direct number required — kill-on-fallback), writes one
         wa-batch-NNN.txt per kept lead + a wa-out-NNN.json OutputFile path.

  merge  reads all wa-out-*.json, joins to leads-with-contact.json, enforces the
         contract (Mr./Mrs.+surname salutation, no money words, no em/en dashes,
         NO automatelb.com / NO "Automate", valid mobile, cross-run dedup), and
         writes whatsapp-drafted.json (the bridge/send_campaign.js input). Halts
         (exit 5) if zero drafts survive.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge"])
p.add_argument("--run-dir", required=True)
p.add_argument("--sent-log", default=None)
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
WITH_CONTACT = ROOT / "leads-with-contact.json"
RAW = ROOT / "raw_html"
OUT = ROOT / "whatsapp-drafted.json"
RUN_SLUG = ROOT.name
SENT_LOG = Path(args.sent_log) if args.sent_log else ROOT.parents[2] / "vault" / "lead-outreach" / "sent-log.md"

PAGE_PRIORITY = ["home", "index", "about", "aboutus", "contact", "contactus",
                 "services", "team", "people"]

COUNTRY_CC = {"AE": "971", "SA": "966", "QA": "974", "BH": "973", "KW": "965", "LB": "961"}
MOBILE_RULES = {
    "LB": [("3", 10), ("70", 11), ("71", 11), ("76", 11), ("78", 11), ("79", 11), ("81", 11)],
}

_DASH_RE = re.compile(r"\s*[—–]\s*")
MONEY_RE = re.compile(
    r"\$|\b(price|pricing|priced|fee|fees|retainer|commission|monthly fee|"
    r"per month|/month|cost|costs|free|charge|charges|dollar|dollars|usd|"
    r"invoice|subscription)\b", re.IGNORECASE)
BRAND_RE = re.compile(r"automate|automatelb\.com", re.IGNORECASE)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WA_PHONE_RE = re.compile(r"wa:(\d{6,15})")
_SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")


def normalize_phone(raw: str, cc_key: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    cc = COUNTRY_CC.get(cc_key, "")
    if cc and not digits.startswith(cc):
        digits = cc + digits.lstrip("0")
    if len(digits) < 9 or len(digits) > 15:
        return ""
    rules = MOBILE_RULES.get(cc_key)
    if rules:
        after = digits[len(cc):]
        if not any(after.startswith(pfx) and len(digits) == n for pfx, n in rules):
            return ""
    return digits


def load_with_contact() -> list[dict]:
    return [json.loads(l) for l in WITH_CONTACT.read_text().splitlines() if l.strip()]


def domain_of(lead: dict) -> str:
    d = lead.get("website", "").replace("https://", "").replace("http://", "").strip("/")
    return d[4:] if d.startswith("www.") else d


def html_files_for(domain: str) -> list[Path]:
    safe = domain.replace("/", "_")
    real = [f for f in sorted(RAW.glob(f"{safe}__*.html")) if f.exists() and f.stat().st_size >= 500]

    def rank(f: Path) -> int:
        slug = f.stem.split("__", 1)[1] if "__" in f.stem else f.stem
        return PAGE_PRIORITY.index(slug) if slug in PAGE_PRIORITY else len(PAGE_PRIORITY)

    real.sort(key=rank)
    return real[:5]


def _root_domain(s: str) -> str:
    s = (s or "").lower().strip()
    if "//" in s:
        s = s.split("//", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    s = s.split("/", 1)[0]
    return s[4:] if s.startswith("www.") else s


def load_sent_index():
    phones, emails, domains, slugs = set(), set(), set(), set()
    if not SENT_LOG.exists():
        return phones, emails, domains, slugs
    for line in SENT_LOG.read_text().splitlines():
        for m in _WA_PHONE_RE.finditer(line):
            phones.add(m.group(1))
        for m in _EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if "smtp-relay" in e or "mailin.fr" in e:
                continue
            emails.add(e)
            domains.add(e.split("@", 1)[1])
        for m in _SLUG_RE.finditer(line):
            slugs.add(m.group(1).strip().lower())
    return phones, emails, domains, slugs


def phase_prep() -> None:
    if not WITH_CONTACT.exists():
        raise SystemExit("ABORT: leads-with-contact.json missing (run Stage 5.5 first).")
    leads = load_with_contact()
    for f in ROOT.glob("wa-batch-*.txt"):
        f.unlink()
    for f in ROOT.glob("wa-out-*.json"):
        f.unlink()

    kept = 0
    dropped_no_mobile = 0
    for lead in leads:
        title = lead.get("contact_title", "").strip()
        first = lead.get("contact_first_name", "").strip()
        last = lead.get("contact_last_name", "").strip()
        phone = normalize_phone(lead.get("contact_phone", ""), lead.get("country_code", ""))
        if not (first and last and title in {"Mr.", "Mrs."} and phone):
            dropped_no_mobile += 1
            continue
        kept += 1
        nnn = f"{kept:03d}"
        out_path = ROOT / f"wa-out-{nnn}.json"
        files = html_files_for(domain_of(lead))
        html_block = "\n".join(f"  {f}" for f in files) if files else "  (none scraped)"
        (ROOT / f"wa-batch-{nnn}.txt").write_text(
            f"LeadId: {lead['lead_id']}\n"
            f"LeadSlug: {lead['lead_slug']}\n"
            f"Business: {lead['name']}\n"
            f"Vertical: {lead.get('vertical','')}\n"
            f"Website: {lead['website']}\n"
            f"Contact: {title} {last}  (first={first} last={last})\n"
            f"HtmlFiles:\n{html_block}\n"
            f"OutputFile: {out_path}\n"
        )
    print(f"Prepped {kept} wa-writer batches in {ROOT}")
    print(f"  dropped no valid CEO mobile: {dropped_no_mobile}  (kill-on-fallback)")
    print("Dispatch this many parallel wa-writer Haiku agents from the orchestrator.")


def phase_merge() -> None:
    leads = load_with_contact()
    by_id = {l["lead_id"]: l for l in leads}
    sent_phones, sent_emails, sent_domains, sent_slugs = load_sent_index()

    drafts = []
    skipped = parse_errors = 0
    d_salu = d_money = d_brand = d_phone = d_nolead = d_dup_sent = 0

    for f in sorted(ROOT.glob("wa-out-*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            parse_errors += 1
            continue
        if data.get("skip"):
            skipped += 1
            continue
        lead = by_id.get(data.get("lead_id"))
        if not lead:
            d_nolead += 1
            continue

        phone = normalize_phone(lead.get("contact_phone", ""), lead.get("country_code", ""))
        if not phone:
            d_phone += 1
            continue

        body = _DASH_RE.sub(", ", (data.get("body_text") or "").strip())
        last = lead.get("contact_last_name", "").strip()
        title = lead.get("contact_title", "").strip()
        first_line = body.split("\n", 1)[0]
        if not first_line.startswith(f"Hello {title} ") or last not in first_line:
            d_salu += 1
            continue
        if MONEY_RE.search(body):
            d_money += 1
            continue
        if BRAND_RE.search(body):
            d_brand += 1
            continue

        email = (lead.get("to_email") or lead.get("contact_email") or "").strip().lower()
        domain = _root_domain(lead.get("website") or email)
        slug = (lead.get("lead_slug") or "").strip().lower()
        if (phone in sent_phones or slug in sent_slugs
                or (email and email in sent_emails)
                or (domain and domain in sent_domains)):
            d_dup_sent += 1
            continue

        drafts.append({
            "lead_id": lead["lead_id"],
            "lead_slug": lead["lead_slug"],
            "to_phone": phone,
            "to_jid": f"{phone}@c.us",
            "contact_email": email,
            "website": lead.get("website", ""),
            "to_name": f"{lead.get('contact_first_name','')} {last}".strip(),
            "salutation": f"{title} {last}",
            "contact_first_name": lead.get("contact_first_name", ""),
            "contact_last_name": last,
            "contact_title": title,
            "contact_role": lead.get("contact_role", ""),
            "contact_phone_source_url": lead.get("contact_phone_source_url", ""),
            "contact_confidence": lead.get("contact_confidence", ""),
            "business_name": lead["name"],
            "body_text": body,
            "workflow_gaps": data.get("workflow_gaps", ""),
            "how_we_help": data.get("how_we_help", ""),
            "tags": ["cold-outreach", "whatsapp", RUN_SLUG, lead.get("vertical", ""), lead.get("country_code", "")],
            "score": lead.get("score", 0),
            "qualification_status": "send_ready",
            "primary_gap": "custom_gap",
            "channel": "whatsapp",
            "send_gate": "pass",
            "country_code": lead.get("country_code", ""),
            "vertical": lead.get("vertical", ""),
        })

    drafts.sort(key=lambda d: -d["score"])
    OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")

    print(f"Custom-drafted {len(drafts)} WhatsApp messages (wa-writer path).")
    print(f"  skipped by wa-writer (no concrete gap): {skipped}")
    print(f"  dropped salutation-contract:            {d_salu}")
    print(f"  dropped money-talk:                     {d_money}")
    print(f"  dropped Automate/automatelb.com:        {d_brand}")
    print(f"  dropped no valid mobile:                {d_phone}")
    print(f"  dropped lead-not-found:                 {d_nolead}")
    print(f"  dropped already-contacted:              {d_dup_sent}")
    print(f"  wa-out parse errors:                    {parse_errors}")
    for d in drafts[:5]:
        print(f"\n--- {d['to_phone']} | {d['to_name']} | {d['salutation']} ---\n{d['body_text'][:280]}...")
    if not drafts:
        print("ABORT: custom WhatsApp draft produced 0 messages after the contract gate.")
        raise SystemExit(5)


if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd project && python3 -m pytest tests/test_draft_whatsapp_custom.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/draft_whatsapp_custom.py project/tests/test_draft_whatsapp_custom.py
git commit -m "feat(lb-enterprise): add draft_whatsapp_custom.py (prep+merge, contract-gated)"
```

---

### Task 5: fire.md — WhatsApp-only + custom-WhatsApp branch

**Files:**
- Modify: `project/.claude/commands/fire.md`

**Interfaces:**
- Consumes: `runs/<slug>/channels.json`, `draft_mode.txt`, the Task 4 script, the `wa-writer` agent.
- Produces: orchestrator behavior — when email is disabled, skip Steps 7/8; when custom + WhatsApp, route Step 8.5 to the wa-writer fan-out.

- [ ] **Step 1: Add the EMAIL_ENABLED detection at Step 7 top**

In `project/.claude/commands/fire.md`, find the Step 7 detection block (the `DRAFT_MODE="template"` snippet around line 228-234). Immediately BEFORE it, insert:

````markdown
**Channel gate (read first).** Determine which channels this run uses:

```bash
EMAIL_ENABLED=1
WA_ENABLED=0
if [ -f "$RUN/channels.json" ]; then
    grep -q '"email"' "$RUN/channels.json" || EMAIL_ENABLED=0
    grep -q '"whatsapp"' "$RUN/channels.json" && WA_ENABLED=1
fi
echo "Channels: email=$EMAIL_ENABLED whatsapp=$WA_ENABLED"
```

If `channels.json` is absent, email stays ON (every existing run is unaffected).
**When `EMAIL_ENABLED=0`, SKIP Step 7 (draft) and Step 8 (Brevo send) entirely** —
do not draft or send any email. Go straight from Stage 5.5 to Step 8.5.
````

- [ ] **Step 2: Gate Steps 7 and 8 on EMAIL_ENABLED**

At the very start of Step 7 ("### Step 7 — draft"), add the line:
`Run Step 7 ONLY if EMAIL_ENABLED=1. If EMAIL_ENABLED=0, skip to Step 8.5.`
At the very start of "### Step 8 — Brevo batch send", add:
`Run Step 8 ONLY if EMAIL_ENABLED=1. If EMAIL_ENABLED=0, skip to Step 8.5.`

- [ ] **Step 3: Add the custom-WhatsApp branch inside Step 8.5**

In "### Step 8.5 — WhatsApp channel", find the line:
`python3 tools/scripts/draft_whatsapp.py --run-dir "$RUN"`
Replace it with this branch:

````markdown
Choose the WhatsApp drafter by draft mode:

```bash
WA_DRAFT_MODE="template"
[ -f "$RUN/draft_mode.txt" ] && WA_DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')
```

- If `WA_DRAFT_MODE` = `custom`: use the wa-writer fan-out (no template).

  Phase A — prep per-company batches:
  ```bash
  python3 tools/scripts/draft_whatsapp_custom.py --phase prep --run-dir "$RUN"
  WA_BATCHES=$(ls "$RUN"/wa-batch-*.txt 2>/dev/null | wc -l | tr -d ' ')
  [ "${WA_BATCHES:-0}" -ge 1 ] || { echo "WARN: 0 leads with a CEO mobile; skipping WhatsApp."; WA_ENABLED=0; }
  echo "Will dispatch $WA_BATCHES parallel wa-writer sub-agents."
  ```

  Then issue the `Agent` tool calls FOREGROUND, up to 50 per assistant message, one
  per `wa-batch-NNN.txt`. Same dispatch economics as Step 3/6.5 — never set
  `run_in_background`. Each call: **subagent_type** `wa-writer`, **model** `haiku`,
  **prompt** = the contents of `wa-batch-NNN.txt` verbatim. Do NOT inline the
  wa-writer instructions — they live in `.claude/agents/wa-writer.md`.

  Phase B — merge + enforce contract:
  ```bash
  python3 tools/scripts/draft_whatsapp_custom.py --phase merge --run-dir "$RUN"
  WA_DRAFTED=$(wc -l < "$RUN/whatsapp-drafted.json" 2>/dev/null | tr -d ' ')
  [ "${WA_DRAFTED:-0}" -ge 1 ] || { echo "ABORT: 0 custom WhatsApp drafts after the contract gate."; exit 8; }
  ```

- Otherwise (template mode), keep the existing template drafter:
  ```bash
  python3 tools/scripts/draft_whatsapp.py --run-dir "$RUN"
  ```
````

- [ ] **Step 4: Extend the halt table + hard rules**

In the Halt table, add a row:
`| WhatsApp draft (custom) | draft_whatsapp_custom.py merge produced 0 drafts after the contract gate |`
In "## Hard rules", add:
`- **WhatsApp-only runs** (channels.json has "whatsapp" and not "email"): skip Steps 7 and 8; never send email. Custom WhatsApp runs use draft_whatsapp_custom.py (wa-writer fan-out), never draft_whatsapp.py.`

- [ ] **Step 5: Verify the edits are present**

Run: `grep -n "EMAIL_ENABLED\|draft_whatsapp_custom\|wa-writer" project/.claude/commands/fire.md`
Expected: matches in the channel gate, Step 8.5 branch, and hard rules.

- [ ] **Step 6: Commit**

```bash
git add project/.claude/commands/fire.md
git commit -m "feat(lb-enterprise): fire.md WhatsApp-only + custom wa-writer branch"
```

---

### Task 6: lb-enterprise run-folder template

**Files:**
- Create: `project/runs/2026-06-22-lb-enterprise/icp.yaml`
- Create: `project/runs/2026-06-22-lb-enterprise/queries.txt`
- Create: `project/runs/2026-06-22-lb-enterprise/countries.txt`
- Create: `project/runs/2026-06-22-lb-enterprise/source_agent.txt`
- Create: `project/runs/2026-06-22-lb-enterprise/channels.json`
- Create: `project/runs/2026-06-22-lb-enterprise/draft_mode.txt`
- Create: `project/runs/2026-06-22-lb-enterprise/vertical.txt`
- Create: `project/runs/2026-06-22-lb-enterprise/brief.md`

**Interfaces:**
- Consumes: the agents/scripts from Tasks 1-5.
- Produces: the template folder the CLAUDE.md router (Task 7) clones from. `runs/` is gitignored, so these are created but not committed — they must be force-added so the template persists (see Step 9).

- [ ] **Step 1: Create the run dir and control files**

```bash
cd project && mkdir -p runs/2026-06-22-lb-enterprise
printf 'LB\n' > runs/2026-06-22-lb-enterprise/countries.txt
printf 'source-agent-lb-enterprise\n' > runs/2026-06-22-lb-enterprise/source_agent.txt
printf '["whatsapp"]\n' > runs/2026-06-22-lb-enterprise/channels.json
printf 'custom\n' > runs/2026-06-22-lb-enterprise/draft_mode.txt
printf 'enterprise\n' > runs/2026-06-22-lb-enterprise/vertical.txt
```

- [ ] **Step 2: Write queries.txt** (one DDG query per line)

```bash
cat > runs/2026-06-22-lb-enterprise/queries.txt <<'EOF'
largest companies in Lebanon
biggest Lebanese brands
top Lebanese fashion houses
luxury retail groups Lebanon
biggest employers Lebanon
Lebanese conglomerates holding groups
top FMCG companies Lebanon
largest banks Lebanon
biggest real estate developers Lebanon
top hospitality groups Lebanon
leading industrial companies Lebanon
largest trading companies Lebanon
Lebanese family business groups
top retail chains Lebanon
biggest telecom companies Lebanon
Lebanon export brands manufacturers
EOF
```

- [ ] **Step 3: Write brief.md**

```bash
cat > runs/2026-06-22-lb-enterprise/brief.md <<'EOF'
# Brief — lb-enterprise (WhatsApp custom consultant)

Target: the biggest Lebanese companies and brands (Elie Saab, Aishti, banks,
FMCG/industrial groups, conglomerates, hospitality groups).

Channel: WhatsApp only. Auto-send via the bridge.

Per company: analyze workflow gaps + repetitive tasks (paragraph 1), how David
Geha and team build custom AI to help (paragraph 2), then form a tailored
WhatsApp message from the two.

Sender identity: David Geha, third-year engineering student at AUB, working with
a team on custom AI systems for clients across Lebanon, the GCC, and India. No
"Automate" mention, no automatelb.com link, no money talk.

Contact: CEO/decision-maker direct mobile required, else drop the company.
EOF
```

- [ ] **Step 4: Write icp.yaml**

```bash
cat > runs/2026-06-22-lb-enterprise/icp.yaml <<'EOF'
run_slug: 2026-06-22-lb-enterprise
created: 2026-06-22

product:
  name: "Custom AI systems (consulting)"
  one_liner: "David Geha (AUB engineering student) and team build custom AI that removes repetitive company workflows. Clients across Lebanon, the GCC, and India."
  channels_supported:
    - WhatsApp outbound (decision-maker direct mobile)

target:
  description: "The biggest Lebanese companies and brands with enough manual, repetitive workflow (customer questions, intake, order status, clienteling, reporting) to automate."
  industries:
    - fashion / couture houses
    - luxury and department retail
    - banks and financial groups
    - FMCG / trading / industrial groups
    - diversified conglomerates / holdings
    - large hospitality / F&B groups
    - large real-estate developers, telecoms, pharma/healthcare groups
  geo:
    countries: ["Lebanon"]
  size_signal: "Large / prominent company or well-known brand. Not small single-location businesses."

sources:
  - web (DuckDuckGo via source-agent-lb-enterprise)

hard_filters:
  - "Must be a large/prominent Lebanon-based company or brand"
  - "Must have a real corporate website"
  - "Must have a resolvable CEO/decision-maker with a DIRECT mobile (else dropped)"
  - "Exclude directories, news, rankings, social profiles, government, NGOs"

buyer_profile:
  required: true
  acceptable_titles: ["Owner","Founder","Co-Founder","CEO","Managing Director","General Manager","Chairman","Managing Partner"]
  reject_titles: ["Receptionist","Coordinator","Assistant","Branch employee"]
  generic_inbox_policy:
    rule: "WhatsApp goes to the decision-maker's direct mobile only. No generic/company numbers. No mobile resolved -> drop."

scoring:
  threshold_qualify: 60
  threshold_send: 60
  hard_floor: 50
  weights: { fit: 40, reachability: 40, buyer: 20 }

run_settings:
  source_target_raw: 120
  qualified_target: 40
  send_cap: 100
  approval_mode: "full_auto"
  channels: ["whatsapp"]
  notes:
    - "WhatsApp-only: fire.md skips email (Steps 7/8) because channels.json has no 'email'."
    - "draft_mode=custom -> wa-writer fan-out via draft_whatsapp_custom.py."
    - "source_agent.txt = source-agent-lb-enterprise."
    - "No Automate mention, no automatelb.com link, no money talk (enforced in merge)."
    - "Mr./Mrs. salutation + no em-dashes enforced in merge."
EOF
```

- [ ] **Step 5: Verify the template passes the bootstrap readiness check**

Run: `cd project && ls runs/2026-06-22-lb-enterprise/ && test -f runs/2026-06-22-lb-enterprise/icp.yaml && test -f runs/2026-06-22-lb-enterprise/queries.txt && echo READY`
Expected: lists all 8 files and prints `READY`.

- [ ] **Step 6: Force-add the template (runs/ is gitignored) and commit**

```bash
cd <repo-root>
git add -f project/runs/2026-06-22-lb-enterprise/icp.yaml \
            project/runs/2026-06-22-lb-enterprise/queries.txt \
            project/runs/2026-06-22-lb-enterprise/countries.txt \
            project/runs/2026-06-22-lb-enterprise/source_agent.txt \
            project/runs/2026-06-22-lb-enterprise/channels.json \
            project/runs/2026-06-22-lb-enterprise/draft_mode.txt \
            project/runs/2026-06-22-lb-enterprise/vertical.txt \
            project/runs/2026-06-22-lb-enterprise/brief.md
git commit -m "feat(lb-enterprise): add run-folder template (whatsapp-only, custom)"
```

---

### Task 7: CLAUDE.md router — "fire lebanese run"

**Files:**
- Modify: `project/CLAUDE.md:43-56` (the Step-1 router table + bullets)

**Interfaces:**
- Consumes: nothing.
- Produces: the natural-language route so "fire lebanese run" bootstraps + fires `lb-enterprise`, and "lb"/"lebanon" disambiguates instead of silently hitting the receptionist run.

- [ ] **Step 1: Add the router table row**

In `project/CLAUDE.md`, in the Step-1 table, the current row is:
`| "lebanon" / "lb" | `lb-receptionist` | `lb-receptionist` |`
Replace it with these three rows:
```
| "lebanese" / "lebanese run" / "biggest lebanese companies" | `lb-enterprise` | `lb-enterprise` |
| "lb receptionist" / "receptionist lebanon" | `lb-receptionist` | `lb-receptionist` |
| bare "lebanon" / "lb" (no other word) | ASK: lb-enterprise or lb-receptionist | — |
```

- [ ] **Step 2: Add a disambiguation bullet**

In the "### When to ASK instead of firing" section, add a bullet:
```
- **Bare "lebanon"/"lb" with no other qualifier** → ASK which Lebanon run:
  lb-enterprise (WhatsApp, biggest companies, custom consultant pitch) or
  lb-receptionist (AI receptionist, phone-heavy SMBs). "lebanese run" alone →
  lb-enterprise, no question.
```

- [ ] **Step 3: Verify**

Run: `grep -n "lb-enterprise" project/CLAUDE.md`
Expected: at least two matches (table row + bullet).

- [ ] **Step 4: Commit**

```bash
git add project/CLAUDE.md
git commit -m "feat(lb-enterprise): route 'fire lebanese run' to lb-enterprise"
```

---

### Task 8: End-to-end dry-run verification

**Files:** none (verification only).

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the unit tests**

Run: `cd project && python3 -m pytest tests/test_draft_whatsapp_custom.py -q`
Expected: PASS (5 passed).

- [ ] **Step 2: Prove the WhatsApp-only custom merge works on a synthetic fixture**

Run:
```bash
cd project
TMP="runs/_smoke-lb-enterprise"
mkdir -p "$TMP/raw_html"
printf '{"lead_id":"web-aishti-com","lead_slug":"aishti-com","name":"Aishti","website":"examplebrand.com","vertical":"retail","country_code":"LB","score":90,"contact_first_name":"Firstname","contact_last_name":"Lastname","contact_title":"Mr.","contact_phone":"+96170000000","contact_email":"founder@examplebrand.com"}\n' > "$TMP/leads-with-contact.json"
python3 tools/scripts/draft_whatsapp_custom.py --phase prep --run-dir "$TMP"
printf '{"lead_id":"web-aishti-com","lead_slug":"aishti-com","workflow_gaps":"g","how_we_help":"h","body_text":"Hello Mr. Salame,\\n\\nAishti handles stock and order questions by hand.\\n\\nI'"'"'m David Geha, a third-year engineering student at AUB. I work with a team building custom AI systems for clients across Lebanon, the GCC, and India, and we'"'"'d take that repetitive work off your team. You own the system, no platform lock-in.\\n\\nWorth a short call this week?\\n\\nDavid Geha"}\n' > "$TMP/wa-out-001.json"
python3 tools/scripts/draft_whatsapp_custom.py --phase merge --run-dir "$TMP"
node bridge/send_campaign.js --run-dir "$(pwd)/$TMP"   # DRY-RUN (no --send): prints the message, ships nothing
rm -rf "$TMP"
```
Expected: prep prints `Prepped 1 wa-writer batches`; merge prints `Custom-drafted 1 WhatsApp messages`; the Node dry-run prints the Aishti message and `DRY-RUN done`. No message is sent.

- [ ] **Step 3: Regression — receptionist route + email-always-on untouched**

Run: `grep -n "lb-receptionist" project/CLAUDE.md && grep -n 'EMAIL_ENABLED=1' project/.claude/commands/fire.md`
Expected: receptionist row still present; `EMAIL_ENABLED` defaults to 1 (so GCC/US/worldwide/receptionist runs, which have no `channels.json` email-removal, keep sending email).

- [ ] **Step 4: Final commit (if any verification fixups were needed)**

```bash
git add -A && git commit -m "test(lb-enterprise): end-to-end dry-run verification" || echo "nothing to commit"
```

---

## Notes for the executor

- Do NOT actually `--send` anything. The user wants the wiring in place and one
  sample message (already shown in the design doc). Live firing happens later when
  the user says "fire lebanese run".
- The WhatsApp bridge auth (`project/bridge/.wwebjs_auth`) is reused by
  `send_campaign.js`; if a QR is ever needed, run `npm start` in `project/bridge`
  once. Do not delete `.wwebjs_auth`.
- Keep all fan-out dispatches FOREGROUND, ≤50 per message (memory:
  no-background-dispatch-fanout).
</content>
