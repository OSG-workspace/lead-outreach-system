# US Custom Gap Emails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make US custom-draft emails attack a distinct, workflow-specific gap per business (instead of the universal "contact form + AI assistant" pitch), and make "no em/en dashes in any email" a hard, guaranteed restriction.

**Architecture:** Split the single gap-writer into two stages — a new Stage 5.6 `gap-finder` enrichment (one agent per business, finds a structured firm-specific gap, no email) feeding a reworked Stage 6 template-edit draft (the `gap-writer` edits a base template skeleton around the enriched gap). A recalibrated merge gate (word-5-gram similarity, banned-opener + lazy-gap drops, hard dash guard) drops emails that converge. A shared `text_guards` module guarantees zero dashes ship from both draft paths.

**Tech Stack:** Python 3 (stdlib only), pytest, Claude Code Haiku sub-agents, Markdown agent/voice/template specs.

**Constraints carried from the session:** Do NOT fire or run any campaign — this plan only fixes the system. Unit tests use synthetic fixtures (no live agents). The live re-draft validation in Task 12 is offered but NOT executed without explicit user approval.

---

## File structure

| File | Responsibility |
|---|---|
| `project/tools/scripts/text_guards.py` (NEW) | Shared dash strip + hard `has_dash` guard for both draft paths |
| `.claude/agents/gap-finder.md` (NEW) | Stage 5.6 agent: find ONE structured, firm-specific gap (no email) |
| `project/tools/scripts/enrich_gap.py` (NEW) | Stage 5.6 prep/merge: batch files in, `gaps-enriched.json` out |
| `project/vault/lead-outreach/email-template-us.md` (NEW) | Base template skeleton the writer edits per business |
| `project/vault/lead-outreach/voice-us.md` (REWRITE) | Remove example + stock lines; per-business offer/CTA; hard no-dash |
| `.claude/agents/gap-writer.md` (REWRITE) | Edit template from gap record; Read/Write only; no full example |
| `project/tools/scripts/draft_custom.py` (REWRITE) | prep reads `gaps-enriched.json`; merge adds recalibrated gate |
| `project/tools/scripts/draft_emails.py` (EDIT) | Apply shared dash guard (closes template-path hole) |
| `project/.claude/commands/fire.md` (EDIT) | Insert Stage 5.6 dispatch; rework Step 7b; halt table; report |
| `project/PIPELINE.md` + `CLAUDE.md` + `project/CLAUDE.md` (EDIT) | Document the new stage + draft-mode flow |
| `project/tests/test_text_guards.py` (NEW) | dash strip/guard tests |
| `project/tests/test_enrich_gap.py` (NEW) | prep/merge tests |
| `project/tests/test_draft_custom.py` (NEW) | gate-function + merge tests |

All script commands run from `project/` with the venv active:
```bash
cd <repo-root>/project && source tools/venv/bin/activate
```

---

## Task 1: Shared dash guard (`text_guards.py`)

**Files:**
- Create: `project/tools/scripts/text_guards.py`
- Test: `project/tests/test_text_guards.py`

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_text_guards.py`:

```python
"""Tests for text_guards.py — the hard no-dash restriction."""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from text_guards import strip_dashes, has_dash


def test_strip_em_dash_signature():
    assert strip_dashes("Automate — automatelb.com") == "Automate, automatelb.com"


def test_strip_removes_every_dash_variant():
    for d in ["—", "–", "―", "‒", "⸺", "⸻"]:
        assert not has_dash(strip_dashes(f"left {d} right"))


def test_has_dash_detects_and_clears():
    assert has_dash("a — b")
    assert not has_dash("a, b")


def test_strip_handles_none_and_empty():
    assert strip_dashes("") == ""
    assert strip_dashes(None) == ""
    assert has_dash(None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_text_guards.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'text_guards'`

- [ ] **Step 3: Write minimal implementation**

Create `project/tools/scripts/text_guards.py`:

```python
#!/usr/bin/env python3
"""Shared text guards for the draft stage.

The em/en/figure/horizontal-bar dash family is the most-recognized AI tell. Every
sent email must contain ZERO of them. `strip_dashes` auto-fixes (replace with a
comma) and `has_dash` is the hard final guard the draft scripts use to GUARANTEE
no dash survives — a draft that still has one after stripping is dropped.
"""
import re

# em (—), en (–), horizontal bar (―), figure dash (‒), two-em (⸺), three-em (⸻)
DASH_CHARS = "—–―‒⸺⸻"
DASH_RE = re.compile(rf"\s*[{DASH_CHARS}]\s*")


def strip_dashes(text: str) -> str:
    """Replace any dash in the family (surrounding whitespace collapsed) with ', '."""
    return DASH_RE.sub(", ", text or "")


def has_dash(text: str) -> bool:
    """True if any dash in the family remains. The hard guarantee guard."""
    return any(ch in (text or "") for ch in DASH_CHARS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_text_guards.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/scripts/text_guards.py tests/test_text_guards.py
git commit -m "feat: shared dash guard for hard no-em-dash restriction"
```

---

## Task 2: Gap-finder agent definition (`gap-finder.md`)

**Files:**
- Create: `.claude/agents/gap-finder.md` (at the SESSION CWD, not under project/)

- [ ] **Step 1: Create the agent file**

Create `<repo-root>/.claude/agents/gap-finder.md`:

````markdown
---
name: gap-finder
description: For ONE US business, find the single most specific, firm-specific manual inbox/scheduling gap tied to that vertical's real workflow. Reads the business's already-scraped pages (and may run one web search). Outputs a structured gap record only, no email writing. Dispatched in parallel (one per lead) by the /fire orchestrator during Stage 5.6 gap enrichment. Uses Read, WebSearch, WebFetch, Write only.
model: haiku
tools: Read, WebSearch, WebFetch, Write
---

# Gap-finder: one business, one structured gap (no email)

You find the gap. You do NOT write an email. Read the scraped pages for ONE
business, name the single sharpest manual inbox/scheduling task it does by hand,
prove it with a verbatim detail from THEIR site, and write one JSON record.

## Input (from orchestrator)
```
LeadId, LeadSlug, Business, Vertical, Website
Contact: Mr./Mrs. <Surname>
ContactEmail
Signal: <type> (<evidence>)
HtmlFiles: <abs paths to already-scraped raw_html pages>
OutputFile: <abs path to gapfind-out-NNN.json>
```

## Workflow
1. Read 2 to 4 of the HtmlFiles (prioritize home, contact, about, team, services).
2. Find the ONE manual task that matters most for THIS vertical's workflow:
   - law: new-matter intake to conflict/deadline triage, consult scheduling
     back-and-forth, matter-type routing.
   - staffing: candidate submission to interview coordination, status updates,
     redeployment follow-ups.
   - clinics: new-patient intake to insurance verification, recall/reactivation,
     no-show chasing.
   - property: maintenance request to vendor dispatch, showing coordination,
     owner/tenant status.
3. Find verbatim evidence it is done by hand. If the HTML did not yield it,
   WebFetch one more page (/contact or /about), then if still nothing run ONE
   WebSearch (`"<Business>" <city> hiring intake coordinator OR receptionist`, or
   `"<Business>" new office OR expanding`). Only after both fail may you skip.
4. Write the OutputFile record.
5. Reply one line: `Done: <LeadId> gap=<gap_type>` or `Skip: <reason>`.

## The gap_type rule (the whole point of this agent)
`gap_type` must name the DOWNSTREAM WORKFLOW CONSEQUENCE, never the raw channel.
- The contact form / shared inbox / info@ / office@ / "we'll get back to you" /
  phone-only intake / no-online-booking are VALID as `evidence` but FORBIDDEN as
  `gap_type`. They are true of every business and say nothing specific.
- A valid `gap_type` is the workflow step a person does by hand because of that
  channel (e.g. "deadline-driven new-matter triage", "interview scheduling
  back-and-forth", "new-patient insurance verification", "maintenance-to-vendor
  dispatch").
- `specificity_check` must answer: would this be true of the firm next door? If
  you cannot explain why NOT, you have a category-level gap, so write
  `{"skip": true, "reason": "..."}`.

## Hard guards (re-check before Write)
- `evidence_quote` is a VERBATIM string copied from THIS firm's pages (a sentence,
  a named staff member, a specific service line, an actual form-field label, a
  stated policy like "24-hour response"). No quote, then skip.
- `gap_type` is NOT the bare contact form / shared inbox / generic-mailbox signal.
- No em-dashes or en-dashes anywhere in the record. Use commas or "to".

## Output schema (write EXACTLY this; no extra keys)
```json
{
  "lead_id": "web-reyeslawfirm-com",
  "gap_type": "deadline-driven new-matter intake triage",
  "workflow_step": "new-matter intake before the consult is booked",
  "evidence": "one sentence: the real signal, with where on the site",
  "evidence_quote": "verbatim string copied from their page",
  "fill": "one sentence: how a quiet assistant removes THAT step",
  "specificity_check": "would this be true of the firm next door? NO because ..."
}
```
Skipping: write `{"skip": true, "reason": "<why>"}` instead.

## Don'ts
Don't write an email or a subject line. Don't invent facts (use only the pages or
your one search). Don't return the contact form as the gap_type. Don't call any
tool other than Read, WebSearch, WebFetch, Write. You are the leaf.
````

- [ ] **Step 2: Verify structure**

Run:
```bash
cd <repo-root>
grep -E "^tools: Read, WebSearch, WebFetch, Write$" .claude/agents/gap-finder.md && \
grep -c "FORBIDDEN as" .claude/agents/gap-finder.md
grep -nE "[\xe2\x80\x94\xe2\x80\x93]" .claude/agents/gap-finder.md && echo "DASH FOUND — fix it" || echo "no dashes OK"
```
Expected: the `tools:` line prints, the FORBIDDEN-as-gap_type rule count is `1`, and the dash grep prints `no dashes OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/gap-finder.md
git commit -m "feat: gap-finder agent for Stage 5.6 gap enrichment"
```

---

## Task 3: Gap enrichment script (`enrich_gap.py`)

**Files:**
- Create: `project/tools/scripts/enrich_gap.py`
- Test: `project/tests/test_enrich_gap.py`

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_enrich_gap.py`:

```python
"""Tests for enrich_gap.py (Stage 5.6 gap enrichment)."""
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from enrich_gap import html_files_for, build_batch_text, collect_enriched


def _lead(**kw):
    base = {"lead_id": "web-x-com", "lead_slug": "x-com", "name": "X LLP",
            "website": "https://x.com", "vertical": "law-firms", "score": 80,
            "contact_title": "Mr.", "contact_first_name": "Jo", "contact_last_name": "Roe",
            "contact_email": "jo@x.com", "signal": "contact_form", "signal_evidence": "form"}
    base.update(kw)
    return base


def test_html_files_for_ranks_priority_and_filters_tiny(tmp_path):
    raw = tmp_path / "raw_html"
    raw.mkdir()
    (raw / "x.com__services.html").write_text("a" * 600)
    (raw / "x.com__home.html").write_text("b" * 600)
    (raw / "x.com__tiny.html").write_text("c" * 100)  # under 500 bytes, dropped
    files = html_files_for("x.com", raw)
    names = [f.name for f in files]
    assert names[0] == "x.com__home.html"      # home ranks before services
    assert "x.com__tiny.html" not in names      # tiny filtered


def test_build_batch_text_has_required_fields(tmp_path):
    text = build_batch_text(_lead(), tmp_path / "gapfind-out-001.json")
    assert "LeadId: web-x-com" in text
    assert "HtmlFiles:" in text
    assert "OutputFile:" in text


def test_collect_enriched_drops_skips_and_joins(tmp_path):
    leads = [_lead(lead_id="web-a-com"), _lead(lead_id="web-b-com")]
    by_id = {l["lead_id"]: l for l in leads}
    f1 = tmp_path / "gapfind-out-001.json"
    f1.write_text(json.dumps({"lead_id": "web-a-com", "gap_type": "deadline triage",
                              "evidence_quote": "q", "fill": "f"}))
    f2 = tmp_path / "gapfind-out-002.json"
    f2.write_text(json.dumps({"skip": True, "reason": "no gap"}))
    enriched, stats = collect_enriched([f1, f2], by_id)
    assert len(enriched) == 1
    assert enriched[0]["lead_id"] == "web-a-com"
    assert enriched[0]["gap_type"] == "deadline triage"
    assert enriched[0]["name"] == "X LLP"        # joined from the lead
    assert stats["skipped"] == 1


def test_collect_enriched_drops_blank_gap_type(tmp_path):
    by_id = {"web-a-com": _lead(lead_id="web-a-com")}
    f = tmp_path / "gapfind-out-001.json"
    f.write_text(json.dumps({"lead_id": "web-a-com", "gap_type": "  "}))
    enriched, stats = collect_enriched([f], by_id)
    assert enriched == []
    assert stats["no_gap_type"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_enrich_gap.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'enrich_gap'`

- [ ] **Step 3: Write the implementation**

Create `project/tools/scripts/enrich_gap.py`:

```python
#!/usr/bin/env python3
"""Stage 5.6 — gap enrichment (US custom-draft runs only).

Runs between Stage 5.5 (contact enrichment) and Stage 6 (custom draft). One
Haiku `gap-finder` per business finds a structured, firm-specific gap; this
script preps the batch files and merges the results.

  prep   reads leads-with-contact.json, writes one gapfind-batch-NNN.txt per lead
         (lead + contact fields + the abs paths of that domain's scraped raw_html
         pages + an OutputFile path). The orchestrator dispatches one gap-finder
         per batch file.

  merge  reads all gapfind-out-*.json, drops {skip:true} records, joins the rest
         to leads-with-contact.json by lead_id, and writes gaps-enriched.json
         (one record per business with a concrete gap). Halts (exit 9) if fewer
         than GAP_FLOOR_FRACTION of leads yield a gap (kill-on-fallback — we do
         not ship generic emails).
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

ROOT = None  # set in __main__ after arg parse

GAP_FLOOR_FRACTION = 0.40

PAGE_PRIORITY = ["home", "index", "contact", "contactus", "about", "aboutus",
                 "team", "ourteam", "attorneys", "people", "staff", "services"]


def load_with_contact(with_contact: Path) -> list[dict]:
    leads = []
    for line in with_contact.read_text().splitlines():
        if line.strip():
            leads.append(json.loads(line))
    return leads


def domain_of(lead: dict) -> str:
    d = lead.get("website", "").replace("https://", "").replace("http://", "").strip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def html_files_for(domain: str, raw_dir: Path) -> list[Path]:
    safe = domain.replace("/", "_")
    files = sorted(raw_dir.glob(f"{safe}__*.html"))
    real = [f for f in files if f.exists() and f.stat().st_size >= 500]

    def rank(f: Path) -> int:
        slug = f.stem.split("__", 1)[1] if "__" in f.stem else f.stem
        return PAGE_PRIORITY.index(slug) if slug in PAGE_PRIORITY else len(PAGE_PRIORITY)

    real.sort(key=rank)
    return real[:5]


def build_batch_text(lead: dict, out_path: Path, raw_dir: Path | None = None) -> str:
    files = html_files_for(domain_of(lead), raw_dir) if raw_dir else []
    html_block = "\n".join(f"  {f}" for f in files) if files else "  (none scraped)"
    title = lead.get("contact_title", "")
    first = lead.get("contact_first_name", "")
    last = lead.get("contact_last_name", "")
    return (
        f"LeadId: {lead['lead_id']}\n"
        f"LeadSlug: {lead['lead_slug']}\n"
        f"Business: {lead['name']}\n"
        f"Vertical: {lead.get('vertical','')}\n"
        f"Website: {lead['website']}\n"
        f"Contact: {title} {last}  (first={first} last={last})\n"
        f"ContactEmail: {lead.get('contact_email','')}\n"
        f"Signal: {lead.get('signal','')}  ({lead.get('signal_evidence','')})\n"
        f"HtmlFiles:\n{html_block}\n"
        f"OutputFile: {out_path}\n"
    )


def collect_enriched(out_files: list[Path], by_id: dict) -> tuple[list[dict], dict]:
    """Join non-skip gap-finder records to their leads. Returns (records, stats)."""
    enriched = []
    stats = {"skipped": 0, "parse_errors": 0, "no_lead": 0, "no_gap_type": 0}
    for f in out_files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            stats["parse_errors"] += 1
            continue
        if data.get("skip"):
            stats["skipped"] += 1
            continue
        lead = by_id.get(data.get("lead_id"))
        if not lead:
            stats["no_lead"] += 1
            continue
        if not (data.get("gap_type") or "").strip():
            stats["no_gap_type"] += 1
            continue
        enriched.append({**lead,
                         "gap_type": data.get("gap_type", "").strip(),
                         "workflow_step": data.get("workflow_step", ""),
                         "gap_evidence": data.get("evidence", ""),
                         "gap_evidence_quote": data.get("evidence_quote", ""),
                         "gap_fill": data.get("fill", ""),
                         "gap_specificity_check": data.get("specificity_check", "")})
    return enriched, stats


def phase_prep(root: Path) -> None:
    with_contact = root / "leads-with-contact.json"
    if not with_contact.exists():
        raise SystemExit("ABORT: leads-with-contact.json missing (run Stage 5.5 first).")
    raw_dir = root / "raw_html"
    leads = load_with_contact(with_contact)
    for f in root.glob("gapfind-batch-*.txt"):
        f.unlink()
    for f in root.glob("gapfind-out-*.json"):
        f.unlink()
    no_html = 0
    for i, lead in enumerate(leads, start=1):
        nnn = f"{i:03d}"
        if not html_files_for(domain_of(lead), raw_dir):
            no_html += 1
        (root / f"gapfind-batch-{nnn}.txt").write_text(
            build_batch_text(lead, root / f"gapfind-out-{nnn}.json", raw_dir)
        )
    print(f"Prepped {len(leads)} gap-finder batches in {root}")
    if no_html:
        print(f"  note: {no_html}/{len(leads)} leads had no scraped pages.")
    print("Dispatch this many parallel gap-finder Haiku agents from the orchestrator.")


def phase_merge(root: Path) -> None:
    with_contact = root / "leads-with-contact.json"
    leads = load_with_contact(with_contact)
    by_id = {l["lead_id"]: l for l in leads}
    enriched, stats = collect_enriched(sorted(root.glob("gapfind-out-*.json")), by_id)

    out = root / "gaps-enriched.json"
    out.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in enriched) + "\n")
    print(f"Gap enrichment: {len(enriched)}/{len(leads)} leads have a concrete gap.")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"Wrote {out}")

    floor = max(1, int(len(leads) * GAP_FLOOR_FRACTION))
    if len(enriched) < floor:
        print(f"ABORT: only {len(enriched)} gaps from {len(leads)} leads "
              f"(< {GAP_FLOOR_FRACTION:.0%} floor). Kill-on-fallback.")
        raise SystemExit(9)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--phase", required=True, choices=["prep", "merge"])
    p.add_argument("--run-dir", required=True)
    a = p.parse_args()
    root = Path(a.run_dir).resolve()
    if a.phase == "prep":
        phase_prep(root)
    else:
        phase_merge(root)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_enrich_gap.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add tools/scripts/enrich_gap.py tests/test_enrich_gap.py
git commit -m "feat: enrich_gap.py Stage 5.6 prep/merge"
```

---

## Task 4: Base email template (`email-template-us.md`)

**Files:**
- Create: `project/vault/lead-outreach/email-template-us.md`

- [ ] **Step 1: Create the template file**

Create `project/vault/lead-outreach/email-template-us.md`:

````markdown
---
type: config
title: "US custom email base template (edited per business)"
status: current
updated: 2026-06-15
---

# US email base template

The skeleton the `gap-writer` agent EDITS for one business using that business's
gap record from Stage 5.6. It is a STRUCTURE, not boilerplate to paste.
Everything in <angle brackets> is rewritten per business from the gap record. The
ONLY fixed parts are the salutation pattern and the signature.

Read `voice-us.md` for the full rules (no money, no em-dashes, under 125 words,
sound human). This file is only the shape.

```
Subject: <3 to 7 words, lowercase, names THIS firm's gap_type, no "AI">

Hello <Mr./Mrs.> <Surname>,

<PARA 1 - EVIDENCE: open on the specific manual workflow_step this firm does by
hand. Ground it in the evidence_quote (a real detail from their site). Name the
workflow, not the channel. Must not read true for the firm next door.>

<PARA 2 - FILL: how a quiet assistant takes THAT exact step off their desk, built
from the gap record's fill. Describe it in their workflow's terms (their matters,
candidates, patients, work orders), not "reads your inbox". Write this sentence
fresh for this firm. Do not reuse a stock line.>

<CTA: one ask, two loose time options, no calendar link. Phrase it fresh for this
firm. Do not reuse a fixed sentence across firms.>

David Geha
Automate, automatelb.com
```

## Fixed vs per-business
- Fixed: the `Hello Mr./Mrs. <Surname>,` pattern; the `David Geha / Automate,
  automatelb.com` signature.
- Rewritten per business from the gap record: subject, PARA 1 (evidence), PARA 2
  (the offer/fill line), and the CTA. The offer line and CTA are NOT fixed; that
  repetition is exactly what we are removing.
````

The template file (which the gap-writer reads) must itself contain zero em/en
dashes, so a Haiku writer is not nudged to imitate them. The content above is
already dash-free; Step 2 enforces it.

- [ ] **Step 2: Verify no dashes and required slots present**

Run:
```bash
cd <repo-root>/project
grep -nE "[\xe2\x80\x94\xe2\x80\x93]" vault/lead-outreach/email-template-us.md && echo "DASH FOUND — fix it" || echo "no dashes OK"
grep -c "PARA 1 - EVIDENCE" vault/lead-outreach/email-template-us.md
```
Expected: `no dashes OK`, and the PARA 1 count is `1`. If a dash is found, replace it with a comma and re-run.

- [ ] **Step 3: Commit**

```bash
git add vault/lead-outreach/email-template-us.md
git commit -m "feat: US base email template skeleton"
```

---

## Task 5: Rewrite the voice spec (`voice-us.md`)

**Files:**
- Rewrite: `project/vault/lead-outreach/voice-us.md`

This removes the full mini-example email and the verbatim stock lines (the
"Not a chatbot…" closer and the fixed CTA) that the gap-writers were copying, and
reframes the spec around editing a template from a supplied gap record.

- [ ] **Step 1: Replace the file contents**

Overwrite `project/vault/lead-outreach/voice-us.md` with:

````markdown
---
type: config
title: "Voice (US): Custom Gap-Based Cold Emails"
status: current
created: 2026-06-08
updated: 2026-06-15
tags:
  - lead-outreach
  - config
  - voice
  - us
related:
  - "[[us-inbox-scheduling]]"
  - "[[us-inbox-scheduling.OVERRIDES]]"
  - "[[voice]]"
---

# Voice (US): canonical spec for the `gap-writer` agent

The single source of truth for US inbox/scheduling campaign email VOICE. The
`gap-writer` Haiku agent reads THIS file before writing. There is no shared
template prose: every email is EDITED for one specific business from the gap
record that the Stage 5.6 `gap-finder` already produced for it.

If anything here conflicts with the old GCC `voice.md`, this file wins for US runs.

---

## The offer (for YOUR understanding, not to paste)

David Geha at Automate (automatelb.com) sets up AI that quietly handles a
business's repetitive inbox and scheduling work: triaging incoming messages,
booking appointments/consults into the calendar, and sending routine follow-ups,
so the team stops doing it by hand. Framed as removing their repetitive work,
not as "AI."

Never describe pricing. No retainer, fee, commission, "free", or cost. Money is a
topic for the call.

---

## The gap record you are given (you do NOT find the gap)

Stage 5.6 already found the gap. Your batch file carries:
- `Gap` (gap_type): the workflow step done by hand (e.g. "deadline-driven
  new-matter triage"). NOT the contact form.
- `WorkflowStep`: where it sits in their process.
- `Evidence` + `EvidenceQuote`: the verbatim proof from their site. Build PARA 1
  on this.
- `Fill`: how a quiet assistant removes that step. Build PARA 2 on this.

EDIT the base template (its path is in your batch file) into a human email built
on these. Do not re-derive the gap. Do not reuse phrasing across firms.

---

## Hard email rules

**Salutation (non-negotiable):** open with `Hello Mr. <Surname>,` or
`Hello Mrs. <Surname>,` on its own line. Never bare "Hello,", never first name,
never "<Firm> team,".

**First line (PARA 1):** the real evidence about THEIR firm, built on the
`EvidenceQuote`. Start with the firm name, a concrete noun about their practice,
or the observation itself. Never start with "I", "We", "My", "I hope this finds
you well", or "Quick question". Never an opener that would read identically for
another firm. Never open on "Your contact form" or "The contact form": name the
workflow step, not the channel.

**Body:** under 125 words including signature. Aim 70 to 110.
- PARA 1: the specific manual workflow step (gap + evidence_quote).
- PARA 2 (the offer line): how an assistant set up by Automate takes THAT exact
  step off their desk, built from `Fill`, in their workflow's terms (their
  matters / candidates / patients / work orders). Write this sentence fresh for
  this firm. Do not reuse a stock line across firms.
- PARA 3 (CTA): one ask, two loose time options, no calendar link. Phrase it
  fresh for this firm. Do not reuse one fixed CTA sentence across firms.

**Subject:** 3 to 7 words, lowercase, tied to this firm's gap, no salesy words,
no "AI". Write fresh per firm.

**Signature (exactly, every email):**
```
David Geha
Automate, automatelb.com
```
Keep the automatelb.com link. No phone, no title, no extra link.

**No em-dashes or en-dashes anywhere (HARD restriction).** Subject, body, and
signature must contain zero `em-dash` and zero `en-dash` characters. Use commas,
periods, colons, parentheses, or "to" for ranges (`15 to 20 minutes`). This is
the most-recognized AI tell. Any email that still contains an em or en dash is
DROPPED at merge, not auto-corrected for you, so write none. Never
`Automate em-dash automatelb.com`; always `Automate, automatelb.com`.

**Sound human.** Short sentences. Plain words. No corporate filler. Write like a
person who looked at their site and noticed one annoying manual thing. Banned
words: transform, revolution, unlock, unleash, leverage, synergy, streamline,
cutting-edge, next-gen, seamless, robust, elevate, empower, "circle back",
"touch base", "move the needle", 10x, game-changer.

**No markdown** in the body. Plain prose, blank line between beats.

---

## Compliance
- Real sender name is present (David Geha).
- Honest subject tied to a real observation.
- An opt-out + physical mailing address are added at send time per CAN-SPAM. Do
  not fake or omit the sender identity in the body.

---

## Diversity rule (the whole point)

Two emails from the same run must not share a sentence. The offer line (PARA 2)
and the CTA are rewritten per firm, built from THAT firm's gap record. Emails
that share a pitch sentence with another are dropped at merge. No stock
"Not a chatbot" line. No stock "Would Tuesday or Thursday" CTA.
````

- [ ] **Step 2: Verify the stock lines and example are gone**

Run:
```bash
cd <repo-root>/project
grep -c "Not a chatbot" vault/lead-outreach/voice-us.md
grep -c "Tuesday or Thursday afternoon work for a quick" vault/lead-outreach/voice-us.md
grep -c "Moreno Family Law" vault/lead-outreach/voice-us.md
grep -c "Mini example" vault/lead-outreach/voice-us.md
grep -c "DROPPED at merge" vault/lead-outreach/voice-us.md
grep -nE "[\xe2\x80\x94\xe2\x80\x93]" vault/lead-outreach/voice-us.md && echo "DASH FOUND — fix it" || echo "no dashes OK"
```
Expected: the first four counts are `0` (no stock closer, no stock CTA, no example email, no "Mini example" heading); the next is `1` (hard no-dash rule present); the dash grep prints `no dashes OK` (the spec that bans dashes must itself contain none, so the Haiku writer is not nudged to imitate them).

- [ ] **Step 3: Commit**

```bash
git add vault/lead-outreach/voice-us.md
git commit -m "refactor: voice-us.md drops example + stock lines, per-business offer/CTA, hard no-dash"
```

---

## Task 6: Rewrite the gap-writer agent (`gap-writer.md`)

**Files:**
- Rewrite: `.claude/agents/gap-writer.md` (at the SESSION CWD)

- [ ] **Step 1: Replace the agent file**

Overwrite `<repo-root>/.claude/agents/gap-writer.md`:

````markdown
---
name: gap-writer
description: Writes ONE custom cold email for ONE US business by EDITING a base template around the structured gap record produced upstream by the Stage 5.6 gap-finder. Reads the base template + voice-us.md, builds the evidence opener, offer line, and CTA fresh for this firm, then writes the email. Dispatched in parallel (one per lead) by the /fire orchestrator on custom-draft runs. Uses Read, Write only.
model: haiku
tools: Read, Write
---

# Gap-writer: edit the template into one custom email

The gap is already found (Stage 5.6 handed it to you). You do NOT search or
re-derive it. You EDIT the base template into a human email for ONE business,
built on the gap record in your batch file, then exit.

## Step 0: read the voice spec AND the template (REQUIRED, first)
`Read` both, follow exactly:
- `<repo-root>/project/vault/lead-outreach/voice-us.md`
- the `Template:` path given in your batch file.
If either is missing, abort and report. Do not improvise voice or structure.

## Input (from orchestrator)
```
LeadId, LeadSlug, Business, Vertical, Website
Contact: Mr./Mrs. <Surname>  (first=… last=…)
ContactEmail
Gap: <gap_type>            the workflow step done by hand (NOT the contact form)
WorkflowStep: <where it sits>
Evidence: <one sentence>
EvidenceQuote: <verbatim string from their site>
Fill: <how an assistant removes that step>
Template: <abs path to email-template-us.md>
OutputFile: <abs path to gap-out-NNN.json>
```

## Workflow
1. Read voice-us.md and the Template.
2. Build PARA 1 (evidence) on `EvidenceQuote` + `WorkflowStep`: name the manual
   step this firm does by hand. Never open on "Your contact form".
3. Build PARA 2 (the offer line) on `Fill`: how a quiet assistant removes THAT
   step, in their workflow's terms. Write it fresh; no stock sentence.
4. Write the CTA fresh: one ask, two loose times, no calendar link.
5. Subject: 3 to 7 words, lowercase, tied to `Gap`, no "AI".
6. Write the OutputFile (schema below).
7. Reply one line: `Done: wrote <OutputFile>`.

## Hard guards (re-check before Write)
- Salutation exactly `Hello Mr. <Surname>,` or `Hello Mrs. <Surname>,`.
- Zero money words: no price, fee, retainer, commission, cost, "free", "$".
- Zero em/en-dashes (use commas / "to"). Any dash means the merge DROPS your email.
- Signature present with the `automatelb.com` link. Body under 125 words.
- The opening must name something real about THIS firm (use `EvidenceQuote`), not
  true of its whole vertical. Do not reuse a sentence another firm could get.
- Do NOT open on "Your contact form" / "The contact form" / "shared inbox". Name
  the workflow step instead.

## Output schema (write EXACTLY this; no extra keys, no `body_html`, merge builds it)
```json
{
  "lead_id": "web-reyeslawfirm-com",
  "lead_slug": "reyeslawfirm-com",
  "to_email": "carlos@morenolawgroup.com",
  "to_name": "Carlos Moreno",
  "subject": "the new matters waiting on intake",
  "body_text": "Hello Mr. Moreno,\n\n<para 1>\n\n<para 2>\n\n<CTA>\n\nDavid Geha\nAutomate, automatelb.com",
  "gap": "<echo the gap_type you were given>",
  "evidence": "<one sentence: the real signal you used>",
  "fill": "<one sentence: how the assistant removes that step>"
}
```

## Don'ts
Don't search the web or read scraped HTML (the gap-finder already did). Don't
re-derive the gap. Don't reuse phrasing across firms (no stock opener, offer
line, or CTA). Don't put price or "AI" in the subject. Don't call any tool other
than Read, Write. You are the leaf.
````

- [ ] **Step 2: Verify tools narrowed and no-search rule present**

Run:
```bash
cd <repo-root>
grep -E "^tools: Read, Write$" .claude/agents/gap-writer.md && echo "tools OK"
grep -c "already found" .claude/agents/gap-writer.md
grep -c "WebSearch" .claude/agents/gap-writer.md
grep -nE "[\xe2\x80\x94\xe2\x80\x93]" .claude/agents/gap-writer.md && echo "DASH FOUND — fix it" || echo "no dashes OK"
```
Expected: `tools OK`; the "already found" count is `1`; the `WebSearch` count is `0` (the writer no longer searches); the dash grep prints `no dashes OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/gap-writer.md
git commit -m "refactor: gap-writer edits template from gap record, Read/Write only"
```

---

## Task 7: Rework `draft_custom.py` prep phase

**Files:**
- Modify: `project/tools/scripts/draft_custom.py` (prep half + headers)
- Test: `project/tests/test_draft_custom.py` (prep portion)

> The full file is rewritten across Tasks 7 and 8. Task 7 lands the prep half and
> the new imports/constants; Task 8 lands the merge gate. Apply them in order.

- [ ] **Step 1: Write the failing prep test**

Create `project/tests/test_draft_custom.py`:

```python
"""Tests for draft_custom.py (Stage 6 custom draft)."""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import draft_custom as dc


def _enriched(**kw):
    base = {"lead_id": "web-x-com", "lead_slug": "x-com", "name": "X LLP",
            "website": "https://x.com", "vertical": "law-firms",
            "contact_title": "Mr.", "contact_first_name": "Jo", "contact_last_name": "Roe",
            "contact_email": "jo@x.com", "gap_type": "deadline triage",
            "workflow_step": "intake", "gap_evidence": "ev", "gap_evidence_quote": "q",
            "gap_fill": "fill"}
    base.update(kw)
    return base


def test_build_gap_batch_carries_gap_record(tmp_path):
    text = dc.build_gap_batch(_enriched(), tmp_path / "gap-out-001.json")
    assert "Gap: deadline triage" in text
    assert "EvidenceQuote: q" in text
    assert "Fill: fill" in text
    assert "Template:" in text
    assert "OutputFile:" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_draft_custom.py -v`
Expected: FAIL — `AttributeError: module 'draft_custom' has no attribute 'build_gap_batch'` (the current file builds batches from HTML, not a gap record).

- [ ] **Step 3: Replace the top + prep half of `draft_custom.py`**

Replace everything from the top of the file down to and including the current
`phase_prep()` definition (the docstring, imports, arg-parse, the `WITH_CONTACT/
RAW/OUT` constants, `PAGE_PRIORITY`, `load_with_contact`, `domain_of`,
`html_files_for`, and `phase_prep`) with:

```python
#!/usr/bin/env python3
"""Stage 6 (CUSTOM draft mode) — per-business gap-based email drafting.

Reworked 2026-06-15: the gap is found upstream by Stage 5.6 (gap-finder ->
gaps-enriched.json). This script no longer hands the writer raw HTML; it hands
each gap-writer the enriched gap record + the base template path. The writer
EDITS the template per business. The merge enforces the contract AND a
recalibrated anti-sameness gate (word-5-gram similarity, banned openers,
lazy-gap drop) plus a HARD em-dash guarantee.

  prep   reads gaps-enriched.json, writes one gap-batch-NNN.txt per enriched lead
         (lead + contact + gap record + template path + OutputFile).
  merge  reads gap-out-*.json, joins to leads-with-contact.json, enforces the
         contract + anti-sameness gate, writes emails-drafted.json. Halts (exit 5)
         if zero drafts survive.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_guards import strip_dashes, has_dash

ROOT = None  # set in __main__
TEMPLATE = (Path(__file__).resolve().parents[2] / "vault" / "lead-outreach"
            / "email-template-us.md")


def load_with_contact(root: Path) -> list[dict]:
    leads = []
    for line in (root / "leads-with-contact.json").read_text().splitlines():
        if line.strip():
            leads.append(json.loads(line))
    return leads


def load_enriched(root: Path) -> list[dict]:
    enriched_path = root / "gaps-enriched.json"
    if not enriched_path.exists():
        raise SystemExit("ABORT: gaps-enriched.json missing (run Stage 5.6 first).")
    out = []
    for line in enriched_path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def build_gap_batch(lead: dict, out_path: Path) -> str:
    title = lead.get("contact_title", "")
    first = lead.get("contact_first_name", "")
    last = lead.get("contact_last_name", "")
    return (
        f"LeadId: {lead['lead_id']}\n"
        f"LeadSlug: {lead['lead_slug']}\n"
        f"Business: {lead['name']}\n"
        f"Vertical: {lead.get('vertical','')}\n"
        f"Website: {lead['website']}\n"
        f"Contact: {title} {last}  (first={first} last={last})\n"
        f"ContactEmail: {lead.get('contact_email','')}\n"
        f"Gap: {lead.get('gap_type','')}\n"
        f"WorkflowStep: {lead.get('workflow_step','')}\n"
        f"Evidence: {lead.get('gap_evidence','')}\n"
        f"EvidenceQuote: {lead.get('gap_evidence_quote','')}\n"
        f"Fill: {lead.get('gap_fill','')}\n"
        f"Template: {TEMPLATE}\n"
        f"OutputFile: {out_path}\n"
    )


def phase_prep(root: Path) -> None:
    enriched = load_enriched(root)
    for f in root.glob("gap-batch-*.txt"):
        f.unlink()
    for f in root.glob("gap-out-*.json"):
        f.unlink()
    for i, lead in enumerate(enriched, start=1):
        nnn = f"{i:03d}"
        (root / f"gap-batch-{nnn}.txt").write_text(
            build_gap_batch(lead, root / f"gap-out-{nnn}.json")
        )
    print(f"Prepped {len(enriched)} gap-writer batches in {root}")
    if not TEMPLATE.exists():
        print(f"  WARNING: base template missing at {TEMPLATE}")
    print("Dispatch this many parallel gap-writer Haiku agents from the orchestrator.")
```

Leave the existing `phase_merge()` in place for now (Task 8 replaces it). At the
very bottom of the file, the current dispatch is:

```python
if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
```

Replace it with:

```python
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--phase", required=True, choices=["prep", "merge"])
    p.add_argument("--run-dir", required=True)
    a = p.parse_args()
    root = Path(a.run_dir).resolve()
    if a.phase == "prep":
        phase_prep(root)
    else:
        phase_merge(root)
```

Leave the old `phase_merge` body untouched here; Task 8 replaces it wholesale.
Importing `draft_custom` does NOT execute either phase (the dispatch is under
`if __name__ == "__main__"`), so the prep test imports and passes fine even with
the stale merge present. Do NOT invoke `--phase merge` until Task 8 lands. Run
only the prep test now: `pytest tests/test_draft_custom.py::test_build_gap_batch_carries_gap_record -v`.

- [ ] **Step 4: Run the prep test**

Run: `pytest tests/test_draft_custom.py::test_build_gap_batch_carries_gap_record -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/scripts/draft_custom.py tests/test_draft_custom.py
git commit -m "refactor: draft_custom prep reads gaps-enriched + template"
```

---

## Task 8: Rework `draft_custom.py` merge gate

**Files:**
- Modify: `project/tools/scripts/draft_custom.py` (replace `phase_merge` + add gate funcs)
- Test: `project/tests/test_draft_custom.py` (add gate tests)

- [ ] **Step 1: Add the failing gate tests**

Append to `project/tests/test_draft_custom.py`:

```python
def test_word_ngrams_and_jaccard_catch_shared_pitch():
    a = ("we set up an assistant that reads new matters as they land and books "
         "the consult straight into your calendar")
    b = ("we set up an assistant that reads new patients as they land and books "
         "the visit straight into your calendar")
    c = ("a recruiter on your team chases interview confirmations across three "
         "inboxes every single afternoon without any help")
    assert dc.ngram_jaccard(a, b) > 0.40   # shared skeleton -> high
    assert dc.ngram_jaccard(a, c) < 0.10   # genuinely different -> low


def test_is_banned_opener():
    body = "Hello Mr. Roe,\n\nYour contact form sends every inquiry to a shared inbox."
    assert dc.is_banned_opener(body)
    ok = "Hello Mr. Roe,\n\nDeadline-driven divorce filings sit in your intake queue."
    assert not dc.is_banned_opener(ok)


def test_is_lazy_gap():
    assert dc.is_lazy_gap("contact form")
    assert dc.is_lazy_gap("a shared inbox")
    assert not dc.is_lazy_gap("deadline-driven new-matter intake triage")
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_draft_custom.py -k "ngrams or banned or lazy" -v`
Expected: FAIL — `AttributeError` (functions not defined yet).

- [ ] **Step 3: Replace `phase_merge` and add the gate functions**

Replace the entire existing `phase_merge` (the old version that uses
character-trigram Jaccard) with the gate constants/functions + new merge below.
Insert these definitions ABOVE `phase_merge`:

```python
MONEY_RE = re.compile(
    r"\$|\b(price|pricing|priced|fee|fees|retainer|commission|monthly fee|"
    r"per month|/month|cost|costs|free setup|no monthly|charge|charges|"
    r"dollar|dollars|usd|invoice|subscription)\b",
    re.IGNORECASE,
)
AI_SUBJECT_RE = re.compile(r"\bai\b", re.IGNORECASE)

GENERIC_LOCAL_PARTS = {
    "info", "contact", "hello", "inquiries", "enquiries", "enquiry",
    "support", "admin", "office", "general", "careers", "career", "hr",
    "jobs", "recruit", "recruitment", "marketing", "press", "media", "pr",
    "comms", "sales", "bookings", "booking", "reservations", "reservation",
    "appointments", "appointment", "frontdesk", "reception", "manager",
    "operations", "banqueting", "catering", "events", "concierge",
    "no-reply", "noreply", "donotreply",
}

BANNED_OPENERS = (
    "your contact form", "the contact form", "has a contact form",
    "uses a shared inbox", "we'll get back to you", "we will get back to you",
    "no online booking", "phone-only intake", "uses info@", "uses office@",
    "your website has a contact form",
)

LAZY_GAP_RE = re.compile(
    r"^\s*(a |an |the )?(contact form|shared inbox|info@|office@|general inquir)",
    re.IGNORECASE,
)

SIMILARITY_THRESHOLD = 0.40  # word-5-gram Jaccard above this -> near-duplicate body


def is_direct_email(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return local not in GENERIC_LOCAL_PARTS


def is_banned_opener(body: str) -> bool:
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    opener = (lines[1] if len(lines) > 1 else (lines[0] if lines else "")).lower()
    return any(opener.startswith(b) for b in BANNED_OPENERS)


def is_lazy_gap(gap: str) -> bool:
    g = (gap or "").strip()
    if not g:
        return False
    return bool(LAZY_GAP_RE.match(g)) and len(g.split()) <= 6


def word_ngrams(text: str, n: int = 5) -> set:
    words = re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()).split()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def ngram_jaccard(a: str, b: str, n: int = 5) -> float:
    ga, gb = word_ngrams(a, n), word_ngrams(b, n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def body_core(body: str) -> str:
    lines = body.splitlines()
    core_lines = lines[1:-3] if len(lines) > 4 else lines[1:]
    return " ".join(core_lines)


def phase_merge(root: Path) -> None:
    leads = load_with_contact(root)
    by_id = {l["lead_id"]: l for l in leads}
    run_slug = root.name

    drafts = []
    stats = {"skipped": 0, "parse_errors": 0, "dropped_salutation": 0,
             "dropped_money": 0, "dropped_no_link": 0, "dropped_no_email": 0,
             "dropped_no_lead": 0, "dropped_banned_opener": 0,
             "dropped_lazy_gap": 0, "dropped_dash": 0, "dropped_similar": 0}

    for f in sorted(root.glob("gap-out-*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            stats["parse_errors"] += 1
            continue
        if data.get("skip"):
            stats["skipped"] += 1
            continue
        lead = by_id.get(data.get("lead_id"))
        if not lead:
            stats["dropped_no_lead"] += 1
            continue

        to_email = (data.get("to_email") or lead.get("contact_email") or "").strip().lower()
        if not is_direct_email(to_email):
            stats["dropped_no_email"] += 1
            continue

        subject = strip_dashes((data.get("subject") or "").strip())
        body_text = strip_dashes((data.get("body_text") or "").strip())

        last = lead.get("contact_last_name", "").strip()
        title = lead.get("contact_title", "").strip()
        if not body_text.startswith(f"Hello {title} ") or last not in body_text.split("\n", 1)[0]:
            stats["dropped_salutation"] += 1
            continue
        if MONEY_RE.search(body_text) or MONEY_RE.search(subject):
            stats["dropped_money"] += 1
            continue
        if "automatelb.com" not in body_text:
            stats["dropped_no_link"] += 1
            continue
        if is_banned_opener(body_text):
            stats["dropped_banned_opener"] += 1
            continue
        if is_lazy_gap(data.get("gap", "")):
            stats["dropped_lazy_gap"] += 1
            continue
        # HARD em-dash restriction: strip ran above; if any dash survives, drop.
        if has_dash(subject) or has_dash(body_text):
            stats["dropped_dash"] += 1
            continue

        subject = AI_SUBJECT_RE.sub("the", subject).strip()
        paragraphs = body_text.split("\n\n")
        body_html = "\n".join(f"<p>{para.replace(chr(10), '<br>')}</p>" for para in paragraphs)

        drafts.append({
            "lead_id": lead["lead_id"],
            "lead_slug": lead["lead_slug"],
            "to_email": to_email,
            "to_name": f"{lead.get('contact_first_name','')} {last}".strip(),
            "salutation": f"{title} {last}",
            "contact_first_name": lead.get("contact_first_name", ""),
            "contact_last_name": last,
            "contact_title": title,
            "contact_role": lead.get("contact_role", ""),
            "contact_source_url": lead.get("contact_source_url", ""),
            "contact_email": to_email,
            "contact_email_source_url": lead.get("contact_email_source_url", ""),
            "contact_confidence": lead.get("contact_confidence", ""),
            "business_name": lead["name"],
            "scraped_inbox": lead.get("to_email", ""),
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
            "tags": ["cold-outreach", run_slug, lead.get("vertical", ""), lead.get("country_code", "")],
            "score": lead.get("score", 0),
            "qualification_status": "send_ready",
            "funnel_status": "custom_gap",
            "signal_used": lead.get("signal", ""),
            "primary_gap": data.get("gap", lead.get("gap_type", "")),
            "gap": data.get("gap", lead.get("gap_type", "")),
            "evidence": data.get("evidence", lead.get("gap_evidence", "")),
            "fill": data.get("fill", lead.get("gap_fill", "")),
            "email_class": "direct_person",
            "send_gate": "pass",
            "country_code": lead.get("country_code", ""),
            "vertical": lead.get("vertical", ""),
        })

    # Anti-sameness gate: drop a draft whose body core is too close (word-5-gram
    # Jaccard) to one already kept. Word-5-grams catch a shared pitch sentence
    # even when firm names differ, which char-trigrams did not.
    kept, kept_cores = [], []
    for d in sorted(drafts, key=lambda x: -x["score"]):
        core = body_core(d["body_text"])
        if any(ngram_jaccard(core, kc) > SIMILARITY_THRESHOLD for kc in kept_cores):
            stats["dropped_similar"] += 1
            continue
        kept.append(d)
        kept_cores.append(core)
    drafts = kept

    (root / "emails-drafted.json").write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")

    counter = Counter()
    for d in drafts:
        for g in word_ngrams(body_core(d["body_text"]), 7):
            counter[g] += 1
    print(f"Custom-drafted {len(drafts)} emails (gap-writer path).")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    if drafts and counter:
        print("  top shared 7-grams (sameness watch):")
        for gram, c in counter.most_common(5):
            if c > 1:
                print(f"    {c}x  {' '.join(gram)}")
    if not drafts:
        print("ABORT: custom draft produced 0 emails after the contract + sameness gate.")
        raise SystemExit(5)
```

- [ ] **Step 4: Run the gate tests**

Run: `pytest tests/test_draft_custom.py -v`
Expected: PASS (all draft_custom tests)

- [ ] **Step 5: Add a merge integration test**

Append to `project/tests/test_draft_custom.py`:

```python
import json as _json


def _write_lead_and_gapout(root, lead_id, last, body, gap="deadline triage", subject="the matters waiting"):
    wc = root / "leads-with-contact.json"
    line = _json.dumps({"lead_id": lead_id, "lead_slug": lead_id, "name": "X LLP",
                        "website": "https://x.com", "vertical": "law-firms", "score": 80,
                        "country_code": "US", "contact_title": "Mr.",
                        "contact_first_name": "Jo", "contact_last_name": last,
                        "contact_email": f"jo@{lead_id}.com"})
    prev = wc.read_text() if wc.exists() else ""
    wc.write_text(prev + line + "\n")
    idx = len(list(root.glob("gap-out-*.json"))) + 1
    (root / f"gap-out-{idx:03d}.json").write_text(_json.dumps({
        "lead_id": lead_id, "to_email": f"jo@{lead_id}.com", "subject": subject,
        "body_text": body, "gap": gap, "evidence": "e", "fill": "f"}))


def test_merge_drops_near_identical_bodies(tmp_path):
    sig = "\n\nDavid Geha\nAutomate, automatelb.com"
    body_a = ("Hello Mr. Roe,\n\nYour divorce intake queue stacks up every morning.\n\n"
              "We set up an assistant that reads new matters as they land and books the "
              "consult straight into your calendar.\n\nWould Monday or Wednesday suit a quick call?" + sig)
    body_b = ("Hello Mr. Poe,\n\nYour custody intake queue stacks up every morning.\n\n"
              "We set up an assistant that reads new matters as they land and books the "
              "consult straight into your calendar.\n\nWould Monday or Wednesday suit a quick call?" + sig)
    _write_lead_and_gapout(tmp_path, "web-a-com", "Roe", body_a)
    _write_lead_and_gapout(tmp_path, "web-b-com", "Poe", body_b)
    dc.phase_merge(tmp_path)
    out = [l for l in (tmp_path / "emails-drafted.json").read_text().splitlines() if l.strip()]
    assert len(out) == 1   # the second, near-identical body is dropped


def test_merge_drops_dash_and_keeps_clean(tmp_path):
    sig = "\n\nDavid Geha\nAutomate, automatelb.com"
    clean = ("Hello Mr. Roe,\n\nYour divorce intake queue stacks up by 9am each day.\n\n"
             "An assistant logs each new matter and slots the consult into your calendar.\n\n"
             "Would Monday or Wednesday suit a quick call?" + sig)
    _write_lead_and_gapout(tmp_path, "web-a-com", "Roe", clean)
    dc.phase_merge(tmp_path)
    text = (tmp_path / "emails-drafted.json").read_text()
    assert "web-a-com" in text
    assert "—" not in text and "–" not in text
```

- [ ] **Step 6: Run the full draft_custom suite**

Run: `pytest tests/test_draft_custom.py -v`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add tools/scripts/draft_custom.py tests/test_draft_custom.py
git commit -m "feat: draft_custom recalibrated gate (word-5-gram, banned opener, lazy-gap, hard dash)"
```

---

## Task 9: Close the dash hole in `draft_emails.py` (template mode)

**Files:**
- Modify: `project/tools/scripts/draft_emails.py`
- Test: `project/tests/test_draft_custom.py` is custom-only; add `project/tests/test_draft_emails_dash.py`

The template path defines `_strip_dashes` but never applies it to the final
subject/body. Wire in the shared guard so "no dashes in any email" holds for GCC/
LB/worldwide runs too.

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_draft_emails_dash.py`:

```python
"""The template-mode draft must also emit zero dashes."""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from text_guards import strip_dashes, has_dash


def test_template_path_uses_shared_guard():
    # draft_emails imports the shared guard (proves the hole is wired closed).
    src = (PROJECT / "tools" / "scripts" / "draft_emails.py").read_text()
    assert "from text_guards import" in src
    assert "strip_dashes(subject)" in src
    assert "has_dash(" in src


def test_guard_clears_em_dash():
    assert not has_dash(strip_dashes("Automate — automatelb.com"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_draft_emails_dash.py -v`
Expected: FAIL on `test_template_path_uses_shared_guard` (import/usage not present yet).

- [ ] **Step 3: Edit `draft_emails.py`**

a) Near the top of the file, after the existing `import` lines, add:

```python
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from text_guards import strip_dashes, has_dash
```

(If `from pathlib import Path` is not already imported above this point, add it.)

b) Delete the now-redundant local dash helper (current lines 93-99):

```python
# Hard rule: no em-dashes anywhere in body copy (em-dash = U+2014).
# Replace stray em-dashes with commas before draft serialization. en-dashes also.
_DASH_RE = re.compile(r"\s*[—–]\s*")


def _strip_dashes(s: str) -> str:
    return _DASH_RE.sub(", ", s)
```

c) In `draft()`, right after `body_text = BODY_TEMPLATE.format_map(fmt)` (current
line 121), add:

```python
    subject = strip_dashes(subject)
    body_text = strip_dashes(body_text)
```

d) Replace the draft loop (current lines 194-207) so the hard guard drops any
draft that still contains a dash:

```python
drafts = []
dropped_no_contact = 0
dropped_no_direct_email = 0
dropped_dash = 0
for l in leads:
    first = l.get("contact_first_name", "").strip()
    last = l.get("contact_last_name", "").strip()
    title = l.get("contact_title", "").strip()
    if not first or not last or title not in {"Mr.", "Mrs."}:
        dropped_no_contact += 1
        continue
    if not is_direct_email(l.get("contact_email", "")):
        dropped_no_direct_email += 1
        continue
    d = draft(l)
    if has_dash(d["subject"]) or has_dash(d["body_text"]):
        dropped_dash += 1
        continue
    drafts.append(d)
```

e) After the existing two `dropped_*` prints (current lines 212-213), add:

```python
print(f"  dropped em/en-dash (hard guard):  {dropped_dash}  (should be 0 after strip)")
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_draft_emails_dash.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tools/scripts/draft_emails.py tests/test_draft_emails_dash.py
git commit -m "fix: apply shared dash guard in template-mode draft (closes hole)"
```

---

## Task 10: Wire Stage 5.6 into the orchestrator (`fire.md`)

**Files:**
- Modify: `project/.claude/commands/fire.md`

- [ ] **Step 1: Add the pre-flight check for the custom path**

In Step 0 (pre-flight), after the `name-finder` check block, add:

```bash
# Custom-draft runs need gap-finder + gap-writer + the base template (Stage 5.6/6)
if [ -f "$RUN/draft_mode.txt" ] && [ "$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')" = "custom" ]; then
    for need in .claude/agents/gap-finder.md .claude/agents/gap-writer.md \
                project/vault/lead-outreach/email-template-us.md \
                project/vault/lead-outreach/voice-us.md; do
        [ -f "<repo-root>/$need" ] \
            || { echo "ABORT: custom run missing $need"; exit 1; }
    done
fi
```

(Note: `$RUN` is resolved in Step 1; if pre-flight runs before Step 1, move this
check to the top of Step 7b instead. Keep it wherever `$RUN` is defined.)

- [ ] **Step 2: Insert the new Stage 5.6 step between Step 6.5 and Step 7**

After Step 6.5 (contact enrichment merge) and before Step 7 (draft), insert:

````markdown
### Step 6.6 — gap enrichment (Stage 5.6, CUSTOM runs only)

Runs ONLY when `draft_mode.txt` = `custom`. One Haiku `gap-finder` per business
finds a distinct, workflow-specific gap (no email). Mirrors the Step 6.5 fan-out
exactly: foreground, ≤50 Agent calls per assistant message, never
`run_in_background`. Template runs skip this step entirely.

```bash
DRAFT_MODE="template"
[ -f "$RUN/draft_mode.txt" ] && DRAFT_MODE=$(head -1 "$RUN/draft_mode.txt" | tr -d ' \n')

if [ "$DRAFT_MODE" = "custom" ]; then
    python3 tools/scripts/enrich_gap.py --phase prep --run-dir "$RUN"
    GAPFIND_BATCHES=$(ls "$RUN"/gapfind-batch-*.txt | wc -l | tr -d ' ')
    echo "Will dispatch $GAPFIND_BATCHES parallel gap-finder sub-agents."
fi
```

If custom, **issue the `Agent` tool calls FOREGROUND, up to 50 per assistant
message** — one per `gapfind-batch-NNN.txt`. Each call:

- **subagent_type**: `gap-finder`
- **model**: `haiku`
- **run_in_background**: omit it (foreground)
- **prompt**: the contents of `gapfind-batch-NNN.txt` verbatim.

Do NOT inline the gap-finder instructions — they live in `.claude/agents/gap-finder.md`.

Then merge (halts with exit 9 if too few gaps — kill-on-fallback):

```bash
if [ "$DRAFT_MODE" = "custom" ]; then
    python3 tools/scripts/enrich_gap.py --phase merge --run-dir "$RUN"
    ENRICHED_GAPS=$(wc -l < "$RUN/gaps-enriched.json")
    echo "Gaps enriched: $ENRICHED_GAPS"
fi
```
````

- [ ] **Step 3: Update Step 7b to drop the HTML/vertical prep references**

In Step 7b, the `draft_custom.py --phase prep` now reads `gaps-enriched.json`
(produced in Step 6.6), not the scraped HTML. Replace the Step 7b Phase A prose
"One `gap-writer` Haiku sub-agent writes a fully custom email per firm, built on
the specific manual inbox/scheduling gap it finds on that firm's scraped pages."
with:

```markdown
**Template-edit, gap pre-found.** Stage 5.6 (Step 6.6) already produced
`gaps-enriched.json`. One `gap-writer` Haiku sub-agent per business EDITS the base
template (`vault/lead-outreach/email-template-us.md`) around that business's gap
record. The writer does not search or read HTML — it uses the supplied gap.
```

Keep the existing `vertical.txt` ABORT guard. Keep the gap-writer dispatch shape
(subagent_type `gap-writer`, haiku, foreground). Keep the Phase B merge block.

- [ ] **Step 4: Update the halt table and final report**

In the halt table, add a row after the Enrich row:

```markdown
| Gap enrich (Stage 5.6, custom only) | < 40% of leads yield a concrete gap → `gaps-enriched.json` too small | exit 9 |
```

In the Step 10 final report template, add after the `survived enrichment` line:

```
  gap-finder agents dispatched : $GAPFIND_BATCHES   (custom runs only)
  gaps enriched                : $ENRICHED_GAPS     (custom runs only)
```

And add `gap-finder` to the "Never inline … instructions" hard rule list.

- [ ] **Step 5: Verify**

Run:
```bash
cd <repo-root>/project
grep -c "Step 6.6 — gap enrichment" .claude/commands/fire.md
grep -c "gap-finder" .claude/commands/fire.md
grep -c "enrich_gap.py" .claude/commands/fire.md
```
Expected: the new step heading count is `1`; `gap-finder` appears at least `3` times; `enrich_gap.py` at least `2` times.

- [ ] **Step 6: Commit**

```bash
git add .claude/commands/fire.md
git commit -m "feat: wire Stage 5.6 gap enrichment into /fire orchestrator"
```

---

## Task 11: Update the docs (`PIPELINE.md`, both `CLAUDE.md`)

**Files:**
- Modify: `project/PIPELINE.md`
- Modify: `CLAUDE.md` (session root) and `project/CLAUDE.md`

- [ ] **Step 1: Update the root `CLAUDE.md` pipeline diagram + draft modes**

In `CLAUDE.md`, in the pipeline ASCII diagram, replace the custom-draft branch:

```
      [CUSTOM]   draft_custom.py prep → gap-batch-NNN.txt
               → PARALLEL: gap-writer Haiku agents (foreground, ≤50 per message)
               → draft_custom.py merge → emails-drafted.json (contract-enforced)
```

with:

```
      [CUSTOM]   enrich_gap.py prep → gapfind-batch-NNN.txt
               → PARALLEL: gap-finder Haiku agents → gaps-enriched.json (distinct per-business gap)
               → draft_custom.py prep → gap-batch-NNN.txt (gap record + template)
               → PARALLEL: gap-writer Haiku agents (edit template per business)
               → draft_custom.py merge → emails-drafted.json (contract + anti-sameness gate)
```

In the "Draft modes" section, replace the **custom** bullet with this concrete text:

```markdown
- **custom**: a two-stage gap-based path. Stage 5.6 `enrich_gap.py` fans out one
  `gap-finder` Haiku per business to find a distinct, workflow-specific gap
  (`gaps-enriched.json`); the contact form / shared inbox is never the gap. Then
  `draft_custom.py` (prep + merge) fans out one `gap-writer` Haiku per business
  that EDITS the base template `vault/lead-outreach/email-template-us.md` around
  that business's gap record (offer line and CTA rewritten per firm, no shared
  phrasing). The merge enforces the contract (Mr./Mrs.+surname salutation, direct
  email, no money words, `automatelb.com` link) AND an anti-sameness gate
  (word-5-gram similarity, banned-opener + lazy-gap drops) AND a hard no-em-dash
  guarantee, dropping anything non-compliant. Voice spec: `voice-us.md`.
```

In the Sub-agents table, add a `gap-finder` row:

```
| `gap-finder` | Read, WebSearch, WebFetch, Write | Find ONE distinct workflow gap per US business (Stage 5.6); no email |
```

And update the `gap-writer` row tools to `Read, Write` and role to "Edit the base
template into a custom email from the enriched gap record".

Add the new script invocations under "Running the pipeline":

```bash
# Stage 5.6 — gap enrichment (custom runs)
python3 tools/scripts/enrich_gap.py --phase prep --run-dir runs/<slug>
# (dispatch gap-finder Haiku agents per gapfind-batch-NNN.txt)
python3 tools/scripts/enrich_gap.py --phase merge --run-dir runs/<slug>
```

- [ ] **Step 2: Update `project/CLAUDE.md`**

In `project/CLAUDE.md`, the ICP-variants and draft-mode notes mention the custom
gap-writer path. Add one sentence to the US row / custom-mode note: "Custom runs
first run Stage 5.6 gap enrichment (`gap-finder` → `gaps-enriched.json`), then the
`gap-writer` edits `email-template-us.md` per business; the merge enforces an
anti-sameness gate and a hard no-em-dash guarantee."

- [ ] **Step 3: Update `project/PIPELINE.md`**

Between the Stage 5.5 (contact enrichment) and Stage 6 (draft) sections, insert
this Stage 5.6 section verbatim:

```markdown
## Stage 5.6 — Gap enrichment (CUSTOM runs only)

**Runs only when** `runs/<slug>/draft_mode.txt` = `custom`. Template runs skip it.

**In:** `leads-with-contact.json` (Stage 5.5) + `raw_html/` (Stage 4).
**Out:** `gaps-enriched.json` (JSONL, one record per business with a concrete gap).

`enrich_gap.py --phase prep` writes one `gapfind-batch-NNN.txt` per lead (lead +
contact fields + the abs paths of that domain's scraped pages). The orchestrator
dispatches one Haiku `gap-finder` per batch (foreground, <=50/message). Each
gap-finder reads the pages and writes one `gapfind-out-NNN.json` naming the gap:

- `gap_type` — the manual WORKFLOW step done by hand (law: deadline-driven
  new-matter triage; staffing: interview coordination; clinics: new-patient
  insurance verification; property: maintenance-to-vendor dispatch).
- The contact form / shared inbox / generic mailbox is valid as `evidence` but
  FORBIDDEN as `gap_type`. No firm-specific gap -> the agent returns `{skip:true}`.

`enrich_gap.py --phase merge` drops skips, joins the rest to the leads, and writes
`gaps-enriched.json`. **Halt:** if fewer than 40% of leads yield a gap, exit 9
(kill-on-fallback — we do not ship generic emails). Stage 6 custom draft consumes
`gaps-enriched.json`; the `gap-writer` edits `email-template-us.md` around each
record.
```

- [ ] **Step 4: Verify**

Run:
```bash
cd <repo-root>
grep -c "gap-finder" CLAUDE.md project/CLAUDE.md project/PIPELINE.md
grep -c "gaps-enriched.json" CLAUDE.md project/PIPELINE.md
```
Expected: `gap-finder` appears in all three files; `gaps-enriched.json` in both.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md project/CLAUDE.md project/PIPELINE.md
git commit -m "docs: document Stage 5.6 gap enrichment + template-edit draft"
```

---

## Task 12: Full suite + (user-gated) live validation

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run:
```bash
cd <repo-root>/project && source tools/venv/bin/activate
pytest tests/ -v
```
Expected: all tests pass (existing + the new text_guards / enrich_gap / draft_custom / draft_emails_dash tests).

- [ ] **Step 2: Static dry check of the new scripts**

Run:
```bash
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['tools/scripts/enrich_gap.py','tools/scripts/draft_custom.py','tools/scripts/text_guards.py','tools/scripts/draft_emails.py']]; print('parse OK')"
```
Expected: `parse OK`

- [ ] **Step 3: OFFER live validation — do NOT run without explicit approval**

The user instruction for this session is: do NOT start any run. The live
validation below dispatches gap-finder + gap-writer agents on an existing run's
data (no sourcing, no scraping, NO send). Present it to the user and run ONLY if
they approve:

```
Proposed validation (no send): reuse runs/2026-06-13-us-law-firms (leads-with-contact.json + raw_html),
run Stage 5.6 + reworked Stage 6 into a scratch copy, then re-run the repetition
metrics. Success = CTA/offer reuse < ~30%, no "contact form" openers, gap_type diverse.
```

If approved, copy the existing run to a scratch slug, run prep/merge + the two
fan-outs, and compare. If not approved, stop here — the system fix is complete and
unit-tested.

- [ ] **Step 4: Final commit (if any docs/notes changed during validation)**

```bash
git add -A && git commit -m "chore: validation notes for US custom gap rework" || echo "nothing to commit"
```

---

## Self-review notes

- **Spec coverage:** Stage 5.6 enrichment (Tasks 2,3), template-edit draft (Tasks 4,5,6,7,8), recalibrated merge gate (Task 8), pipeline wiring (Task 10), docs (Task 11), validation (Task 12), hard no-dash (Tasks 1,8,9) — every spec section maps to a task.
- **Em-dash hard restriction (session instruction):** Task 1 (shared guard), Task 8 (custom-path drop guarantee), Task 9 (template-path hole closed) — all three draft surfaces covered.
- **No run started (session instruction):** Task 12 gates the only live dispatch behind explicit user approval; all other tasks are code + unit tests on synthetic fixtures.
- **Type/name consistency:** `gaps-enriched.json` fields (`gap_type`, `workflow_step`, `gap_evidence`, `gap_evidence_quote`, `gap_fill`) are produced by `collect_enriched` (Task 3) and consumed by `build_gap_batch` (Task 7); `strip_dashes`/`has_dash` are defined once (Task 1) and imported in Tasks 8 and 9.
