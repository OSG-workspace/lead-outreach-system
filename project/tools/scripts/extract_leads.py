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
# A LinkedIn run reaches a PERSON on LinkedIn, so the company's mailbox is
# irrelevant to it. Without this flag a company that publishes no email is
# dropped here and never reaches the walker — which deletes most of the
# audience in exactly the markets LinkedIn is for (the gcc-outreach-li fixture
# measured 529 brokerages, 47 with a website). Off by default: every email
# campaign keeps its existing behaviour byte-for-byte.
p.add_argument("--allow-no-email", action="store_true",
               help="keep leads with no discoverable email (LinkedIn-only runs)")
args = p.parse_args()

ROOT = Path(args.run_dir)
RAW = ROOT / "raw_html"
CANDIDATES = ROOT / "candidates-all.txt"
OUT = ROOT / "leads-extracted.json"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# SHARED nets — do NOT re-declare these locally.
# This file used to keep its own, weaker copies of ROLE_RE / JUNK_RE / FREEMAIL
# while email_utils.py carried the maintained ones. The two drifted, and the gap
# was exactly the placeholder-address net: email_utils blocks `@email.com`,
# `@inbox.com`, `@yourdomain.com` and a 24-entry PLACEHOLDER_LOCALS list, this
# file blocked none of them. Result (audited 2026-07-31 across 132 runs):
# 69 form-placeholder addresses — `your@email.com` ×27, `you@email.com` ×8,
# `j.doe@inbox.com` ×7 — were scraped out of contact-form `placeholder=`
# attributes, became a lead's to_email, passed qualification, and burned one
# name-finder agent each (~4 per au-trades run). 3 were actually emailed.
# Importing keeps one definition of "is this a real mailbox" for the whole chain.
from email_utils import (ROLE_RE, JUNK_RE, FREEMAIL,          # noqa: E402
                         PLACEHOLDER_LOCALS)
# Freemail/ISP variants outside the base set (live.com.au, bigpond.com, …).
from name_from_email import ALL_FREEMAIL as FREEMAIL_ALL      # noqa: E402
from traffic_signals import detect_traffic                    # noqa: E402

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
# Keywords too generic to prove BIG-property scale on their own (even a small
# boutique inn advertises "spa"). Validated 2026-07-16 against run
# 2026-07-15-eu-hotels-1: leads whose ONLY big-keyword hit was "spa" had a 27%
# enrichment hit rate (n=33) vs 47% for "resort"-alone (n=19) and a 41% run
# average — the weakest, most generic signal in the list, so it must not
# qualify a lead for "medium" by itself. Still counts toward the `big >= 3`
# tally and the 5-star combo path below, where it's corroborated by other
# evidence.
_WEAK_SOLO_KEYWORDS = {"spa"}
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
    # True only when EVERY keyword hit is a generic amenity word that doesn't
    # prove scale on its own (see _WEAK_SOLO_KEYWORDS above).
    solo_weak = bool(big_hits) and set(big_hits) <= _WEAK_SOLO_KEYWORDS

    if rooms >= 80 or (rooms >= 50 and (four or five)) or (five and big >= 1) or big >= 3:
        ev = f"{rooms} rooms" if rooms >= 50 else (f"5-star + {big} scale cues" if five else f"{big} large-property cues")
        return ("high", ev)
    if rooms >= 40 or four or five or (big >= 1 and not solo_weak):
        bits = []
        if rooms:
            bits.append(f"{rooms} rooms")
        if five:
            bits.append("5-star")
        elif four:
            bits.append("4-star")
        if big_hits and not solo_weak:
            bits.append(big_hits[0])
        return ("medium", ", ".join(bits) or "some scale cues")
    if solo_weak:
        return ("low", f"only a generic '{big_hits[0]}' mention — not scale evidence on its own")
    return ("low", "no room-count / star / resort-scale signal found")


def classify_email(e: str, lead_domain: str) -> str:
    e = e.lower().strip()
    if "@" not in e or e.count("@") != 1: return "junk"
    local, dom = e.split("@",1)
    if len(local) < 3: return "junk"
    if JUNK_RE.search(e): return "junk"
    # Form-placeholder locals (`your@`, `you@`, `name@`, `j.doe@`, …). These are
    # scraped out of `placeholder="your@email.com"` attributes on contact forms,
    # not published by the business. Shared list, see the import note above.
    if local in PLACEHOLDER_LOCALS: return "junk"
    # ROLE_RE is anchored to the whole LOCAL part in email_utils (`^(info|…)$`),
    # so it must be matched against `local`, not the full address.
    if ROLE_RE.match(local): return "role"
    if dom in FREEMAIL or dom in FREEMAIL_ALL: return "personal"
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
        # off-variant business email (e.g. clinic.example -> info@examplehospital.com):
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
    # DETERMINISM: the candidate emails arrive as a set comprehension over the
    # page text, and Python randomises string hashing per process, so two
    # candidates that tie on (rank, length) used to be resolved by set iteration
    # order — i.e. by PYTHONHASHSEED. The same run folder re-extracted twice
    # picked a different to_email for 11 of 279 eu-hotels leads
    # (leif@ vs oslo@, porto@ vs ghent@, rotterdam@ vs amsterdam@ — all exact
    # length ties). Sorting the address itself as the final key makes the choice
    # reproducible, which is what lets a run be re-extracted and compared at all.
    scored.sort(key=lambda x: (x[0], len(x[1]), x[1]))
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
    # "none" = no email found, kept only on a LinkedIn run (--allow-no-email).
    # It scores at the role base because on that channel the email class carries
    # no information at all — this is NOT a claim that the mailbox is good, and
    # the branches/pages bonuses below stay the real evidence. qualify_leads.py
    # exempts these from the email-quality floor rather than judging them by it.
    base = {"person": 90, "role": 82, "personal": 78, "none": 82}[klass]
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
        # LinkedIn reaches a person, not a mailbox: no email is not a defect
        # there, so the company survives with to_email=None and email_class
        # "none". Every downstream email path already requires a real address,
        # so these can never leak into a send.
        if not args.allow_no_email:
            continue
        best, klass = None, "none"
    elif best in SENT:
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

    # Traffic / demand signal — EVERY vertical, not just hotels. Reads only the
    # HTML already fetched above, so it costs no request and no token.
    traffic_tier, traffic_score, traffic_evidence, traffic_signals = detect_traffic(
        html_lower, pages, branches
    )

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
        "traffic_tier": traffic_tier,
        "traffic_score": traffic_score,
        "traffic_evidence": traffic_evidence,
        "traffic_signals": traffic_signals,
        "score": score,
        "pages_scanned": pages,
        "emails_found": n_emails,
    }
    leads.append(lead)

def drop_shared_offdomain(rows: list[dict]) -> int:
    """Drop an OFF-domain address that several unrelated leads all picked.

    pick_best deliberately keeps a single off-variant company mailbox (clinic.example ->
    info@examplehospital.com) rather than discarding the lead. That rescue is right
    for ONE business, but it cannot see across leads — and a directory page, a
    shared web developer, or a franchise footer puts the SAME third-party address
    on many unrelated domains. Audit of the 2026-08-05 backlog found 17 lead
    records sharing 7 addresses this way (jobs@sharedtrades.example.au stood as the
    to_email for three different plumbers), so each would have been pitched at a
    mailbox belonging to someone else and burned in the sent-log forever.

    Sharing is only evidence of contamination when the address is off-domain for
    the leads holding it: a real business legitimately reuses its own mailbox
    across its own pages, and that case is on-variant and untouched here.
    """
    by_addr: dict[str, list[dict]] = {}
    for l in rows:
        addr = (l.get("to_email") or "").lower()
        if not addr:
            continue
        variants = lead_root_variants(l["lead_slug"].replace("-", "."))
        if root_domain(addr) in variants:
            continue                      # the business's own mailbox
        by_addr.setdefault(addr, []).append(l)

    dropped = 0
    for addr, holders in by_addr.items():
        if len(holders) < 2:
            continue                      # a lone off-domain mailbox is the rescue case
        for l in holders:
            l["to_email"] = None
            l["email_class"] = "none"
            l["email_drop_reason"] = f"off-domain address shared by {len(holders)} unrelated leads"
            dropped += 1
        print(f"  contaminated: {addr} was the to_email for {len(holders)} unrelated "
              f"leads -> dropped from all of them")
    return dropped


n_contaminated = drop_shared_offdomain(leads)
if n_contaminated and not args.allow_no_email:
    # Without an email these leads cannot be contacted on this channel at all.
    leads = [l for l in leads if l.get("to_email")]

leads.sort(key=lambda x: -x["score"])
OUT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in leads) + "\n")
print(f"Extracted {len(leads)} leads")
if n_contaminated:
    print(f"  ({n_contaminated} lead(s) lost a shared off-domain address)")
print(f"Person-class: {sum(1 for l in leads if l['email_class']=='person')}")
print(f"Role-class:   {sum(1 for l in leads if l['email_class']=='role')}")
print(f"Personal:     {sum(1 for l in leads if l['email_class']=='personal')}")
if args.allow_no_email:
    print(f"No email:     {sum(1 for l in leads if l['email_class']=='none')} "
          f"(kept for LinkedIn; unreachable by email)")
for l in leads[:20]:
    pages = ",".join(l['pages_scanned'])
    # to_email is None on LinkedIn-only leads, which has no format spec.
    print(f"  {l['score']} {l['country_code']} {l['vertical']:8} {l['email_class']:8} "
          f"{(l['to_email'] or '-'):40} pages={pages:25} {l['name']}")
