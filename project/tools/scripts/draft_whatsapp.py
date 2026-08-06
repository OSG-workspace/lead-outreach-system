#!/usr/bin/env python3
"""Stage 7-WA — Generate per-lead WhatsApp drafts.

Companion to draft_emails.py. Reads the same leads-with-contact.json input
(Stage 5.5 output) and writes whatsapp-drafted.json with one JSON object per
lead that has a resolvable WhatsApp number (contact_phone).

Channel rules:
- WhatsApp messages are shorter than email (3-4 short paragraphs max).
- Salutation: `Hello Mr./Mrs. <Surname>,` when surname + gender exist, else
  `Hello <First>,` — one confirmed name component is enough (same contract
  as email, per CLAUDE.md).
- NO em-dashes or en-dashes anywhere in body copy. Use commas.
- One link max (the signature website).
- No subject line (WhatsApp has none).

Kill-on-fallback: a lead without a `contact_phone` is dropped, not sent to
some generic business number. The pipeline ships fewer WhatsApp drafts
rather than misdirecting them.

Output: runs/<slug>/whatsapp-drafted.json — one JSON object per line:
  {"lead_id": ..., "to_phone": "+9715xxxxxxxx", "to_name": ...,
   "salutation": "Mr. Surname", "body_text": "...", "tags": [...], ...}

The Node sender (project/bridge/send_campaign.js) consumes this file.
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
p.add_argument("--sent-log", default=None,
               help="Master dedup log (default: <repo>/vault/lead-outreach/sent-log.md)")
p.add_argument("--fallback-only", action="store_true",
               help="draft ONLY leads Stage 5.5 marked contact_channel=whatsapp_fallback "
                    "(no verified direct email; owner reached on WhatsApp instead). "
                    "Leads with a verified email stay email-only and are skipped here.")
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
WITH_CONTACT = ROOT / "leads-with-contact.json"
OUT = ROOT / "whatsapp-drafted.json"
RUN_SLUG = ROOT.name
# ROOT is <repo>/project/runs/<slug>; the vault lives at <repo>/vault.
SENT_LOG = Path(args.sent_log) if args.sent_log else ROOT.parents[1] / "vault" / "lead-outreach" / "sent-log.md"

if not WITH_CONTACT.exists():
    raise SystemExit(f"ABORT: {WITH_CONTACT} missing — Stage 5.5 must run first")

# Cross-run dedup: never WhatsApp a lead already contacted on EITHER channel.
# The master sent-log records emails (email channel) and `wa:<phone>` tokens
# (WhatsApp channel), plus the [[lead_slug]] for each. We match on any of:
# phone, lead_slug, business email, or business domain.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WA_PHONE_RE = re.compile(r"wa:(\d{6,15})")
_SLUG_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _root_domain(s: str) -> str:
    s = (s or "").lower().strip()
    if "//" in s:
        s = s.split("//", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    s = s.split("/", 1)[0]
    return s[4:] if s.startswith("www.") else s


def load_sent_index(sent_log: Path):
    phones, emails, domains, slugs = set(), set(), set(), set()
    if not sent_log.exists():
        return phones, emails, domains, slugs
    for line in sent_log.read_text().splitlines():
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


SENT_PHONES, SENT_EMAILS, SENT_DOMAINS, SENT_SLUGS = load_sent_index(SENT_LOG)

# Pitch templates. Per-run `pitch.json` can override any of these keys; this
# matches the email drafter's overlay so both channels stay in sync.

DEFAULT_COUNTRY_NAMES = dict(COUNTRY_NAMES_ISO)   # broad ISO map; pitch.json overrides
DEFAULT_VERTICAL_NAMES = {
    "clinic": "clinic groups", "vet": "veterinary clinics",
    "optical": "optical chains", "fitness": "fitness brands",
    "salon": "salon and spa brands", "spa": "spa brands",
    "restaurant": "restaurant groups", "cafe": "cafe groups",
    "cloud_kitchen": "cloud-kitchen groups", "bakery": "bakery groups",
    "pharmacy": "pharmacy chains", "retail": "specialty retailers",
}

DEFAULT_SIGNAL_OPENERS = {
    "phone_led": "{name}'s customer path looks phone-led. For a chain in {country}, that is exactly the kind of intake work a custom AI system can take off the front desk.",
    "contact_form": "{name}'s site routes customers through a contact form. At chain scale in {country}, that turns into a lot of manual back-and-forth.",
    "whatsapp": "Most of {name}'s customer contact runs through WhatsApp. At chain scale in {country}, every reply is still a person, and a clean place to slot in a custom AI layer.",
    "booking_form_manual": "{name}'s booking flow goes through a form rather than a real calendar. For a chain in {country}, every booking still routes through a person.",
    "pdf_menu": "{name}'s online presence still leans on PDF menus. For a chain in {country}, that is a lot of manual ops the team is absorbing.",
    "no_online_booking": "{name}'s site does not surface a real online booking flow. For a chain in {country}, front-desk staff are still the booking system.",
}

DEFAULT_WA_BODY_TEMPLATE = (
    "Hello {salutation},\n\n"
    "{opener}\n\n"
    "OSG designs custom AI systems for mid-market companies in the GCC. "
    "For {vertical}, we build the intake and handoff workflows around the tools "
    "already in place. You own the system, no monthly platform lock-in.\n\n"
    "Worth a brief call this week?\n\n"
    "David Geha, OSG\nosgdev.com\nInstagram: dave.automates"
)

PITCH_FILE = ROOT / "pitch.json"
_pitch = {}
if PITCH_FILE.exists():
    _pitch = json.loads(PITCH_FILE.read_text())

COUNTRY_NAMES = {**DEFAULT_COUNTRY_NAMES, **(_pitch.get("country_names") or {})}
VERTICAL_NAMES = {**DEFAULT_VERTICAL_NAMES, **(_pitch.get("vertical_names") or {})}
# WhatsApp can reuse the email openers OR define its own under wa_signal_openers.
SIGNAL_OPENERS = {**DEFAULT_SIGNAL_OPENERS,
                  **(_pitch.get("signal_openers") or {}),
                  **(_pitch.get("wa_signal_openers") or {})}
WA_BODY_TEMPLATE = _pitch.get("wa_body_template", DEFAULT_WA_BODY_TEMPLATE)
DEFAULT_VERTICAL = _pitch.get("default_vertical", "chains")

_DASH_RE = re.compile(r"\s*[—–]\s*")


def _strip_dashes(s: str) -> str:
    return _DASH_RE.sub(", ", s)


def name_short(n: str) -> str:
    n = re.split(r"\s+[\-|–|—]\s+", n)[0]
    n = re.sub(r"\s+(LLC|L\.L\.C\.|FZ-LLC|FZE|Co\.?|Group)$", "", n, flags=re.IGNORECASE)
    if len(n) > 40:
        n = n[:40].rsplit(" ", 1)[0]
    return n.strip()


# Country-code defaults so we can validate / normalize phones lightly.
COUNTRY_CC = {"AE": "971", "SA": "966", "QA": "974", "BH": "973", "KW": "965", "LB": "961"}

MOBILE_RULES: dict[str, list[tuple[str, int]]] = {
    "LB": [("3", 10), ("70", 11), ("71", 11), ("76", 11), ("78", 11), ("79", 11), ("81", 11)],
    "AE": [("5", 12)],
    "SA": [("5", 12)],
    "QA": [("3", 11), ("5", 11), ("6", 11), ("7", 11)],
    "BH": [("3", 11)],
    "KW": [("5", 11), ("6", 11), ("9", 11)],
}


def normalize_phone(raw: str, country_code: str) -> str:
    """Return a digits-only mobile phone in international format, or "" if not
    a valid mobile number for WhatsApp.

    whatsapp-web.js wants `<digits>@c.us` — no plus, country-code first.
    Rejects landlines/switchboards since WhatsApp only registers on mobiles."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    cc = COUNTRY_CC.get(country_code, "")
    if cc and not digits.startswith(cc):
        digits = cc + digits.lstrip("0")
    if len(digits) < 9 or len(digits) > 15:
        return ""
    rules = MOBILE_RULES.get(country_code)
    if rules:
        after_cc = digits[len(cc):]
        if not any(after_cc.startswith(pfx) and len(digits) == exp_len for pfx, exp_len in rules):
            return ""
    return digits


def draft(lead: dict, normalized_phone: str) -> dict:
    name = name_short(lead["name"])
    country = COUNTRY_NAMES.get(lead["country_code"], lead["country_code"])
    vertical = VERTICAL_NAMES.get(lead["vertical"], DEFAULT_VERTICAL)
    signal = lead.get("signal", "phone_led")
    opener_tpl = SIGNAL_OPENERS.get(signal, SIGNAL_OPENERS["phone_led"])
    opener = opener_tpl.format(name=name, country=country, vertical=vertical)

    _last = lead["contact_last_name"]
    _title = lead["contact_title"]
    salutation = f"{_title} {_last}" if (_last and _title) else lead["contact_first_name"]
    fmt = dict(name=name, country=country, vertical=vertical, salutation=salutation, opener=opener)
    body_text = WA_BODY_TEMPLATE.format_map(fmt)
    body_text = _strip_dashes(body_text)

    return {
        "lead_id": lead["lead_id"],
        "lead_slug": lead["lead_slug"],
        "to_phone": normalized_phone,
        "to_jid": f"{normalized_phone}@c.us",
        "contact_email": (lead.get("to_email") or lead.get("contact_email") or "").strip().lower(),
        "website": lead.get("website", ""),
        "to_name": f"{lead['contact_first_name']} {lead['contact_last_name']}".strip(),
        "salutation": salutation,
        "contact_first_name": lead["contact_first_name"],
        "contact_last_name": lead["contact_last_name"],
        "contact_title": lead["contact_title"],
        "contact_role": lead.get("contact_role", ""),
        "contact_phone_source_url": lead.get("contact_phone_source_url", ""),
        "contact_confidence": lead.get("contact_confidence", ""),
        "business_name": name,
        "body_text": body_text,
        "tags": ["cold-outreach", "whatsapp", RUN_SLUG, lead["vertical"], lead["country_code"]],
        "score": lead["score"],
        "qualification_status": "send_ready",
        "signal_used": signal,
        "primary_gap": signal,
        "channel": "whatsapp",
        "send_gate": "pass",
        "country_code": lead["country_code"],
        "vertical": lead["vertical"],
    }


leads = []
for line in WITH_CONTACT.read_text().splitlines():
    if line.strip():
        leads.append(json.loads(line))

drafts = []
skipped_email_channel = 0
dropped_no_contact = 0
dropped_no_phone = 0
dropped_bad_phone = 0
dropped_already_sent = 0
for l in leads:
    if args.fallback_only and l.get("contact_channel") != "whatsapp_fallback":
        skipped_email_channel += 1
        continue
    first = l.get("contact_first_name", "").strip()
    last = l.get("contact_last_name", "").strip()
    title = l.get("contact_title", "").strip()
    # One name component is enough (per CLAUDE.md salutation contract).
    if not ((last and title in {"Mr.", "Mrs."}) or first):
        dropped_no_contact += 1
        continue
    raw_phone = (l.get("contact_phone") or "").strip()
    if not raw_phone:
        dropped_no_phone += 1
        continue
    normalized = normalize_phone(raw_phone, l.get("country_code", ""))
    if not normalized:
        dropped_bad_phone += 1
        continue
    # Already contacted on either channel? Drop, never re-contact.
    email = (l.get("to_email") or l.get("contact_email") or "").strip().lower()
    domain = _root_domain(l.get("website") or email)
    slug = (l.get("lead_slug") or "").strip().lower()
    if (normalized in SENT_PHONES or slug in SENT_SLUGS
            or (email and email in SENT_EMAILS)
            or (domain and domain in SENT_DOMAINS)):
        dropped_already_sent += 1
        continue
    drafts.append(draft(l, normalized))

drafts.sort(key=lambda d: -d["score"])
OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")
print(f"Drafted {len(drafts)} WhatsApp messages (input: {WITH_CONTACT.name})")
if args.fallback_only:
    print(f"  skipped email-channel leads:      {skipped_email_channel}  (--fallback-only)")
print(f"  dropped no-contact (name/title):  {dropped_no_contact}")
print(f"  dropped no-phone:                 {dropped_no_phone}  (decision-maker WhatsApp required)")
print(f"  dropped phone-unparseable:        {dropped_bad_phone}")
print(f"  dropped already-contacted:        {dropped_already_sent}  (in sent-log, either channel)")
for d in drafts[:3]:
    print(f"\n--- {d['to_phone']} | {d['to_name']} | salutation={d['salutation']} ---")
    print(d["body_text"][:400] + ("..." if len(d["body_text"]) > 400 else ""))
