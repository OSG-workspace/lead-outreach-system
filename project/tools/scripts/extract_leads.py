#!/usr/bin/env python3
"""Extract emails + signals from raw HTML (homepage + /contact + /about + /team).

Multi-page extraction: scans every raw_html/{domain}__{slug}.html file per domain
and concatenates their text before regex-scanning for emails. Roughly 2-3x the
yield of homepage-only extraction.

Scoring (raised so role-class can clear the >=85 send gate when chain-sized):
  person  : 90 base, +5 if 5-15 branches, +3 if 16-30 branches  -> max 95
  role    : 82 base, +5 if 5-15 branches, +3 if 16-30 branches  -> max 87
  personal: 78 base                                              -> max 78

Outputs leads-extracted.json (JSONL).
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
p.add_argument("--sent-log", default="")
args = p.parse_args()

ROOT = Path(args.run_dir)
RAW = ROOT / "raw_html"
CANDIDATES = ROOT / "candidates-all.txt"
OUT = ROOT / "leads-extracted.json"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ROLE_RE = re.compile(r"^(info|contact|hello|hi|sales|admin|support|noreply|marketing|hr|jobs|careers|booking|appointment|reception|office|team|enquiry|enquiries|inquiry|inquiries|mail|email|service|customercare|help|feedback|webmaster|finance|accounts|billing|news|abuse|postmaster|general|operations|ops|frontdesk|press|media|pr|cs)@", re.IGNORECASE)
JUNK_RE = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|html?)$|@(example\.com|sentry\.io|wpforms\.com|wixpress\.com|wix\.com|godaddy\.com|sentry-next|gmail\.com\.|domain\.com)|^(\d+x|[a-f0-9]{16,})@", re.IGNORECASE)
FREEMAIL = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com","protonmail.com","live.com","aol.com"}

SENT = set()
sent_log_path = Path(args.sent_log) if args.sent_log else ROOT.parent.parent / "vault" / "lead-outreach" / "sent-log.md"
if sent_log_path.exists():
    for line in sent_log_path.read_text().splitlines():
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if "smtp-relay" not in e and "mailin.fr" not in e:
                SENT.add(e)

def root_domain(s: str) -> str:
    s = s.lower().strip()
    if "@" in s: s = s.split("@",1)[1]
    s = s.split(":",1)[0]
    if s.startswith("www."): s = s[4:]
    parts = [p for p in s.split(".") if p]
    if len(parts) < 2: return s
    if len(parts) >= 3 and parts[-2] in {"com","net","org","co","gov","med"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

def detect_signal(html_lower: str) -> tuple[str, str]:
    """Return (signal_code, evidence_phrase)."""
    has_whatsapp = bool(re.search(r"whatsapp|wa\.me/", html_lower))
    has_phone = bool(re.search(r"\+?\d{1,3}[\s\-]?\d{2,3}[\s\-]?\d{3,4}[\s\-]?\d{3,4}|tel:", html_lower))
    has_form = bool(re.search(r"<form|contact[\-_ ]form|name=\"email\"|input[^>]*email", html_lower))
    has_book = bool(re.search(r"book[\-_ ]now|book[\-_ ]online|book[\-_ ]appointment|book[\-_ ]your", html_lower))
    has_pdf_menu = bool(re.search(r"\.pdf.*menu|menu.*\.pdf", html_lower))
    has_real_booking = bool(re.search(r"calendly|setmore|fresha|simplybook|nookal|zenoti|10to8|appointy|squareup|acuityscheduling", html_lower))

    if has_real_booking:
        return ("modern_booking", "uses a third-party online booking platform")
    if has_pdf_menu:
        return ("pdf_menu", "PDF menu/brochure on the site")
    if has_whatsapp and not has_book:
        return ("whatsapp", "primary customer contact is WhatsApp")
    if has_form and not has_book:
        return ("contact_form", "site sends customers through a contact form")
    if has_book and not has_real_booking:
        return ("booking_form_manual", "manual booking form, no integrated calendar")
    if has_phone and not has_form and not has_whatsapp:
        return ("phone_led", "phone-led customer intake")
    return ("phone_led", "phone-led customer intake")

HOTEL_VERTICALS = {"hotel", "hotels", "resort", "resorts", "bnb", "guesthouse", "guesthouses"}
_ROOMS_RE = re.compile(
    r"(\d{2,4})\s*(?:\+|plus)?\s*(?:guest\s*)?(?:rooms|bedrooms|suites|keys|"
    r"habitaciones|chambres|camere|zimmer)\b", re.IGNORECASE)
# Big-property / high-traffic indicators (multilingual, light).
_BIG_KEYWORDS = (
    "resort", "spa", "conference", "convention", "congress", "ballroom",
    "banquet", "meetings", "mice", "palace", "grand hotel", "wedding venue",
    "rooftop", "5-star", "5 star", "five star", "five-star", "4-star", "4 star",
    "four star", "four-star", "cinco estrellas", "cuatro estrellas",
    "5 estrellas", "4 estrellas", "5 etoiles", "5 étoiles", "4 etoiles",
    "4 étoiles", "5 stelle", "4 stelle",
)
_FIVE_STAR_RE = re.compile(r"5[\s\-]?star|five[\s\-]?star|cinco estrellas|5 estrellas|5\s*[ée]toiles|5 stelle", re.IGNORECASE)
_FOUR_STAR_RE = re.compile(r"4[\s\-]?star|four[\s\-]?star|cuatro estrellas|4 estrellas|4\s*[ée]toiles|4 stelle", re.IGNORECASE)


def detect_hotel_volume(html_lower: str) -> tuple[str, str]:
    """Return (tier, evidence) for a hotel lead: high | medium | low.

    A QUALIFIED hotel client is a BIG, high-call-volume property. We infer size
    from the site itself (room count, star rating, resort/spa/conference scale).
    Small B&Bs and quiet guesthouses fall to `low` and get dropped at qualify.
    """
    rooms = 0
    for m in _ROOMS_RE.finditer(html_lower):
        try:
            rooms = max(rooms, int(m.group(1)))
        except ValueError:
            pass
    five = bool(_FIVE_STAR_RE.search(html_lower))
    four = bool(_FOUR_STAR_RE.search(html_lower))
    big_hits = [k for k in _BIG_KEYWORDS if k in html_lower]
    big = len(set(big_hits))

    if rooms >= 80 or (rooms >= 50 and (four or five)) or (five and big >= 1) or big >= 3:
        ev = f"{rooms} rooms" if rooms >= 50 else (f"5-star + {big} scale cues" if five else f"{big} large-property cues")
        return ("high", ev)
    if rooms >= 40 or four or five or big >= 1:
        bits = []
        if rooms:
            bits.append(f"{rooms} rooms")
        if five:
            bits.append("5-star")
        elif four:
            bits.append("4-star")
        if big_hits:
            bits.append(big_hits[0])
        return ("medium", ", ".join(bits) or "some scale cues")
    return ("low", "no room-count / star / resort-scale signal found")


def classify_email(e: str, lead_domain: str) -> str:
    e = e.lower().strip()
    if "@" not in e or e.count("@") != 1: return "junk"
    local, dom = e.split("@",1)
    if len(local) < 3: return "junk"
    if JUNK_RE.search(e): return "junk"
    if ROLE_RE.match(e): return "role"
    if dom in FREEMAIL: return "personal"
    return "person"

def deobfuscate(text: str) -> str:
    """Decode the email-relevant encodings/obfuscations BEFORE the generic %XX
    strip so addresses like `info%40domain.com`, `name [at] domain [dot] com`,
    and `name&#64;domain.com` survive into EMAIL_RE."""
    text = re.sub(r"%40", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"%2e", ".", text, flags=re.IGNORECASE)
    text = (text.replace("&#64;", "@").replace("&#x40;", "@").replace("&commat;", "@")
                .replace("&#46;", ".").replace("&#x2e;", ".").replace("&period;", "."))
    # Bracketed/parenthesized obfuscation: name [at] domain [dot] com
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.IGNORECASE)
    return text


def lead_root_variants(domain: str) -> set[str]:
    """Generate domain variants for email-domain match check."""
    d = root_domain(domain)
    base = d.split(".")[0]
    # Allow related TLDs and a few common group-suffix patterns.
    out = {d}
    for tld in [".com", ".ae", ".sa", ".com.sa", ".qa", ".com.qa", ".bh", ".com.bh", ".kw", ".com.kw", ".me", ".org", ".net", ".net.sa", ".net.ae"]:
        out.add(f"{base}{tld}")
    # Strip a trailing "group" / "retailgroup" suffix and try variants for that too.
    for suffix in ("retailgroup", "group", "health", "clinics", "clinic", "company"):
        if base.endswith(suffix) and len(base) > len(suffix) + 2:
            stem = base[: -len(suffix)]
            for tld in [".com", ".ae", ".sa", ".qa", ".bh", ".kw"]:
                out.add(f"{stem}{tld}")
    return out

def pick_best(emails: list[str], lead_domain: str) -> tuple[str, str, int]:
    """Return (best_email, class, num_candidates_seen) — num candidates is for ranking."""
    variants = lead_root_variants(lead_domain)
    scored = []
    for e in emails:
        c = classify_email(e, lead_domain)
        if c == "junk": continue
        e_root = root_domain(e)
        on_variant = (not lead_domain) or (e_root in variants)
        # Prefer same-domain addresses, but no longer DROP a clearly-related
        # off-variant business email (e.g. aster.qa -> info@asterhospital.com):
        # demote it to last resort so a lead with only an off-domain company
        # mailbox still reaches enrichment instead of being discarded.
        if c == "person":
            rank = 0 if on_variant else 3
        elif c == "role":
            rank = 1 if on_variant else 4
        else:  # personal (freemail)
            rank = 2
        scored.append((rank, e, c))
    if not scored: return "", "missing", 0
    scored.sort(key=lambda x: (x[0], len(x[1])))
    return scored[0][1], scored[0][2], len(scored)

def collect_html_for_domain(domain: str) -> tuple[str, list[str]]:
    """Return (concatenated lowercase HTML, list of page slugs that had content)."""
    safe = domain.replace("/", "_")
    pages_found: list[str] = []
    chunks: list[str] = []
    # Multi-page: raw_html/{domain}__{slug}.html
    for f in sorted(RAW.glob(f"{safe}__*.html")):
        try:
            txt = f.read_text(errors="ignore")
            if len(txt) >= 500:  # skip tiny error pages
                chunks.append(txt)
                slug = f.stem.split("__", 1)[1]
                pages_found.append(slug)
        except Exception:
            pass
    # Backward compat: raw_html/{domain}.html (legacy single-page)
    legacy = RAW / f"{safe}.html"
    if legacy.exists() and not chunks:
        try:
            txt = legacy.read_text(errors="ignore")
            if len(txt) >= 500:
                chunks.append(txt)
                pages_found.append("home")
        except Exception:
            pass
    return ("\n".join(chunks), pages_found)

def score_for(klass: str, branches: int, pages: list[str], num_emails: int) -> int:
    base = {"person": 90, "role": 82, "personal": 78}[klass]
    if 5 <= branches <= 15:
        base += 5
    elif 16 <= branches <= 30:
        base += 3
    # Multi-page evidence bonus: contact/team page hit AND multiple emails seen
    if num_emails >= 2 and any(p in {"contact", "contactus", "team"} for p in pages):
        base += 1
    return base

leads = []
candidates_rows = [l.split("|") for l in CANDIDATES.read_text().splitlines() if l.strip()]

for row in candidates_rows:
    if len(row) < 5: continue
    domain, name, country, vertical, branches_s = row[0], row[1], row[2], row[3], row[4]
    html, pages = collect_html_for_domain(domain)
    if not html:
        continue
    html_lower = html.lower()
    # Decode email-relevant encodings/obfuscations FIRST (so %40 -> @ survives),
    # then strip the remaining URL-encoded garbage that glues to addresses.
    clean_html = deobfuscate(html)
    clean_html = re.sub(r"%[0-9A-Fa-f]{2}", " ", clean_html)
    clean_html = clean_html.replace("\xa0", " ")
    emails = {m.group(0).lower() for m in EMAIL_RE.finditer(clean_html)}
    # Harvest mailto: targets explicitly (covers hrefs with query params/encoding).
    for m in re.finditer(r"mailto:([^\"'?>\s]+)", html, flags=re.IGNORECASE):
        addr = m.group(1).replace("%40", "@").replace("%2E", ".").replace("%2e", ".").lower()
        if "@" in addr:
            emails.add(addr)
    emails_found = [e.lstrip("%20").lstrip(".") for e in emails]
    best, klass, n_emails = pick_best(emails_found, domain)
    if not best:
        continue
    if best in SENT:
        continue
    signal, evidence = detect_signal(html_lower)
    if signal == "modern_booking":
        continue

    try:
        branches = int(branches_s)
    except ValueError:
        branches = 0
    score = score_for(klass, branches, pages, n_emails)

    if vertical.lower() in HOTEL_VERTICALS:
        hotel_volume, hotel_volume_evidence = detect_hotel_volume(html_lower)
    else:
        hotel_volume, hotel_volume_evidence = "na", ""

    lead = {
        "lead_id": f"web-{domain.replace('.','-')}",
        "lead_slug": domain.replace(".","-"),
        "name": name,
        "website": f"https://{domain}",
        "country_code": country,
        "vertical": vertical,
        "branches_estimate": branches,
        "to_email": best,
        "to_name": name,
        "email_class": klass,
        "signal": signal,
        "signal_evidence": evidence,
        "hotel_volume": hotel_volume,
        "hotel_volume_evidence": hotel_volume_evidence,
        "score": score,
        "pages_scanned": pages,
        "emails_found": n_emails,
    }
    leads.append(lead)

leads.sort(key=lambda x: -x["score"])
OUT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in leads) + "\n")
print(f"Extracted {len(leads)} leads")
print(f"Person-class: {sum(1 for l in leads if l['email_class']=='person')}")
print(f"Role-class:   {sum(1 for l in leads if l['email_class']=='role')}")
print(f"Personal:     {sum(1 for l in leads if l['email_class']=='personal')}")
for l in leads[:20]:
    pages = ",".join(l['pages_scanned'])
    print(f"  {l['score']} {l['country_code']} {l['vertical']:8} {l['email_class']:8} {l['to_email']:40} pages={pages:25} {l['name']}")
