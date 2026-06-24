#!/usr/bin/env python3
"""Stage 6 (CUSTOM draft mode) — per-business gap-based email drafting.

Used INSTEAD of draft_emails.py when a run sets draft_mode=custom (see
runs/<slug>/draft_mode.txt). There is NO template: one Haiku `gap-writer`
sub-agent writes a fully custom email per business, built on the specific
manual inbox/scheduling gap it finds for that business.

Two phases, both run by the /fire orchestrator:

  prep   reads leads-with-contact.json, writes one gap-batch-NNN.txt per lead
         (lead + contact fields + the absolute paths of that domain's scraped
         raw_html pages + an OutputFile path). Orchestrator then dispatches one
         gap-writer Haiku per batch file (max parallelism, foreground).

  merge  reads all gap-out-*.json, joins to leads-with-contact.json, enforces
         the email contract (Mr./Mrs.+surname salutation, direct email only,
         no money words, automatelb.com link present, no em/en dashes), and
         writes emails-drafted.json (the Stage 7 send input). Skips/non-compliant
         drafts are dropped. Halts (exit 5) only if zero drafts survive.

Output schema matches draft_emails.py so send_batch_brevo.py + persist work unchanged.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge"])
p.add_argument("--run-dir", required=True)
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
WITH_CONTACT = ROOT / "leads-with-contact.json"
RAW = ROOT / "raw_html"
OUT = ROOT / "emails-drafted.json"
RUN_SLUG = ROOT.name

# Pages worth reading first (the gap usually shows on these).
PAGE_PRIORITY = ["home", "index", "contact", "contactus", "about", "aboutus",
                 "team", "ourteam", "attorneys", "people", "staff", "services"]


def load_with_contact() -> list[dict]:
    leads = []
    for line in WITH_CONTACT.read_text().splitlines():
        if line.strip():
            leads.append(json.loads(line))
    return leads


def domain_of(lead: dict) -> str:
    d = lead.get("website", "").replace("https://", "").replace("http://", "").strip("/")
    if d.startswith("www."):
        d = d[4:]
    return d


def html_files_for(domain: str) -> list[Path]:
    safe = domain.replace("/", "_")
    files = sorted(RAW.glob(f"{safe}__*.html"))
    # Skip tiny/error pages; rank by PAGE_PRIORITY then keep up to 5.
    real = [f for f in files if f.exists() and f.stat().st_size >= 500]

    def rank(f: Path) -> int:
        slug = f.stem.split("__", 1)[1] if "__" in f.stem else f.stem
        return PAGE_PRIORITY.index(slug) if slug in PAGE_PRIORITY else len(PAGE_PRIORITY)

    real.sort(key=rank)
    return real[:5]


def phase_prep() -> None:
    if not WITH_CONTACT.exists():
        raise SystemExit("ABORT: leads-with-contact.json missing (run Stage 5.5 first).")
    leads = load_with_contact()
    for f in ROOT.glob("gap-batch-*.txt"):
        f.unlink()
    for f in ROOT.glob("gap-out-*.json"):
        f.unlink()

    no_html = 0
    for i, lead in enumerate(leads, start=1):
        nnn = f"{i:03d}"
        batch_path = ROOT / f"gap-batch-{nnn}.txt"
        out_path = ROOT / f"gap-out-{nnn}.json"
        domain = domain_of(lead)
        files = html_files_for(domain)
        if not files:
            no_html += 1
        html_block = "\n".join(f"  {f}" for f in files) if files else "  (none scraped)"
        title = lead.get("contact_title", "")
        first = lead.get("contact_first_name", "")
        last = lead.get("contact_last_name", "")
        batch_path.write_text(
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
    print(f"Prepped {len(leads)} gap-writer batches in {ROOT}")
    if no_html:
        print(f"  note: {no_html}/{len(leads)} leads had no scraped pages (gap-writer may web-search or skip).")
    print("Dispatch this many parallel gap-writer Haiku agents from the orchestrator.")


# --- merge-phase compliance gates ----------------------------------------

_DASH_RE = re.compile(r"\s*[—–]\s*")  # em / en dash -> ", "
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


def is_direct_email(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return False
    return local not in GENERIC_LOCAL_PARTS


def phase_merge() -> None:
    leads = load_with_contact()
    by_id = {l["lead_id"]: l for l in leads}

    drafts = []
    parse_errors = 0
    skipped = 0
    dropped_salutation = 0
    dropped_money = 0
    dropped_no_link = 0
    dropped_no_email = 0
    dropped_no_lead = 0

    for f in sorted(ROOT.glob("gap-out-*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            parse_errors += 1
            continue
        if data.get("skip"):
            skipped += 1
            continue
        lead_id = data.get("lead_id")
        lead = by_id.get(lead_id)
        if not lead:
            dropped_no_lead += 1
            continue

        to_email = (data.get("to_email") or lead.get("contact_email") or "").strip().lower()
        if not is_direct_email(to_email):
            dropped_no_email += 1
            continue

        subject = _DASH_RE.sub(", ", (data.get("subject") or "").strip())
        body_text = _DASH_RE.sub(", ", (data.get("body_text") or "").strip())

        last = lead.get("contact_last_name", "").strip()
        title = lead.get("contact_title", "").strip()
        expected_open = f"Hello {title} {last},"
        if not body_text.startswith(f"Hello {title} ") or last not in body_text.split("\n", 1)[0]:
            dropped_salutation += 1
            continue
        if MONEY_RE.search(body_text) or MONEY_RE.search(subject):
            dropped_money += 1
            continue
        if "automatelb.com" not in body_text:
            dropped_no_link += 1
            continue
        # subject must not contain the literal "AI" buzzword
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
            "tags": ["cold-outreach", RUN_SLUG, lead.get("vertical", ""), lead.get("country_code", "")],
            "score": lead.get("score", 0),
            "qualification_status": "send_ready",
            "funnel_status": "custom_gap",
            "signal_used": lead.get("signal", ""),
            "primary_gap": data.get("gap", lead.get("signal", "")),
            "gap": data.get("gap", ""),
            "evidence": data.get("evidence", ""),
            "fill": data.get("fill", ""),
            "email_class": "direct_person",
            "send_gate": "pass",
            "country_code": lead.get("country_code", ""),
            "vertical": lead.get("vertical", ""),
        })

    # Similarity gate: drop near-identical emails (>85% trigram Jaccard on body core).
    # Strips salutation line and last 3 signature lines before comparing so that
    # name/company differences don't mask identical body copy.
    def trigrams(text: str) -> set[str]:
        t = re.sub(r"\s+", " ", text.lower().strip())
        return {t[i:i+3] for i in range(len(t) - 2)}

    def jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def body_core(body: str) -> str:
        lines = body.splitlines()
        # Strip first line (salutation) and last 3 lines (signature block)
        core_lines = lines[1:-3] if len(lines) > 4 else lines[1:]
        return " ".join(core_lines)

    kept: list[dict] = []
    dropped_similar = 0
    kept_trigrams: list[set] = []
    for draft in drafts:
        core = trigrams(body_core(draft["body_text"]))
        if any(jaccard(core, existing) > 0.85 for existing in kept_trigrams):
            dropped_similar += 1
            continue
        kept.append(draft)
        kept_trigrams.append(core)
    drafts = kept

    drafts.sort(key=lambda d: -d["score"])
    OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")

    print(f"Custom-drafted {len(drafts)} emails (gap-writer path).")
    print(f"  skipped by gap-writer (no concrete gap): {skipped}")
    print(f"  dropped salutation-contract:             {dropped_salutation}")
    print(f"  dropped money-talk:                      {dropped_money}")
    print(f"  dropped missing automatelb.com link:     {dropped_no_link}")
    print(f"  dropped no-direct-email:                 {dropped_no_email}")
    print(f"  dropped lead-not-found:                  {dropped_no_lead}")
    print(f"  dropped near-identical (>85% Jaccard):   {dropped_similar}")
    print(f"  gap-out parse errors:                    {parse_errors}")
    for d in drafts[:5]:
        print(f"\n--- {d['to_email']} | {d['salutation']} ---\nSubject: {d['subject']}\n{d['body_text'][:280]}...")
    if not drafts:
        print("ABORT: custom draft produced 0 emails after the contract gate.")
        raise SystemExit(5)


if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
