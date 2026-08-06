#!/usr/bin/env python3
"""Stage 6 — Generate per-lead email drafts.

Input: leads-with-contact.json (output of Stage 5.5 enrichment). Every lead in
that file already has `contact_first_name`, `contact_last_name`,
`contact_title` (Mr./Mrs.), AND `contact_email` (the decision-maker's direct
address) — Stage 5.5 dropped anything missing any of those.

Send target: the decision-maker's direct email (`contact_email`), NOT the
website-scraped role mailbox. Generic addresses (info@, contact@, banqueting@,
careers@, etc.) are kill-on-fallback — the lead is dropped rather than sent
to a shared inbox where the salutation `Hello Mr. <Surname>,` would be wasted.

Salutation contract (per CLAUDE.md): every draft opens with
`Hello Mr./Mrs. <Surname>,` when a surname + gender exist, else
`Hello <First>,` (one confirmed name component is enough; the verified
direct email is the real bar). Never bare "Hello," and never
"<Business> team,". Leads that lack every name component or a direct email
(can only happen if this script is run against a stale leads-extracted.json
that skipped Stage 5.5) are dropped.

Falls back to reading leads-extracted.json only when leads-with-contact.json
is absent, in which case it drops every lead lacking enriched fields.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from email_utils import COUNTRY_NAMES_ISO

p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
args = p.parse_args()

ROOT = Path(args.run_dir)
WITH_CONTACT = ROOT / "leads-with-contact.json"
EXTRACTED = ROOT / "leads-extracted.json"
OUT = ROOT / "emails-drafted.json"

IN = WITH_CONTACT if WITH_CONTACT.exists() else EXTRACTED
RUN_SLUG = ROOT.name

# --------------------------------------------------------------------------
# Pitch templates. Per-run `pitch.json` can override any of these keys so
# different campaigns (GCC custom-AI, Lebanon AI-receptionist, …) can ship
# different copy from the same script. All placeholders use {name}, {country},
# {vertical}, {salutation}, {opener}.
# --------------------------------------------------------------------------

# Broad ISO map from email_utils so {country} renders correctly for ANY
# campaign geography; pitch.json `country_names` still overrides per-fixture.
DEFAULT_COUNTRY_NAMES = dict(COUNTRY_NAMES_ISO)
DEFAULT_VERTICAL_NAMES = {
    "clinic": "clinic groups", "vet": "veterinary clinics",
    "optical": "optical chains", "fitness": "fitness brands",
    "salon": "salon and spa brands", "spa": "spa brands",
    "restaurant": "restaurant groups", "cafe": "cafe groups",
    "cloud_kitchen": "cloud-kitchen groups", "bakery": "bakery groups",
    "pharmacy": "pharmacy chains", "retail": "specialty retailers",
}

DEFAULT_SIGNAL_OPENERS = {
    "phone_led": "{name}'s visible customer path appears to be phone-led. For a chain in {country}, that is exactly the kind of repeated intake work a custom AI system can take off the front desk.",
    "contact_form": "{name}'s site sends customers through a contact form rather than a real-time workflow. At chain scale in {country}, that creates manual back-and-forth after every inquiry.",
    "whatsapp": "{name} runs most customer contact through WhatsApp. At chain scale in {country}, every reply is still a manual person, and a clean place to slot in a custom AI layer.",
    "booking_form_manual": "{name}'s booking flow sends customers through a form rather than an integrated calendar. For a chain in {country}, that means every booking still routes through a person.",
    "pdf_menu": "{name}'s online presence still leans on PDF menus and brochure pages. For a chain in {country}, that's a lot of manual customer ops the team is absorbing.",
    "no_online_booking": "{name}'s site doesn't surface a real online booking flow. For a chain in {country}, that means front-desk staff are still the booking system.",
}

DEFAULT_SUBJECT_TEMPLATE = "{name}: AI for the intake workflow"
DEFAULT_BODY_TEMPLATE = """Hello {salutation},

{opener}

OSG is an AI consulting firm that designs custom AI systems for mid-market companies in the GCC. For {vertical}, we build the intake, reporting, and handoff workflows around the tools already in place. You own the system, with no monthly platform lock-in. Not a chatbot, but a custom AI tool that quietly takes work off your team.

A fraction of what global consulting firms charge, and we walk if there is no clear ROI in the first 20 minutes.

Worth a brief call this week?

Regards,
David Geha
OSG, osgdev.com
Instagram: dave.automates"""

PITCH_FILE = ROOT / "pitch.json"
_pitch = {}
if PITCH_FILE.exists():
    _pitch = json.loads(PITCH_FILE.read_text())

COUNTRY_NAMES = {**DEFAULT_COUNTRY_NAMES, **(_pitch.get("country_names") or {})}
VERTICAL_NAMES = {**DEFAULT_VERTICAL_NAMES, **(_pitch.get("vertical_names") or {})}
SIGNAL_OPENERS = {**DEFAULT_SIGNAL_OPENERS, **(_pitch.get("signal_openers") or {})}
# Optional per-country phrase for the local language/dialect the agent speaks,
# keyed by ISO2 (e.g. {"SA": "Saudi Najdi and Hejazi Arabic"}). Exposed to the
# templates as the {dialect} slot. Absent from most fixtures; a campaign that
# never uses {dialect} is unaffected.
DIALECT_NAMES = _pitch.get("dialect_names") or {}
DEFAULT_DIALECT = _pitch.get("default_dialect", "the local language")
SUBJECT_TEMPLATE = _pitch.get("subject_template", DEFAULT_SUBJECT_TEMPLATE)
BODY_TEMPLATE = _pitch.get("body_template", DEFAULT_BODY_TEMPLATE)
DEFAULT_VERTICAL = _pitch.get("default_vertical", "chains")

# Hard rule: no em-dashes anywhere in body copy (em-dash = U+2014).
# Replace stray em-dashes with commas before draft serialization. en-dashes also.
_DASH_RE = re.compile(r"\s*[—–]\s*")


def _strip_dashes(s: str) -> str:
    return _DASH_RE.sub(", ", s)


def name_short(n: str) -> str:
    n = re.split(r"\s+[\-|–|—]\s+", n)[0]
    n = re.sub(r"\s+(LLC|L\.L\.C\.|FZ-LLC|FZE|Co\.?|Group)$", "", n, flags=re.IGNORECASE)
    if len(n) > 40:
        n = n[:40].rsplit(" ", 1)[0]
    return n.strip()


def draft(lead: dict) -> dict:
    name = name_short(lead["name"])
    country = COUNTRY_NAMES.get(lead["country_code"], lead["country_code"])
    vertical = VERTICAL_NAMES.get(lead["vertical"], DEFAULT_VERTICAL)
    signal = lead.get("signal", "phone_led")
    opener_tpl = SIGNAL_OPENERS.get(signal, SIGNAL_OPENERS["phone_led"])
    dialect = DIALECT_NAMES.get(lead["country_code"], DEFAULT_DIALECT)
    opener = opener_tpl.format(name=name, country=country, vertical=vertical,
                               dialect=dialect)

    last = lead["contact_last_name"]
    title = lead["contact_title"]
    salutation = f"{title} {last}" if (last and title) else lead["contact_first_name"]
    fmt = dict(name=name, country=country, vertical=vertical, salutation=salutation,
               opener=opener, dialect=dialect)
    subject = _strip_dashes(SUBJECT_TEMPLATE.format_map(fmt))
    body_text = _strip_dashes(BODY_TEMPLATE.format_map(fmt))

    paragraphs = body_text.strip().split("\n\n")
    body_html = "\n".join(f"<p>{p.replace(chr(10),'<br>')}</p>" for p in paragraphs)

    return {
        "lead_id": lead["lead_id"],
        "lead_slug": lead["lead_slug"],
        "to_email": lead["contact_email"],
        "to_name": f"{lead['contact_first_name']} {lead['contact_last_name']}".strip(),
        "salutation": salutation,
        "contact_first_name": lead["contact_first_name"],
        "contact_last_name": lead["contact_last_name"],
        "contact_title": lead["contact_title"],
        "contact_role": lead.get("contact_role", ""),
        "contact_source_url": lead.get("contact_source_url", ""),
        "contact_email": lead["contact_email"],
        "contact_email_source_url": lead.get("contact_email_source_url", ""),
        "contact_confidence": lead.get("contact_confidence", ""),
        "business_name": name,
        "scraped_inbox": lead.get("to_email", ""),
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "tags": ["cold-outreach", RUN_SLUG, lead["vertical"], lead["country_code"]],
        "score": lead["score"],
        "qualification_status": "send_ready",
        "funnel_status": "needs_signal",
        "signal_used": signal,
        "primary_gap": signal,
        "email_class": "direct_person",
        "send_gate": "pass",
        "country_code": lead["country_code"],
        "vertical": lead["vertical"],
    }


leads = []
for line in IN.read_text().splitlines():
    if line.strip():
        leads.append(json.loads(line))

# Send-gate policy (post Stage 5.5 rewrite):
#   We route every send to the decision-maker's direct email (contact_email),
#   not the website-scraped mailbox. The old email_class gate (role/personal)
#   no longer applies because we are not sending to scraped role mailboxes.
#   Stage 5.5 (enrich_contact_person.py merge) has already dropped any lead
#   that lacks first+last name, Mr./Mrs. title, or a non-generic direct email.
#   This script just re-verifies those three fields and drafts.

GENERIC_LOCAL_PARTS = {
    "info", "contact", "hello", "inquiries", "enquiries", "enquiry",
    "support", "admin", "office", "general",
    "careers", "career", "hr", "jobs", "recruit", "recruitment",
    "marketing", "press", "media", "pr", "comms",
    "sales", "bookings", "booking", "reservations", "reservation",
    "appointments", "appointment",
    "frontdesk", "reception", "manager", "operations",
    "banqueting", "catering", "events", "concierge",
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


drafts = []
dropped_no_contact = 0
dropped_no_direct_email = 0
for l in leads:
    first = l.get("contact_first_name", "").strip()
    last = l.get("contact_last_name", "").strip()
    title = l.get("contact_title", "").strip()
    # One name component is enough (per CLAUDE.md salutation contract):
    # surname + Mr./Mrs., or a bare first name.
    if not ((last and title in {"Mr.", "Mrs."}) or first):
        dropped_no_contact += 1
        continue
    if not is_direct_email(l.get("contact_email", "")):
        dropped_no_direct_email += 1
        continue
    drafts.append(draft(l))

drafts.sort(key=lambda d: -d["score"])
OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")
print(f"Drafted {len(drafts)} emails (input: {IN.name})")
print(f"  dropped no-contact (name/title):  {dropped_no_contact}  (Stage 5.5 must run first)")
print(f"  dropped no-direct-email:          {dropped_no_direct_email}  (decision-maker email required)")
for d in drafts[:5]:
    print(f"\n--- {d['to_email']} | {d['to_name']} | salutation={d['salutation']} ---")
    print(f"Subject: {d['subject']}")
    print(d["body_text"][:300] + "...")
