#!/usr/bin/env python3
"""Shared email-harvesting helpers used by the contact-person enrichment chain.

The point of this module is to give the name-finder agent a STRONG, tool-backed
set of real emails to work from — not to make it guess. Stage 4 renders each
site with crawl4ai (JS-aware), so the rendered HTML in raw_html/ already holds
the addresses a site publishes. We harvest those here, classify them, and
surface:

  - personal_emails : real person-format addresses ON the lead's own domain
                      (these reveal the company's email FORMAT, and may BE the
                      decision-maker's address verbatim)
  - role_emails     : info@/contact@/etc. on the domain (NOT for sending, but
                      they confirm the live mail domain)
  - all_emails      : everything non-junk we saw, for the agent's reference

The agent then (1) matches a personal address to the decision-maker by name
[verbatim, high confidence], or (2) reconstructs the decision-maker's address
from the observed format [medium confidence], or (3) falls back to web search.
Reconstruction is the LAST resort, behind real findings.
"""
from __future__ import annotations

import html as _html
import re
from pathlib import Path

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ROLE_RE = re.compile(
    r"^(info|contact|hello|hi|sales|admin|support|noreply|no-reply|marketing|hr|jobs|"
    r"careers|booking|bookings|appointment|appointments|reception|office|team|enquiry|"
    r"enquiries|inquiry|inquiries|mail|email|service|services|customercare|help|feedback|"
    r"webmaster|finance|accounts|billing|news|abuse|postmaster|general|operations|ops|"
    r"frontdesk|press|media|pr|cs|reservations|reservation|banqueting|catering|events|concierge)$",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|woff2?|ttf|eot|mp4|pdf|html?)$"
    r"|@(example\.com|example\.org|sentry\.io|wpforms\.com|wixpress\.com|wix\.com|godaddy\.com|"
    r"sentry-next|domain\.com|email\.com|yourdomain\.com|company\.com|surname\.com|"
    r"name\.com|firstname\.com|test\.com|sample\.com|acme\.com|placeholder)"
    r"|^(\d+x|[a-f0-9]{16,})@",
    re.IGNORECASE,
)
# Form-placeholder local parts that masquerade as real addresses in input fields.
PLACEHOLDER_LOCALS = {
    "yourname", "your.name", "your_email", "youremail", "name", "firstname",
    "lastname", "fname", "lname", "first.last", "first", "last", "example",
    "sample", "test", "user", "username", "johndoe", "john.doe", "janedoe",
    "jane.doe", "email", "your", "abc", "xyz",
}
FREEMAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "protonmail.com", "proton.me", "live.com", "aol.com", "msn.com",
}


def deobfuscate(text: str) -> str:
    """Decode email-relevant encodings/obfuscations so addresses survive scanning."""
    text = re.sub(r"%40", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"%2e", ".", text, flags=re.IGNORECASE)
    text = (text.replace("&#64;", "@").replace("&#x40;", "@").replace("&commat;", "@")
                .replace("&#46;", ".").replace("&#x2e;", ".").replace("&period;", "."))
    text = re.sub(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", "@", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", text, flags=re.IGNORECASE)
    return text


def root_domain(s: str) -> str:
    s = s.lower().strip()
    if "@" in s:
        s = s.split("@", 1)[1]
    s = s.split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    parts = [p for p in s.split(".") if p]
    if len(parts) < 2:
        return s
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "co", "gov", "med"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _classify(local: str, dom: str, lead_root: str) -> str:
    if len(local) < 2:
        return "junk"
    if local in PLACEHOLDER_LOCALS:
        return "junk"
    if ROLE_RE.match(local):
        return "role"
    if dom in FREEMAIL:
        return "freemail"
    if lead_root and root_domain(dom) != lead_root:
        return "offdomain"
    return "personal"


def emails_from_text(text: str, lead_domain: str) -> dict:
    """Harvest + classify emails from one blob of (already de-obfuscated) text."""
    lead_root = root_domain(lead_domain) if lead_domain else ""
    seen: set[str] = set()
    personal, role, offdomain, freemail = [], [], [], []
    # mailto first (highest signal)
    blob = text
    for m in re.finditer(r"mailto:([^\"'?>\s]+)", blob, flags=re.IGNORECASE):
        addr = m.group(1).replace("%40", "@").replace("%2E", ".").replace("%2e", ".")
        blob += " " + addr
    blob = deobfuscate(blob)
    for m in EMAIL_RE.finditer(blob):
        e = m.group(0).lower().lstrip(".").rstrip(".")
        if e in seen or "@" not in e or e.count("@") != 1:
            continue
        if JUNK_RE.search(e):
            continue
        seen.add(e)
        local, dom = e.split("@", 1)
        cls = _classify(local, dom, lead_root)
        if cls == "personal":
            personal.append(e)
        elif cls == "role":
            role.append(e)
        elif cls == "freemail":
            freemail.append(e)
        elif cls == "offdomain":
            offdomain.append(e)
    return {"personal": personal, "role": role, "offdomain": offdomain, "freemail": freemail}


def harvest_domain(domain: str, raw_html_dir: Path) -> dict:
    """Read every raw_html/{domain}__*.html page for a domain and return the
    classified email sets. Pure file read — no network — so it is instant and
    safe to call once per lead during prep."""
    safe = domain.replace("/", "_")
    chunks: list[str] = []
    for f in sorted(raw_html_dir.glob(f"{safe}__*.html")):
        try:
            txt = f.read_text(errors="ignore")
        except TypeError:
            txt = f.read_text()
        except Exception:
            continue
        if len(txt) >= 200:
            chunks.append(txt)
    if not chunks:
        return {"personal": [], "role": [], "offdomain": [], "freemail": []}
    combined = emails_from_text("\n".join(chunks), domain)
    # de-dup while preserving order, cap each list so batch files stay small
    out = {}
    for k, v in combined.items():
        seen, keep = set(), []
        for e in v:
            if e not in seen:
                seen.add(e)
                keep.append(e)
        out[k] = keep[:12]
    return out


# --- decision-maker page-text extraction (pure file read) -----------------
# Inject the about/team/leadership/contact text we ALREADY scraped in Stage 4
# straight into the name-finder task, so the agent reads pages it owns instead
# of re-fetching them over the web (fewer tokens, fewer round-trips, faster).
PAGE_TEXT_PRIORITY = [
    "leadership", "management", "team", "ourteam", "our-team", "about", "aboutus",
    "about-us", "people", "staff", "attorneys", "lawyers", "doctors", "physicians",
    "providers", "owners", "founders", "contact", "contactus", "contact-us",
]
_TAG_DROP_RE = re.compile(r"<(script|style|head|noscript|svg)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")

# Cookie-consent / privacy / nav chrome, multilingual (EN/ES/IT/FR/DE). These
# banners render at the TOP of the HTML, so without stripping them the char cap
# captures cookie noise and truncates BEFORE the decision-maker bio. Dropping
# them cuts injected tokens ~2x AND surfaces the actual name. Tuned to consent
# vocabulary so it won't eat a real "About"/bio line.
_BOILERPLATE_RE = re.compile(
    r"cookie|consent|consentimiento|consenso|consentement|einwilligung|"
    r"pol[íi]tica de (cookies|privacidad)|privacy policy|informativa|"
    r"politique de confidentialit|datenschutz|"
    r"configuraci[óo]n|aceptar|rechazar|accept all|reject all|accetta|rifiuta|"
    r"accepter|refuser|akzeptieren|ablehnen|"
    r"utilizamos|utilizziamo|nous utilisons|wir verwenden|we use cookies|"
    r"navegaci[óo]n|manage preferences|gestionar preferencias|configurar|"
    r"recordar determinadas|preferencias del usuario|"
    r"t[ée]cnicas esenciales|anal[íi]ticas|publicitarias|personalizaci[óo]n necesari",
    re.IGNORECASE)


def _page_slug(f: Path) -> str:
    stem = f.stem
    return stem.split("__", 1)[1] if "__" in stem else stem


def _html_to_text(raw: str) -> str:
    t = _TAG_DROP_RE.sub(" ", raw)
    t = _TAG_RE.sub("\n", t)
    t = _html.unescape(t)
    t = _INLINE_WS_RE.sub(" ", t)
    t = _BLANK_LINES_RE.sub("\n", t)
    return t.strip()


def _clean_lines(text: str, seen: set) -> str:
    """Drop cookie/consent/nav boilerplate lines and lines already seen on an
    earlier page (the same banner repeats across about/team/contact). Keeps the
    high-signal text the name-finder actually needs."""
    out = []
    for ln in text.split("\n"):
        s = ln.strip()
        if len(s) < 3:
            continue
        if _BOILERPLATE_RE.search(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return "\n".join(out)


def extract_page_text(domain: str, raw_html_dir: Path, max_pages: int = 3,
                      per_page_chars: int = 1200, total_chars: int = 3600) -> str:
    """Return compact, labeled readable text from the decision-maker-relevant
    pages already in raw_html/ (about/team/leadership/contact), capped so the
    batch file stays small. Empty string if none found. Pure file read — no
    network."""
    safe = domain.replace("/", "_")
    files = [f for f in raw_html_dir.glob(f"{safe}__*.html") if f.is_file()]
    if not files:
        return ""

    def rank(f: Path) -> int:
        slug = _page_slug(f).lower()
        for i, key in enumerate(PAGE_TEXT_PRIORITY):
            if slug == key or slug.startswith(key):
                return i
        return len(PAGE_TEXT_PRIORITY)

    files.sort(key=rank)
    blocks: list[str] = []
    used = 0
    seen: set = set()  # cross-page line dedupe (kills repeated cookie/nav blocks)
    for f in files:
        if rank(f) >= len(PAGE_TEXT_PRIORITY):
            continue  # only inject decision-maker-relevant pages, not home/services/etc.
        if len(blocks) >= max_pages or used >= total_chars:
            break
        try:
            raw = f.read_text(errors="ignore")
        except Exception:
            continue
        # Strip boilerplate BEFORE the char cap so the cap keeps the bio, not the
        # cookie banner that renders first in the HTML.
        text = _clean_lines(_html_to_text(raw), seen)
        if len(text) < 80:
            continue
        remaining = total_chars - used
        snippet = text[:min(per_page_chars, remaining)].strip()
        if not snippet:
            continue
        blocks.append(f"--- {_page_slug(f)} page ---\n{snippet}")
        used += len(snippet)
    return "\n\n".join(blocks)


# --- phone-link harvesting (pure file read, no network) -------------------
# Extract wa.me and tel: links from the raw HTML files we already scraped.
# These are the company's OWN published WhatsApp/phone numbers and are the
# highest-signal pre-search candidates for the name-finder's mobile step.
_WA_ME_RE = re.compile(r'wa\.me/(\d{7,15})', re.IGNORECASE)
_TEL_LINK_RE = re.compile(r'href=["\']?tel:\+?(\d{6,15})["\']?', re.IGNORECASE)


def harvest_phone_links(domain: str, raw_html_dir: Path) -> list[str]:
    """Scan all raw_html/{domain}__*.html pages for wa.me/DIGITS and tel:+DIGITS
    links. Returns a deduplicated list of candidates as '+DIGITS' strings, capped
    at 6. Pure file read — no network. Caller should validate against country
    mobile rules before trusting."""
    safe = domain.replace("/", "_")
    seen: set[str] = set()
    phones: list[str] = []
    for f in sorted(raw_html_dir.glob(f"{safe}__*.html")):
        try:
            raw = f.read_text(errors="ignore")
        except Exception:
            continue
        for m in _WA_ME_RE.finditer(raw):
            digits = m.group(1)
            if digits not in seen and 9 <= len(digits) <= 15:
                seen.add(digits)
                phones.append("+" + digits)
        for m in _TEL_LINK_RE.finditer(raw):
            digits = re.sub(r"\D", "", m.group(1))
            if digits not in seen and 9 <= len(digits) <= 15:
                seen.add(digits)
                phones.append("+" + digits)
    return phones[:6]


if __name__ == "__main__":  # tiny manual smoke: python email_utils.py <run-dir> <domain>
    import sys
    rd = Path(sys.argv[1]) / "raw_html"
    print(harvest_domain(sys.argv[2], rd))
    print("\n--- page text ---\n")
    print(extract_page_text(sys.argv[2], rd))
    print("\n--- phone links ---\n")
    print(harvest_phone_links(sys.argv[2], rd))
