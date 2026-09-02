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
         contract (personal salutation: Mr./Mrs.+surname, or bare first name
         when no surname was resolvable; no money words, no em/en dashes,
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
p.add_argument("--work-dir", default=None,
               help="where the per-agent wa-batch-NNN.txt / wa-out-NNN.json files "
                    "live (default: the run dir). Kept OUTSIDE project/ so a sub-agent "
                    "reading a batch file does not pull project/CLAUDE.md into context.")
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
# Per-agent batch/out files go here; every run-level artifact stays in ROOT.
WORK = Path(args.work_dir).resolve() if args.work_dir else ROOT
WORK.mkdir(parents=True, exist_ok=True)
WITH_CONTACT = ROOT / "leads-with-contact.json"
RAW = ROOT / "raw_html"
OUT = ROOT / "whatsapp-drafted.json"
RUN_SLUG = ROOT.name
SENT_LOG = Path(args.sent_log) if args.sent_log else ROOT.parents[1] / "vault" / "lead-outreach" / "sent-log.md"

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
BRAND_RE = re.compile(r"automate|automatelb\.com|\bosg\b|osg-site|osgdev\.com", re.IGNORECASE)

# The personal Instagram handle ships in every outreach signature. It contains
# the substring "automate", so it is stripped out before the brand gate runs,
# otherwise every message would be dropped as a brand mention.
INSTAGRAM_LINE = "Instagram: dave.automates"
INSTAGRAM_RE = re.compile(r"\n?[Ii]nstagram\s*[:@]?\s*@?dave\.automates")


def with_instagram(body: str) -> str:
    body = body.rstrip()
    return body if INSTAGRAM_RE.search(body) else f"{body}\n{INSTAGRAM_LINE}"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WA_PHONE_RE = re.compile(r"wa:(\d{6,15})")
_SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")


def salutation_of(lead: dict) -> str:
    """`Mr./Mrs. <Surname>` when surname + gender exist, else the first name
    (one confirmed name component is enough, per the CLAUDE.md contract)."""
    last = lead.get("contact_last_name", "").strip()
    title = lead.get("contact_title", "").strip()
    first = lead.get("contact_first_name", "").strip()
    return f"{title} {last}" if (last and title in {"Mr.", "Mrs."}) else first


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
    for f in WORK.glob("wa-batch-*.txt"):
        f.unlink()
    for f in WORK.glob("wa-out-*.json"):
        f.unlink()

    kept = 0
    dropped_no_mobile = 0
    for lead in leads:
        title = lead.get("contact_title", "").strip()
        first = lead.get("contact_first_name", "").strip()
        last = lead.get("contact_last_name", "").strip()
        phone = normalize_phone(lead.get("contact_phone", ""), lead.get("country_code", ""))
        # One name component is enough (per CLAUDE.md salutation contract).
        name_ok = (last and title in {"Mr.", "Mrs."}) or first
        if not (name_ok and phone):
            dropped_no_mobile += 1
            continue
        kept += 1
        nnn = f"{kept:03d}"
        out_path = WORK / f"wa-out-{nnn}.json"
        files = html_files_for(domain_of(lead))
        html_block = "\n".join(f"  {f}" for f in files) if files else "  (none scraped)"
        (WORK / f"wa-batch-{nnn}.txt").write_text(
            f"LeadId: {lead['lead_id']}\n"
            f"LeadSlug: {lead['lead_slug']}\n"
            f"Business: {lead['name']}\n"
            f"Vertical: {lead.get('vertical','')}\n"
            f"Website: {lead['website']}\n"
            f"Contact: {title} {last}  (first={first} last={last})\n"
            f"Salutation: Hello {salutation_of(lead)},\n"
            f"HtmlFiles:\n{html_block}\n"
            f"OutputFile: {out_path}\n"
        )
    print(f"Prepped {kept} wa-writer batches in {WORK}")
    print(f"  dropped no valid CEO mobile: {dropped_no_mobile}  (kill-on-fallback)")
    print("Dispatch this many parallel wa-writer Haiku agents from the orchestrator.")


def phase_merge() -> None:
    leads = load_with_contact()
    by_id = {l["lead_id"]: l for l in leads}
    sent_phones, sent_emails, sent_domains, sent_slugs = load_sent_index()

    drafts = []
    skipped = parse_errors = 0
    d_salu = d_money = d_brand = d_phone = d_nolead = d_dup_sent = 0

    for f in sorted(WORK.glob("wa-out-*.json")):
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
        if not first_line.startswith(f"Hello {salutation_of(lead)},"):
            d_salu += 1
            continue
        if MONEY_RE.search(body):
            d_money += 1
            continue
        # Brand gate runs on the copy minus the Instagram handle, then the
        # handle is appended to the signature.
        if BRAND_RE.search(INSTAGRAM_RE.sub("", body)):
            d_brand += 1
            continue
        body = with_instagram(body)

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
            "salutation": salutation_of(lead),
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
