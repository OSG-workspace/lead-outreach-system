#!/usr/bin/env python3
"""Stage 5.5 + 6 COMBINED (custom draft mode) — one agent per lead does both.

Replaces the old two-step custom path (enrich_contact_person -> name-finder
fan-out, THEN draft_custom -> gap-writer fan-out) with a SINGLE fan-out: one
`lead-writer` Haiku per qualified lead finds the decision-maker AND writes the
custom gap email in one web pass. Halves the agent count and the web-research
token cost on custom runs (the two old agents independently re-fetched the same
domain).

Two phases, both run by the /fire orchestrator:

  prep   reads leads-qualified.json (Stage 5.3 output; falls back to
         leads-extracted.json), writes one lead-batch-NNN.txt per lead with the
         business fields, site-harvested email/phone clues, and the ABSOLUTE
         PATHS of that domain's scraped raw_html pages (the agent has Read, so we
         pass paths, not inlined page text -> far smaller prompts). Orchestrator
         dispatches one lead-writer per batch file (foreground, max parallelism).

  merge  reads all lead-out-*.json, joins to the qualified leads, enforces BOTH
         the contact contract (real Mr./Mrs.+surname, direct non-generic email)
         AND the email contract (salutation, no money words, osgdev.com link,
         no em/en dashes), de-dups near-identical bodies, and writes
         emails-drafted.json. Output schema matches draft_emails.py /
         draft_custom.py so send_batch_brevo.py + persist work unchanged.
         Halts (exit 5) only if zero drafts survive.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from email_utils import harvest_domain, harvest_phone_links
except Exception:  # harvester best-effort; prep still works without it
    harvest_domain = None
    harvest_phone_links = None

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge"])
p.add_argument("--run-dir", required=True)
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
QUALIFIED = ROOT / "leads-qualified.json"
EXTRACTED = QUALIFIED if QUALIFIED.exists() else (ROOT / "leads-extracted.json")
RAW = ROOT / "raw_html"
OUT = ROOT / "emails-drafted.json"
RUN_SLUG = ROOT.name

COUNTRY_NAMES = {"AE": "UAE", "SA": "Saudi Arabia", "QA": "Qatar", "BH": "Bahrain",
                 "KW": "Kuwait", "LB": "Lebanon", "US": "United States"}
PAGE_PRIORITY = ["home", "index", "contact", "contactus", "about", "aboutus",
                 "team", "ourteam", "attorneys", "people", "staff", "services"]


def load_leads() -> list[dict]:
    return [json.loads(l) for l in EXTRACTED.read_text().splitlines() if l.strip()]


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
    return real[:4]


# --- prep -----------------------------------------------------------------

def phase_prep() -> None:
    if not EXTRACTED.exists():
        raise SystemExit("ABORT: leads-qualified.json / leads-extracted.json missing.")
    leads = load_leads()
    for f in ROOT.glob("lead-batch-*.txt"):
        f.unlink()
    for f in ROOT.glob("lead-out-*.json"):
        f.unlink()

    no_html = 0
    for i, lead in enumerate(leads, start=1):
        nnn = f"{i:03d}"
        batch_path = ROOT / f"lead-batch-{nnn}.txt"
        out_path = ROOT / f"lead-out-{nnn}.json"
        domain = domain_of(lead)
        cc = lead.get("country_code", "")
        country_name = COUNTRY_NAMES.get(cc, cc)

        harvested = {"personal": [], "role": [], "offdomain": [], "freemail": []}
        phone_links: list[str] = []
        if harvest_domain is not None:
            try:
                harvested = harvest_domain(domain, RAW)
            except Exception:
                pass
        if harvest_phone_links is not None:
            try:
                phone_links = harvest_phone_links(domain, RAW)
            except Exception:
                pass
        personal_pool = harvested.get("personal", []) + harvested.get("offdomain", [])

        files = html_files_for(domain)
        if not files:
            no_html += 1
        html_block = "\n".join(f"  {f}" for f in files) if files else "  (none scraped)"

        def _fmt(lst):
            return ", ".join(lst) if lst else "(none found on site)"

        batch_path.write_text(
            f"LeadId: {lead['lead_id']}\n"
            f"Business: {lead['name']}\n"
            f"Country: {cc}  ({country_name})\n"
            f"Vertical: {lead.get('vertical','')}\n"
            f"Website: {lead['website']}\n"
            f"Signal: {lead.get('signal','')}  ({lead.get('signal_evidence','')})\n"
            f"SitePersonalEmails: {_fmt(personal_pool)}\n"
            f"SiteRoleEmails: {_fmt(harvested.get('role', []))}\n"
            f"SitePhoneLinks: {_fmt(phone_links)}\n"
            f"HtmlFiles:\n{html_block}\n"
            f"OutputFile: {out_path}\n"
        )
    print(f"Prepped {len(leads)} lead-writer batches in {ROOT}")
    if no_html:
        print(f"  note: {no_html}/{len(leads)} leads had no scraped pages "
              f"(lead-writer may web-search or skip).")
    print("Dispatch this many parallel lead-writer Haiku agents from the orchestrator.")


# --- merge: contract gates ------------------------------------------------

_DASH_RE = re.compile(r"\s*[—–]\s*")
MONEY_RE = re.compile(
    r"\$|\b(price|pricing|priced|fee|fees|retainer|commission|monthly fee|"
    r"per month|/month|cost|costs|free setup|no monthly|charge|charges|"
    r"dollar|dollars|usd|invoice|subscription)\b", re.IGNORECASE)
AI_SUBJECT_RE = re.compile(r"\bai\b", re.IGNORECASE)

# Every outreach signature carries the Instagram handle. The HTML shell renders
# it as a styled signature line; this keeps the plain-text part (Brevo
# textContent) in sync. Appended, never gated, so no lead is dropped over it.
INSTAGRAM_LINE = "Instagram: dave.automates"
INSTAGRAM_RE = re.compile(r"[Ii]nstagram\s*[:@]?\s*@?dave\.automates")


def with_instagram(body: str) -> str:
    body = body.rstrip()
    return body if INSTAGRAM_RE.search(body) else f"{body}\n{INSTAGRAM_LINE}"


GENERIC_LOCAL_PARTS = {
    "info", "contact", "hello", "inquiries", "enquiries", "enquiry", "support",
    "admin", "office", "general", "careers", "career", "hr", "jobs", "recruit",
    "recruitment", "marketing", "press", "media", "pr", "comms", "sales",
    "bookings", "booking", "reservations", "reservation", "appointments",
    "appointment", "frontdesk", "reception", "manager", "operations",
    "banqueting", "catering", "events", "concierge", "no-reply", "noreply",
    "donotreply",
}
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "msn.com", "aol.com", "proton.me", "protonmail.com",
}


def is_direct_email(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, dom = addr.partition("@")
    if not local or not dom or "." not in dom:
        return False
    return local not in GENERIC_LOCAL_PARTS and dom not in FREE_MAIL_DOMAINS


def phase_merge() -> None:
    leads = load_leads()
    by_id = {l["lead_id"]: l for l in leads}

    drafts = []
    parse_errors = skipped = 0
    d_nolead = d_notfound = d_name = d_email = d_salutation = d_money = d_link = 0

    for f in sorted(ROOT.glob("lead-out-*.json")):
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
        if not data.get("found"):
            d_notfound += 1
            continue

        first = (data.get("first_name") or "").strip()
        last = (data.get("last_name") or "").strip()
        title = (data.get("title") or "").strip()
        # One name component is enough (per CLAUDE.md salutation contract):
        # surname + Mr./Mrs., or a bare first name.
        surname_ok = bool(last) and title in {"Mr.", "Mrs."}
        if not (surname_ok or first):
            d_name += 1
            continue
        if title not in {"Mr.", "Mrs."}:
            title = ""
        salutation = f"{title} {last}" if surname_ok else first

        to_email = (data.get("email") or "").strip().lower()
        if not is_direct_email(to_email):
            d_email += 1
            continue

        subject = _DASH_RE.sub(", ", (data.get("subject") or "").strip())
        body_text = _DASH_RE.sub(", ", (data.get("body_text") or "").strip())

        if not body_text.startswith(f"Hello {salutation},"):
            d_salutation += 1
            continue
        if MONEY_RE.search(body_text) or MONEY_RE.search(subject):
            d_money += 1
            continue
        if "osgdev.com" not in body_text:
            d_link += 1
            continue
        subject = AI_SUBJECT_RE.sub("the", subject).strip()
        body_text = with_instagram(body_text)

        paragraphs = body_text.split("\n\n")
        body_html = "\n".join(f"<p>{para.replace(chr(10), '<br>')}</p>" for para in paragraphs)

        phone = (data.get("phone") or "").strip()
        digits = "".join(ch for ch in phone if ch.isdigit())
        phone_stored = f"+{digits}" if digits else ""

        drafts.append({
            "lead_id": lead["lead_id"],
            "lead_slug": lead["lead_slug"],
            "to_email": to_email,
            "to_name": f"{first} {last}".strip(),
            "salutation": salutation,
            "contact_first_name": first,
            "contact_last_name": last,
            "contact_title": title,
            "contact_role": data.get("role", ""),
            "contact_source_url": data.get("source_url", ""),
            "contact_email": to_email,
            "contact_email_source_url": data.get("email_source_url", "") or data.get("source_url", ""),
            "contact_phone": phone_stored,
            "contact_confidence": data.get("confidence", ""),
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

    # Similarity gate: drop near-identical bodies (>85% trigram Jaccard on core).
    def trigrams(text: str) -> set[str]:
        t = re.sub(r"\s+", " ", text.lower().strip())
        return {t[i:i + 3] for i in range(len(t) - 2)}

    def body_core(body: str) -> str:
        lines = body.splitlines()
        core = lines[1:-3] if len(lines) > 4 else lines[1:]
        return " ".join(core)

    kept: list[dict] = []
    kept_tris: list[set] = []
    dropped_similar = 0
    for d in drafts:
        core = trigrams(body_core(d["body_text"]))
        if any((len(core & e) / len(core | e) if core and e else 0) > 0.85 for e in kept_tris):
            dropped_similar += 1
            continue
        kept.append(d)
        kept_tris.append(core)
    drafts = kept

    drafts.sort(key=lambda d: -d["score"])
    OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")

    print(f"Lead-writer drafted {len(drafts)} emails (combined find+write path).")
    print(f"  skipped by lead-writer (no recipient / no gap): {skipped}")
    print(f"  dropped lead-not-found:        {d_nolead}")
    print(f"  dropped agent found:false:     {d_notfound}")
    print(f"  dropped name/gender-missing:   {d_name}")
    print(f"  dropped no-direct-email:       {d_email}")
    print(f"  dropped salutation-contract:   {d_salutation}")
    print(f"  dropped money-talk:            {d_money}")
    print(f"  dropped missing-link:          {d_link}")
    print(f"  dropped near-identical:        {dropped_similar}")
    print(f"  parse errors:                  {parse_errors}")
    for d in drafts[:5]:
        print(f"\n--- {d['to_email']} | {d['salutation']} ---\nSubject: {d['subject']}\n{d['body_text'][:240]}...")
    if not drafts:
        print("ABORT: combined custom path produced 0 emails after the contract gate.")
        raise SystemExit(5)


if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
