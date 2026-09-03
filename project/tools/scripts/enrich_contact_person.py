#!/usr/bin/env python3
"""Stage 5.5 — contact-person enrichment.

Two phases, both run by the /fire orchestrator:

  prep   reads leads-extracted.json, writes one enrich-batch-NNN.txt per lead.
         Orchestrator then dispatches one Haiku `name-finder` sub-agent per
         batch file (max parallelism, single message).

  merge  reads all enrich-out-*.json, joins to leads-extracted.json, drops
         leads missing any of:
           - at least ONE name component (full name preferred; a first name
             alone, or a surname + Mr./Mrs., is enough — see the step-2d
             one-name fallback in name-finder.md; the verified direct email
             is the real bar)
           - direct email (a non-generic mailbox tied to the decision-maker)
         and writes leads-with-contact.json (the Stage 6 input). Generic
         mailboxes (info@, contact@, banqueting@, careers@, …) are NEVER
         accepted as a fallback — per kill-on-fallback we ship fewer, real
         emails instead of degrading to shared inboxes. The run halts only
         if zero leads survive.
"""
from __future__ import annotations
import argparse
import functools
import json
import os
import re as _re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from email_utils import harvest_domain, extract_page_text, harvest_phone_links
except Exception:  # harvester is best-effort; prep still works without it
    harvest_domain = None
    extract_page_text = None
    harvest_phone_links = None
try:
    import name_from_email as nfe
except Exception:
    nfe = None
try:
    import smtp_email_probe as smtp_probe
except (Exception, SystemExit):  # smtp_email_probe sys.exit()s on a missing dnspython — rescue is best-effort
    smtp_probe = None

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge", "rescue"])
p.add_argument("--run-dir", required=True)
p.add_argument("--work-dir", default=None,
               help="where the per-agent intermediates live (enrich-batch-NNN.txt, "
                    "enrich-out-NNN.json, and the OutputFile: path written inside each "
                    "batch). Default: the run dir, unchanged behaviour. run_fire passes "
                    "a dir OUTSIDE project/ because a batch file under runs/ makes "
                    "Claude Code attach project/CLAUDE.md (~7.5k tokens) to every "
                    "name-finder agent — 664/665 transcripts on 2026-09-02. Everything "
                    "run-level (leads-*.json, derived-contacts.json, enrich-summary.json, "
                    "leads-dropped.json, raw_html) stays in --run-dir.")
p.add_argument("--enrich-phone", action="store_true",
               help="resolve a mobile/WhatsApp number per lead (name-finder step 3). "
                    "OFF by default — only WhatsApp runs need it; an email-only run "
                    "never uses a phone, and the phone ladder is the biggest source "
                    "of wasted searches.")
p.add_argument("--wa-fallback", action="store_true",
               help="merge phase: when the decision-maker's direct email can't be "
                    "verified (no_direct_email / constructed-no-URL-evidence / "
                    "dead-domain) but the name contract holds AND a phone was "
                    "resolved, keep the lead as a WhatsApp-only lead "
                    "(contact_channel=whatsapp_fallback, empty contact_email) "
                    "instead of dropping it. Email stays the primary channel; "
                    "this only rescues leads that would otherwise die on the "
                    "email gate. Requires --enrich-phone upstream.")
p.add_argument("--linkedin-results",
               help="rescue phase: path to linkedin/scripts/lookup_contact_email.js's "
                    "output ([{lead_id,url,email_found,email,note}]). For each "
                    "email_found:true entry whose lead_id died at Stage 5.5 with a "
                    "name but no email, re-run the SAME direct-email gates "
                    "phase_merge uses and, if it survives, append it to "
                    "leads-with-contact.json instead of leaving it dropped.")
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
# The per-agent intermediates (enrich-batch-*.txt / enrich-out-*.json). Same as
# ROOT unless --work-dir is given; created on demand so run_fire can hand over a
# path that does not exist yet. Only prep writes here and only merge globs here.
WORK = Path(args.work_dir).resolve() if args.work_dir else ROOT
# Read the qualified+capped set when Stage 5.3 produced one, so we only enrich
# the top-N fit leads instead of every extracted lead. Falls back to the raw
# extract for older runs that predate the qualify gate.
QUALIFIED = ROOT / "leads-qualified.json"
EXTRACTED = QUALIFIED if QUALIFIED.exists() else (ROOT / "leads-extracted.json")
WITH_CONTACT = ROOT / "leads-with-contact.json"
# Leads whose decision-maker name was parsed straight out of a site email in the
# prep phase — no agent was dispatched for these. See DERIVE_FIRST below.
DERIVED = ROOT / "derived-contacts.json"

# Optional per-campaign override: a fixture whose decision-maker isn't the
# generic founder/CEO/owner/GM (e.g. a bank's Head of Compliance, an NGO's
# Grants Manager). One line, comma-separated titles, appended verbatim to
# every enrich-batch as `TargetRoles:` — same mechanism as fit_criteria.txt
# for source-agent. Absent -> unchanged generic ladder.
TARGET_ROLES_FILE = ROOT / "target_roles.txt"
TARGET_ROLES = TARGET_ROLES_FILE.read_text().strip() if TARGET_ROLES_FILE.exists() else ""

COUNTRY_NAMES = {"AE": "UAE", "SA": "Saudi Arabia", "QA": "Qatar", "BH": "Bahrain", "KW": "Kuwait", "LB": "Lebanon"}

# SMTP rescue (2026-08-20): the MAIL FROM used to probe a lead's own domain when
# name-finder found a person but no email. Reuses the real, already-deliverable
# sending identity this pipeline already sends from (send_batch_brevo.py reads
# the same var) rather than a placeholder address some mail servers would
# reject outright at the MAIL FROM step. See smtp_email_probe.py.
if smtp_probe is not None:
    _env = smtp_probe.load_env(Path(__file__).resolve().parents[2] / ".env")
    SMTP_MAIL_FROM = _env.get("BREVO_SENDER_EMAIL") or "verify@example.com"
else:
    SMTP_MAIL_FROM = "verify@example.com"

# Catch-all construction (enabled 2026-08-20 by user directive). On a catch-all
# domain SMTP can prove nothing, so the fallback is the company's own observed
# email convention — see build_from_site_format(). Kept behind an env knob
# because constructed addresses are the historical bounce risk in this
# pipeline: set SMTP_RESCUE_CATCHALL=0 to turn it off if Brevo bounces rise.
CATCHALL_CONSTRUCT = os.environ.get("SMTP_RESCUE_CATCHALL", "1") not in ("0", "false", "no")


# --- Prose name recovery (2026-08-20) ---------------------------------------
# MEASURED, and the reason this exists: across the 80 drops of
# 2026-08-20-eu-hotels, exactly ONE carried structured first/last-name fields,
# while 55 more named the person only in the free-text `reason`
# ("name found (Michael Oberrauch, General Manager) but no direct email…").
# name-finder.md now asks for the structured fields on a found:false, but that
# is a PROMPT instruction to a Haiku agent — best-effort compliance, not a
# guarantee, and every historical run predates it. Keying the SMTP rescue only
# on the structured fields would therefore have fired it on 1 lead instead of
# 56. This parser is the deterministic backstop: it reads the name the agent
# already wrote in prose, so the rescue works regardless of whether the agent
# filled the structured fields.
# A name token: capitalized, OR a lowercase nobiliary particle. Without the
# particle branch "Paul de Römph" stops at "Paul" and the surname is lost —
# measured against the real 2026-08-20-eu-hotels reasons, which are mostly
# German/Dutch/Italian/Spanish hotel owners.
_NAME_PARTICLES = (
    "de", "del", "della", "di", "da", "dos", "du", "van", "von", "der", "den",
    "ten", "ter", "op", "la", "le", "el", "bin", "al", "ibn", "y", "e",
)
_NAME_TOKEN = (r"(?:[A-ZÀ-Þ][\w''\-\.]+|" + "|".join(_NAME_PARTICLES) + r")")
# Form 1 — the name inside parentheses: "name found (Michael Oberrauch, GM) …"
_PROSE_NAME_RE = _re.compile(
    r"\(\s*([A-ZÀ-Þ][\w''\-\.]+(?:\s+" + _NAME_TOKEN + r"){1,3})\s*[,)]", _re.UNICODE)
# Form 2 — the name stated inline after a role word, no parentheses at all:
# "Owner Klaus Frühwirth-Stangl identified via FirmenABC.at …",
# "owner identified as Josef Grander (confirmed via Firmenbuch)".
# Worth its own pattern: 8 of the 19 reasons form 1 could not reach are this
# shape. A wrong guess here is self-correcting — it just fails to verify by
# SMTP and the lead stays dropped exactly as before, so a slightly permissive
# pattern costs one probe, never a bad send.
# NOTE the SCOPED (?i:…) around the role words only. A blanket _re.IGNORECASE
# here silently disables the [A-ZÀ-Þ] capitalization requirement in the capture
# group too, so ordinary verbs match as name tokens and the surname comes back
# as "identified"/"via"/"from" — caught in testing against the real reasons.
_PROSE_ROLE_NAME_RE = _re.compile(
    r"(?i:\b(?:owners?|founders?|CEO|managing\s+director|general\s+manager|proprietors?|"
    r"operators?|decision-makers?|Geschäftsführer(?:in)?)\b"
    r"(?:\s+(?:identified|found|confirmed))?(?:\s+as)?)\s+"
    r"([A-ZÀ-Þ][\w''\-\.]+(?:\s+" + _NAME_TOKEN + r"){1,3})",
    _re.UNICODE)
# Honorifics/academic titles that precede a real name in DACH sources and must
# not be mistaken for the first name ("Mag. Luigi von Pasquali", "Dr. Anna Süß").
_HONORIFICS = {"mag", "dr", "prof", "ing", "dipl", "mba", "herr", "frau",
               "mr", "mrs", "ms", "miss", "sra", "dott", "ir"}
# Generational suffixes: "Albert Schwaighofer Jr." must yield Schwaighofer, not "Jr".
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "junior", "senior"}
# A capture ending in a legal-entity suffix is a COMPANY, not a person
# ("Knollseisen GmbH"). Reject it so the other pattern gets its turn — for that
# very lead the role form does hold the real person ("owner Bernhard
# Knollseisen confirmed via company registry").
_COMPANY_SUFFIXES = {"gmbh", "ag", "kg", "og", "bv", "nv", "sa", "srl", "spa",
                     "ltd", "llc", "inc", "plc", "sarl", "sl", "oy", "ab",
                     "as", "aps", "gmbh.", "e.u.", "eu", "co", "holding",
                     "group", "hotel", "hotels", "gastronomie"}


def _person_name_from_match(raw: str) -> tuple[str, str]:
    """Normalize one captured name string to (first, last), or ("","") if it
    does not look like a person."""
    parts = [p for p in raw.split() if p]
    while parts and parts[0].rstrip(".").lower() in _HONORIFICS:
        parts.pop(0)
    while parts and parts[-1].rstrip(".").lower() in _NAME_SUFFIXES:
        parts.pop()
    if len(parts) < 2:
        return "", ""
    if parts[-1].rstrip(".").lower() in _COMPANY_SUFFIXES:
        return "", ""
    first, last = parts[0], parts[-1].rstrip(".")
    if last.lower() in _NAME_PARTICLES or len(last) < 2:
        return first, ""
    return first, last
# Phrases that mean "no individual was actually identified" — never mine a name
# out of a reason that says the search FAILED (e.g. "Business operator
# identified as Familie Schmidhofer, but no individual person's name found").
# WORD-BOUNDARY matched, deliberately: a plain substring test for "no person"
# also fires on "no persONAL email", which is the single most common phrasing
# in these reasons — it silently suppressed ~14 recoverable names (Uwe Schramm,
# Christoph Ursprunger, Christine Loitfelder …) before this was caught.
_NO_PERSON_RE = _re.compile(
    r"\bno\s+(?:individual|named\s+(?:decision-maker|person)|person\b|"
    r"decision-maker\s+found|name\s+found)", _re.IGNORECASE)


def recover_name_from_reason(reason: str) -> tuple[str, str]:
    """Best-effort (first, last) from a name-finder `reason` string.
    Returns ("", "") when no person was actually named. Middle names collapse:
    "Maria Adelheid Scherer" -> ("Maria", "Scherer"), which is what the
    salutation contract needs (a first name alone is send-eligible as
    `Hello <First>,`; no gender required for that form)."""
    text = (reason or "").strip()
    if not text:
        return "", ""
    if _NO_PERSON_RE.search(text):
        return "", ""
    # Try BOTH forms and take the first that normalizes to a real person, so a
    # company-shaped capture in one form falls through to the other instead of
    # losing the lead.
    for pattern in (_PROSE_NAME_RE, _PROSE_ROLE_NAME_RE):
        for m in pattern.finditer(text):
            first, last = _person_name_from_match(m.group(1))
            if first:
                return first, last
    return "", ""

# --- The direct-email gate (module level: phase_merge AND phase_rescue share it,
#     one authoritative definition of "what counts as a direct email") ---------
# Generic mailboxes that must NOT be accepted as the decision-maker's address.
# Even if the name-finder agent returned one of these, treat it as no-email.
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
    # --- ROLE TITLES AND THEIR ABBREVIATIONS (added 2026-08-06) ---------
    # The 2026-08-05 eu-hotels retries SENT four of these as "direct"
    # decision-maker addresses: `hd@` (hotel director), `gm@`,
    # `director@` and `shop@` at four different properties. The
    # name-finder's own reject list already covers them in prose; this
    # deterministic gate is the enforcement point and was thinner than the
    # prompt, so an agent slip shipped straight to Brevo.
    "gm", "hd", "md", "ceo", "coo", "cfo", "cto", "dir",
    "director", "directors", "management", "owner", "founder",
    "boss", "head", "chief", "president", "principal",
    "shop", "store", "retail", "spa", "wellness", "restaurant", "bar",
    "kitchen", "housekeeping", "maintenance", "security", "groups",
    "accounts", "accounting", "finance", "billing", "invoice", "invoices",
    "webmaster", "web", "it", "helpdesk", "mail", "email", "post",
    "stay", "welcome", "hotel", "team", "service", "services", "customer",
    "customerservice", "guest", "guests", "front", "desk", "meetings",
    # --- NON-ENGLISH ROLE WORDS ----------------------------------------
    # This is a 31-country EU campaign; an English-only list is a hole the
    # size of the audience. DE / FR / IT / ES / CZ / SK / PL / NL / HU.
    "direktion", "direktor", "geschaeftsfuehrung", "empfang", "buchung",
    "anfrage", "anfragen", "rezeption", "verwaltung",
    "direction", "accueil", "reservationsfr", "renseignements",
    "direzione", "prenotazioni", "ufficio", "informazioni", "ricevimento",
    "direccion", "reservas", "gerencia", "gerente", "recepcion",
    "recepce", "rezervace", "vedeni", "kancelar",
    "recepcja", "rezerwacje", "biuro",
    "receptie", "reserveringen", "kantoor",
    "igazgato", "recepcio", "foglalas", "iroda",
}
# A role word is still a role word with a qualifier glued on
# (`hotel.director@`, `gm-malta@`, `front_desk@`, `direktion.basel@`).
_ROLE_SEPS = str.maketrans({".": " ", "_": " ", "-": " ", "+": " "})
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "msn.com", "aol.com", "proton.me", "protonmail.com",
}


def is_direct_email(addr: str) -> bool:
    addr = (addr or "").strip().lower()
    if "@" not in addr:
        return False
    local, _, domain = addr.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if local in GENERIC_LOCAL_PARTS:
        return False
    # Qualified role addresses: `hotel.director@`, `gm-malta@`, `front_desk@`.
    # A PERSON's address splits into name parts (`alvaro.ferrandis`), so a
    # role word in a 2-3 token local is the giveaway. Reject when the FIRST
    # token is a role word, or when the whole local is role tokens only —
    # `sarah.jones` and `f.vitek` are untouched by both tests.
    toks = [t for t in local.translate(_ROLE_SEPS).split() if t]
    if toks and len(toks) <= 3:
        if toks[0] in GENERIC_LOCAL_PARTS:
            return False
        if all(t in GENERIC_LOCAL_PARTS for t in toks):
            return False
    if domain in FREE_MAIL_DOMAINS:
        return False
    return True


def looks_like_business_name(last_name: str, business: str) -> bool:
    """True if last_name appears to actually be a fragment of the business
    name (e.g. last_name="Ferrari" while business="Ferrari Dental Clinic").
    We do NOT drop family-business cases where the surname is genuinely
    a family name visible on the business name — we drop only when the
    first significant business-name word equals the last_name AND we have
    no other signal. To stay conservative this only fires when the FIRST
    token of the business equals last_name case-insensitively and the
    business name contains a generic descriptor like Clinic / Hospital /
    Group / Real Estate / Restaurant / Salon / Pharmacy / Center."""
    if not last_name or not business:
        return False
    bn = business.strip()
    ln = last_name.strip().lower()
    if not ln:
        return False
    first_token = bn.split()[0].lower() if bn.split() else ""
    if first_token != ln:
        return False
    descriptors = (
        "clinic", "clinics", "hospital", "polyclinic", "center", "centre",
        "group", "holding", "real estate", "properties", "restaurant",
        "cafe", "salon", "spa", "pharmacy", "dental", "medical", "law",
        "associates", "partners", "insurance", "studio", "academy",
        "hotel", "resort", "motors", "automotive", "lab", "laboratory",
    )
    bn_lower = bn.lower()
    return any(d in bn_lower for d in descriptors)


def load_leads() -> list[dict]:
    leads = []
    for line in EXTRACTED.read_text().splitlines():
        if line.strip():
            leads.append(json.loads(line))
    return leads


def phase_prep() -> None:
    """Write enrich-batch-NNN.txt — one per lead. The orchestrator then
    dispatches one name-finder Haiku per batch in a single message."""
    leads = load_leads()
    # Clear any stale prep files from a prior /fire on this folder.
    WORK.mkdir(parents=True, exist_ok=True)
    for f in WORK.glob("enrich-batch-*.txt"):
        f.unlink()
    for f in WORK.glob("enrich-out-*.json"):
        f.unlink()

    for f in ROOT.glob("derived-contacts.json"):
        f.unlink()

    # raw_html is a RUN artifact (Stage 4 writes it there) — never the work dir.
    raw_dir = ROOT / "raw_html"
    # Every batch file is built from raw_html (harvested addresses + about/team/
    # contact text). Without it the agents start blind: on
    # 2026-09-01-gcc-receptionist a prep re-run after the abort's cleanup gave
    # 0/250 leads any site text (the previous fire: 65/250 with harvested
    # person addresses) and the survival rate fell from 26% to 5%. Refuse to
    # write 250 empty batches silently.
    if not any(raw_dir.glob("*.html")) and os.environ.get("ALLOW_NO_RAW_HTML") != "1":
        raise SystemExit(
            f"ABORT: Stage 5.5 prep: {raw_dir} is missing or holds no pages, so every "
            f"enrich batch would carry no SitePages and no harvested addresses. "
            f"Re-run Stage 4 (bash tools/scripts/fetch_html.sh {ROOT}) first, or set "
            f"ALLOW_NO_RAW_HTML=1 to dispatch blind on purpose.")
    with_personal = 0
    with_pages = 0
    # DERIVE_FIRST (user directive, re-affirmed 2026-07-31): when the site's own
    # email already carries the decision-maker's name, PARSE it instead of paying
    # a name-finder agent to go looking for it. Only the leads this cannot answer
    # are dispatched. Set DERIVE_NAMES_FROM_EMAIL=0 to force the old
    # every-lead-gets-an-agent behaviour.
    derive_on = (nfe is not None
                 and os.environ.get("DERIVE_NAMES_FROM_EMAIL", "1") != "0")
    derived: list[dict] = []
    # Conventions proven by earlier fires — replayed into the batch so the agent
    # applies a known format instead of searching for it again.
    fmt_ledger = load_format_ledger()
    n_fmt_hits = 0
    n_batches = 0
    for lead in leads:
        country_name = COUNTRY_NAMES.get(lead["country_code"], lead["country_code"])

        # Tool-backed email harvest from the crawl4ai-rendered pages (Stage 4).
        # Gives the agent REAL site emails to match/reconstruct from before it
        # ever resorts to a constructed address.
        domain = lead["website"].replace("https://", "").replace("http://", "").strip("/")
        harvested = {"personal": [], "role": [], "offdomain": [], "freemail": []}
        if harvest_domain is not None:
            try:
                harvested = harvest_domain(domain, raw_dir)
            except Exception:
                pass
        personal_pool = harvested.get("personal", []) + harvested.get("offdomain", [])
        if personal_pool:
            with_personal += 1

        # wa.me and tel: phone links extracted from raw HTML (Stage 4 output).
        # These are the highest-signal mobile candidates — the company's OWN
        # published WhatsApp/phone numbers. Passed to name-finder so it checks
        # them BEFORE spending tokens on web searches. Only harvested when phone
        # enrichment is enabled (--enrich-phone); an email-only run never uses a
        # number, so we neither harvest nor hand any to the agent.
        phone_links: list[str] = []
        if args.enrich_phone and harvest_phone_links is not None:
            try:
                phone_links = harvest_phone_links(domain, raw_dir)
            except Exception:
                pass

        # Readable text from the about/team/leadership/contact pages we ALREADY
        # scraped in Stage 4. Handing these to the agent means it reads the
        # decision-maker bios it needs WITHOUT re-fetching them over the web —
        # fewer tokens, fewer tool round-trips, faster, same result.
        pages_text = ""
        if extract_page_text is not None:
            try:
                pages_text = extract_page_text(domain, raw_dir)
            except Exception:
                pass
        if pages_text:
            with_pages += 1
        pages_block = pages_text if pages_text else "(no about/team/contact pages scraped — search the web)"

        def _fmt(lst):
            return ", ".join(lst) if lst else "(none found on site)"

        # --- DERIVE_FIRST: can the name be read straight off an address? -----
        # Candidate pool, best first: the lead's own chosen mailbox, then every
        # person-format address harvested from its pages. The parse is strict
        # (known given name + role/brand/placeholder nets + page corroboration
        # for single tokens) so an unparseable lead simply falls through to an
        # agent — the same outcome it had before, minus the guessing.
        if derive_on:
            hit = None
            for cand in ([lead.get("to_email") or ""] + personal_pool):
                got = nfe.derive(cand, lead.get("name", ""), domain, pages_text)
                if got:
                    hit = (cand, got)
                    break
            if hit:
                cand, got = hit
                derived.append({
                    "lead_id": lead["lead_id"],
                    "found": True,
                    "first_name": got["first_name"],
                    "last_name": got["last_name"],
                    "title": "",              # no gender is inferable from an address
                    "role": "",
                    "email": cand,
                    "email_basis": "verbatim",     # the address was published on the site
                    "email_source_url": lead["website"],
                    "source_url": lead["website"],
                    "confidence": "medium",
                    "name_basis": got["basis"],
                    "reason": got["evidence"],
                })
                continue          # no enrich-batch, so no agent for this lead

        n_batches += 1
        nnn = f"{n_batches:03d}"
        batch_path = WORK / f"enrich-batch-{nnn}.txt"
        out_path = WORK / f"enrich-out-{nnn}.json"
        target_roles_line = f"TargetRoles: {TARGET_ROLES}\n" if TARGET_ROLES else ""
        _bare = domain.split("/")[0].removeprefix("www.").lower()
        _tpl, _url = fmt_ledger.get(_bare, ("", ""))
        if _tpl:
            n_fmt_hits += 1
        fmt_line = (f"KnownEmailFormat: {_tpl}"
                    + (f"   (proven earlier at {_url})" if _url else "")
                    + "\n") if _tpl else ""
        batch_path.write_text(
            f"LeadId: {lead['lead_id']}\n"
            f"Business: {lead['name']}\n"
            f"Country: {lead['country_code']}  ({country_name})\n"
            f"Vertical: {lead['vertical']}\n"
            f"Website: {lead['website']}\n"
            f"SitePersonalEmails: {_fmt(personal_pool)}\n"
            f"SiteRoleEmails: {_fmt(harvested.get('role', []))}\n"
            f"SiteFreemail: {_fmt(harvested.get('freemail', []))}\n"
            f"SitePhoneLinks: {_fmt(phone_links)}\n"
            f"EnrichPhone: {'yes' if args.enrich_phone else 'no'}\n"
            f"{target_roles_line}"
            f"{fmt_line}"
            f"OutputFile: {out_path}\n"
            f"\n"
            f"SitePages (already-scraped text — READ THIS BEFORE any web fetch):\n"
            f"{pages_block}\n"
        )
    if derived:
        DERIVED.write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n")
    print(f"Prepped {n_batches} enrich batches in {WORK}"
          + (f" (work dir; run dir {ROOT})" if WORK != ROOT else ""))
    if TARGET_ROLES:
        print(f"  TargetRoles override active: {TARGET_ROLES}")
    if derive_on:
        from collections import Counter
        how = Counter(d["name_basis"] for d in derived)
        print(f"  name-from-email: {len(derived)}/{len(leads)} decision-makers parsed "
              f"straight off a site address — {len(derived)} agents NOT dispatched "
              f"({dict(how) if how else 'none matched'}).")
    if fmt_ledger:
        print(f"  email-format ledger: {n_fmt_hits}/{n_batches} batch(es) carry a convention "
              f"proven by an earlier fire ({len(fmt_ledger)} domain(s) known) — those agents "
              f"apply it instead of searching for it.")
    print(f"  {with_personal}/{len(leads)} have >=1 person-format email harvested from the site "
          f"(verbatim candidates / format examples for the name-finder).")
    print(f"  {with_pages}/{len(leads)} have pre-scraped about/team/contact text injected "
          f"(agent reads these instead of re-fetching the web).")
    print(f"Dispatch {n_batches} parallel name-finder Haiku agents from the orchestrator.")


@functools.lru_cache(maxsize=None)
def domain_accepts_mail(domain: str) -> bool:
    """Free DNS-level deliverability check: a domain with no MX and no A record
    cannot receive mail — every send to it is a guaranteed hard bounce (this is
    what let invented domains like @kimptonatlantico.com ship). Uses `dig`;
    if dig is unavailable the check passes open (never blocks a run on tooling)."""
    if shutil.which("dig") is None:
        return True
    for rtype in ("MX", "A"):
        try:
            r = subprocess.run(["dig", "+short", "+time=3", "+tries=1", rtype, domain],
                               capture_output=True, text=True, timeout=8)
            if r.stdout.strip():
                return True
        except Exception:
            return True  # resolver trouble: pass open, don't kill the run on infra
    return False


CONSTRUCTED_BASES = {"pattern_inferred", "reconstructed_from_mask"}

GENERIC_HARVEST_LOCALS = {
    "info", "contact", "hello", "support", "admin", "office", "sales",
    "bookings", "booking", "reservations", "reception", "frontdesk",
    "careers", "hr", "jobs", "marketing", "press", "events", "noreply",
}


@functools.lru_cache(maxsize=None)
def _site_has_person_format(domain: str) -> bool:
    """True if the business's OWN scraped pages contain at least one
    person-format same-domain address (e.g. sarah.jones@domain) — that is real,
    auditable format evidence for a constructed address even when the agent
    failed to cite a URL for it."""
    if harvest_domain is None:
        return False
    try:
        harvested = harvest_domain(domain, ROOT / "raw_html")
    except Exception:
        return False
    for addr in harvested.get("personal", []):
        local, _, dom = addr.lower().partition("@")
        if dom == domain and local and local not in GENERIC_HARVEST_LOCALS:
            return True
    return False


# --- Catch-all construction from site-observed format (2026-08-20) ----------
# THE PROBLEM THIS SOLVES, measured: on a 14-domain live sample of the
# 2026-08-20-eu-hotels drops, 6 (43%) were CATCH-ALL — the mail server accepts
# every address, so an SMTP probe can prove nothing and those leads die even
# though the decision-maker's name is known. That was the single largest
# remaining bucket.
#
# THE CONSTRAINT, from this repo's own history: "Constructed addresses were 66%
# of sends and drove the 21% hard-bounce rate." So construction is allowed ONLY
# where the business's OWN scraped pages show a real person-format address on
# the same domain, whose shape tells us the company's convention. That is the
# same bar name-finder's step 2c applies, made deterministic. NO site evidence
# -> no construction, the lead stays dropped. This never guesses blind.
#
# Turn off with SMTP_RESCUE_CATCHALL=0 if Brevo bounce rate rises.
_SEPS = (".", "_", "-")


# --- Per-domain email-convention ledger (2026-09-04) -------------------------
# The convention is a fact about the DOMAIN, not about one lead:
# {first}.{last}@easyhotel.com holds for every easyHotel lead, in this fire and
# every future one. Measured on 2026-09-04-eu-hotels: 25 of the 40 addresses the
# name-finders closed were pattern_inferred, i.e. a convention discovered by
# search and then applied — and multi-property groups repeat (Louvre Hotels
# turned up with a stated 89.9% format). Learning it once and replaying it turns
# that search into a free lookup, and the saving compounds across fires.
#
# Format: one "domain<TAB>template<TAB>evidence-url" row, newest wins on read.
# Templates use smtp_email_probe's field names: {first} {last} {f}.
FORMAT_LEDGER = Path("vault/lead-outreach/email-formats.txt")


def load_format_ledger() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    try:
        for line in FORMAT_LEDGER.read_text().splitlines():
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2 and parts[0] and parts[1]:
                out[parts[0].strip().lower()] = (parts[1].strip(),
                                                 parts[2].strip() if len(parts) > 2 else "")
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def infer_template(first: str, last: str, local: str) -> str:
    """Which convention produced this local part? "" when it is not decidable.

    Only STRUCTURAL, name-anchored answers count — the local part must actually
    be built from the person's name. `artboutique` for Dominik Zurbrügg yields
    "" rather than a fictional convention, which is the same failure the
    provider's name-link guard exists to stop."""
    f = _re.sub(r"[^a-z]", "", _ascii_fold(first))
    s = _re.sub(r"[^a-z]", "", _ascii_fold(last))
    l = (local or "").strip().lower()
    if not l or not (f or s):
        return ""
    for sep in (".", "_", "-"):
        if f and s and l == f + sep + s:
            return "{first}" + sep + "{last}"
        if f and s and l == f[0] + sep + s:
            return "{f}" + sep + "{last}"
    if f and s and l == f + s:
        return "{first}{last}"
    if f and s and l == f[0] + s:
        return "{f}{last}"
    if f and l == f:
        return "{first}"
    if s and l == s:
        return "{last}"
    return ""


def _ascii_fold(s: str) -> str:
    import unicodedata
    t = (s or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        t = t.replace(a, b)
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()


def remember_format(domain: str, template: str, evidence_url: str) -> None:
    """Append a proven convention. Never overwrites: the file is a log and the
    newest row wins on read, so a domain that changes provider self-corrects.

    A FREEMAIL domain is never learned. gmail.com happens to have produced a
    clean `{first}.{last}` on this run's data, but "the convention at gmail.com"
    is a category error: caching it would fabricate an address for every future
    lead whose owner uses Gmail. Only a domain the business actually controls
    has a convention."""
    if not (domain and template):
        return
    try:
        from email_utils import FREEMAIL
        if domain.lower() in FREEMAIL:
            return
    except Exception:
        if domain.lower() in {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com",
                              "outlook.com", "icloud.com", "gmx.net", "gmx.de", "web.de",
                              "bluewin.ch", "aol.com", "proton.me", "protonmail.com"}:
            return
    try:
        FORMAT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with FORMAT_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(f"{domain.lower()}\t{template}\t{evidence_url or ''}\n")
    except Exception:
        pass                      # a ledger write must never fail a run


@functools.lru_cache(maxsize=None)
def _site_email_format(domain: str) -> tuple[str, str]:
    """Infer the company's personal-email convention from addresses harvested
    off its own site. Returns (template, evidence) or ("", "").

    Templates use the same field names as smtp_email_probe: {first} {last} {f}.
    Inference is STRUCTURAL — we do not know whose addresses these are, only
    their shape, which is exactly what the convention is:
        sarah.jones@ -> "{first}.{last}"      s.jones@ -> "{f}.{last}"
        sarah@       -> "{first}"             sjones@  -> (ambiguous, skipped)
    """
    if harvest_domain is None:
        return "", ""
    try:
        harvested = harvest_domain(domain, ROOT / "raw_html")
    except Exception:
        return "", ""
    for addr in harvested.get("personal", []):
        local, _, dom = addr.lower().partition("@")
        # Filter with is_direct_email, NOT the thin GENERIC_HARVEST_LOCALS set.
        # That set omits "hotel", "welcome", "direktion", "rezeption" and the
        # rest of the multilingual role vocabulary, so `hotel@dollinger.at`
        # read as a bare-first-name convention and "proved" a format that does
        # not exist — construction with no real evidence, which is precisely
        # the bounce risk this whole path is fenced against. is_direct_email is
        # the one authoritative definition; use it here too.
        if dom != domain or not local or not is_direct_email(addr.lower()):
            continue
        for sep in _SEPS:
            if sep in local:
                head, _, tail = local.partition(sep)
                if not head or not tail or not head.isalpha() or not tail.isalpha():
                    continue
                tpl = ("{f}" + sep + "{last}") if len(head) == 1 else ("{first}" + sep + "{last}")
                return tpl, addr
        # No separator: a bare first name is a real convention ("christine@").
        # A run-together "sjones" is ambiguous between {f}{last} and {first}{last},
        # so it is deliberately NOT used as evidence.
        if local.isalpha() and 2 <= len(local) <= 12:
            return "{first}", addr
    return "", ""


def build_from_site_format(first: str, last: str, domain: str) -> tuple[str, str]:
    """Construct this person's address in the company's observed convention.
    Returns (email, evidence_note), or ("", "") when there is no evidence or
    the name lacks the component the convention needs."""
    tpl, example = _site_email_format(domain)
    if not tpl or smtp_probe is None:
        return "", ""
    f, l = smtp_probe.clean(first), smtp_probe.clean(last)
    if "{last}" in tpl and not l:
        return "", ""
    if ("{first}" in tpl or "{f}" in tpl) and not f:
        return "", ""
    local = tpl.format(first=f, last=l, f=f[:1])
    if not local:
        return "", ""
    return f"{local}@{domain}", f"company email convention observed on its own site ({example})"


def phase_merge() -> None:
    """Read enrich-out-*.json, join to leads-extracted.json, drop leads with
    no name+gender, write leads-with-contact.json."""
    leads = load_leads()
    by_id = {l["lead_id"]: l for l in leads}

    # --- fan-out completion guard (kill-on-fallback) ---------------------
    # 2026-06-25-eu-hotels prepped 94 batches, only 24 agents ever wrote an
    # output, and the run shipped 15 sends as if nothing was wrong. A partial
    # fan-out is a degraded run: halt instead of silently sending a fraction.
    n_batches = len(list(WORK.glob("enrich-batch-*.txt")))
    n_outs = len(list(WORK.glob("enrich-out-*.json")))
    min_completion = float(os.environ.get("ENRICH_MIN_COMPLETION", "0.6"))
    if n_batches and n_outs / n_batches < min_completion:
        print(f"ABORT: enrichment fan-out incomplete — {n_outs}/{n_batches} agent outputs "
              f"(<{min_completion:.0%}). A degraded run must not ship (kill-on-fallback). "
              f"Re-dispatch the missing agents or re-fire; ENRICH_MIN_COMPLETION overrides.")
        raise SystemExit(7)

    # Contacts parsed off a site address in prep. These had NO agent dispatched,
    # so they are seeded here and then face EVERY gate below (direct-email,
    # business-as-surname, DNS) exactly like an agent result — from this point on
    # the two routes are indistinguishable, which is the whole point.
    enriched = {}
    n_derived = 0
    if DERIVED.exists():
        try:
            for d in json.loads(DERIVED.read_text()):
                if d.get("lead_id"):
                    enriched[d["lead_id"]] = d
                    n_derived += 1
        except Exception:
            print("  WARN: derived-contacts.json unreadable; falling back to agents only.")

    out_files = sorted(WORK.glob("enrich-out-*.json"))
    parse_errors = 0
    for f in out_files:
        try:
            data = json.loads(f.read_text())
        except Exception:
            parse_errors += 1
            continue
        lead_id = data.get("lead_id")
        if not lead_id:
            continue
        enriched[lead_id] = data

    survivors = []
    dropped = []  # lightweight audit trail — see note above WITH_CONTACT.write_text below
    dropped_no_name = 0
    dropped_no_match = 0
    dropped_no_email = 0
    dropped_business_as_surname = 0
    dropped_no_evidence_url = 0
    dropped_dead_domain = 0
    wa_fallback_kept = 0
    smtp_rescued = 0
    smtp_probed = 0
    catchall_built = 0

    # domain -> (template, evidence url) proven by a lead that cleared every gate
    # this run; appended to the ledger at the end so the next fire skips the search.
    learned_formats: dict[str, tuple[str, str]] = {}

    # Domains this stage PROVED cannot be reached: the agent did its research and
    # reported no direct decision-maker email. Retired permanently (see
    # _retire_unreachable below) rather than queued for an identical retry.
    unreachable: list[tuple[str, str]] = []

    def _lead_domain(lead) -> str:
        site = (lead.get("website") or "").strip().lower()
        return site.split("//")[-1].split("/")[0].removeprefix("www.")

    # Drop stages that are PROOF about the business itself, and so retire the
    # domain permanently. Everything else (a malformed agent payload, a brand
    # parsed as a surname, missing URL evidence) is a defect in one agent run,
    # not a fact about the lead — those stay retryable.
    #
    # REACHABILITY IS NOT A VERDICT ON FIT (2026-08-18). These stages still
    # record the domain — the evidence is worth keeping — but "we could not find
    # an address for this owner today" is not permanent proof about the business
    # the way a dead domain or a too-small hotel is. Left permanent it had become
    # the single largest cause of bans: 731 of the 1,352 rows in
    # disqualified-log.txt (54%), against 498 for the only genuinely fit-based
    # reason. What it discarded is visible in the drop reasons themselves —
    # "name found (Maria Banti, Founder & Laboratory Director) but no direct
    # email". So the row is still WRITTEN here, and expiry is enforced on the
    # READ side: load_disqualified_domains() in merge_candidates.py blocks a
    # `no-direct-email*` row only for NO_EMAIL_RETRY_DAYS (default 90), while
    # dead-domain and the fit-based reasons stay permanent.
    #
    # Writing it (rather than dropping it) is what keeps the 2026-08-05 churn
    # from returning: three fires that day each spent ~2h and 208 name-finder
    # agents on the SAME 208 hotels. Within the window they are still skipped.
    RETIRING_STAGES = {"no_match": "no-direct-email",
                       "no_direct_email": "no-direct-email",
                       "dead_domain": "dead-domain"}

    def _record_drop(lead, stage, result=None):
        r = result or {}
        # The agent routinely FINDS the person and fails only on the email
        # (3,760 of 4,095 historical drops). Keep the name it paid to find —
        # discarding it meant every rerun researched the same person from zero.
        dropped.append({
            "lead_id": lead["lead_id"],
            "name": lead.get("name", ""),
            "hotel_volume": lead.get("hotel_volume", ""),
            "score": lead.get("score", ""),
            "drop_stage": stage,
            "agent_reason": r.get("reason", ""),
            "found_first_name": (r.get("first_name") or "").strip(),
            "found_last_name": (r.get("last_name") or "").strip(),
            "found_title": (r.get("title") or "").strip(),
            "found_role": (r.get("role") or "").strip(),
            "found_source_url": (r.get("source_url") or "").strip(),
            "smtp_probe_note": (r.get("smtp_probe_note") or "").strip(),
        })
        # Retire ONLY on a real verdict. An agent that crashed or timed out
        # returns no output at all (result is None) and proved nothing about the
        # business — 112 of the historical drops were exactly this — so it must
        # never reach the permanent ledger.
        if result is None or stage not in RETIRING_STAGES:
            return
        dom = _lead_domain(lead)
        if dom:
            unreachable.append((dom, RETIRING_STAGES[stage]))

    # --- SMTP rescue, pass 1: decide WHO gets probed and run the probes -------
    # SMTP RESCUE (2026-08-20, always on — free, no channel flag, no browser,
    # no third-party API). name-finder usually finds the right person and
    # fails only on the email (see name-finder.md's found:false-with-name
    # addition); before giving up, probe the LEAD'S OWN domain by SMTP RCPT for
    # the common pattern candidates. See smtp_email_probe.py for exactly how
    # and its measured, honest limitations (catch-all domains report
    # `catch_all`, never a false `verified`). A verified hit still runs through
    # the SAME is_direct_email()/looks_like_business_name gates below as any
    # other candidate — an SMTP accept that only coincidentally matches a role
    # mailbox (e.g. "gm@" by initials) is still caught and rejected downstream.
    #
    # WHY THE PROBES RUN AHEAD OF THE LOOP, IN PARALLEL (2026-09-02). Measured
    # on 2026-09-02-gcc-receptionist: 71 probes ran one after another inside
    # the per-lead loop, 14.7 minutes, ~12.4 s each, for 5 verified. Every
    # probe talks to a DIFFERENT mail server (the lead's own MX), so running
    # them concurrently keeps the module's one-session-per-domain politeness
    # intact — no server sees more than it did before, they just overlap. The
    # decision of who gets probed, and every gate applied to the answer, is
    # byte-identical to the serial version; only the scheduling moved.
    # SMTP_WORKERS sizes the pool (default 16). SMTP_PROBE=0 (set by run_fire's
    # preflight when port25_reachable() is False) skips the probes entirely
    # instead of paying N x timeout to learn nothing.
    smtp_enabled = os.environ.get("SMTP_PROBE", "1") not in ("0", "false", "no")
    smtp_workers = max(1, int(os.environ.get("SMTP_WORKERS", "16")))
    rescue_plan: dict[str, tuple[str, str, str, str]] = {}   # lead_id -> (first, last, basis, domain)
    for lead in leads:
        result = enriched.get(lead["lead_id"])
        if result and result.get("found"):
            continue
        r = result or {}
        first_try = (r.get("first_name") or "").strip()
        last_try = (r.get("last_name") or "").strip()
        name_basis = "structured"
        if not (first_try or last_try):
            # The agent named the person in prose but left the structured
            # fields empty — 55 of 80 drops on 2026-08-20-eu-hotels. Mine
            # the name it already wrote rather than discard the lead.
            first_try, last_try = recover_name_from_reason(r.get("reason", ""))
            name_basis = "prose-recovered"
        domain_try = _lead_domain(lead)
        if (first_try or last_try) and domain_try and smtp_probe is not None:
            rescue_plan[lead["lead_id"]] = (first_try, last_try, name_basis, domain_try)

    probes: dict[str, dict] = {}
    if rescue_plan and smtp_enabled:
        import concurrent.futures
        t0 = time.monotonic()
        print(f"  SMTP rescue: probing {len(rescue_plan)} name-found-no-email lead(s) "
              f"on {min(smtp_workers, len(rescue_plan))} worker(s)…", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=smtp_workers) as pool:
            futs = {pool.submit(smtp_probe.probe_person, f_, l_, d_, SMTP_MAIL_FROM): lid
                    for lid, (f_, l_, _b, d_) in rescue_plan.items()}
            for fut in concurrent.futures.as_completed(futs):
                probes[futs[fut]] = fut.result()
        print(f"  SMTP rescue: {len(probes)} probe(s) done in "
              f"{time.monotonic() - t0:.0f}s", flush=True)
    elif rescue_plan:
        print(f"  SMTP rescue: SKIPPED for {len(rescue_plan)} lead(s) — SMTP_PROBE=0 "
              f"(port 25 unreachable from this network per preflight)")

    # --- pass 2: the per-lead gates, using the collected probe results --------
    for lead in leads:
        result = enriched.get(lead["lead_id"])
        if not result or not result.get("found"):
            r = result or {}
            rescued = False
            plan = rescue_plan.get(lead["lead_id"])
            if plan is not None and not smtp_enabled:
                result = {**r, "smtp_probe_note": "skipped: port 25 unreachable"}
            elif plan is not None:
                first_try, last_try, name_basis, domain_try = plan
                smtp_probed += 1
                probe = probes[lead["lead_id"]]
                if probe["status"] == "verified":
                    result = {**r, "found": True,
                              "first_name": first_try, "last_name": last_try,
                              "email": probe["email"],
                              "email_basis": "smtp_verified", "confidence": "medium",
                              "email_source_url": r.get("source_url", ""),
                              "email_evidence_note": f"SMTP RCPT accepted by {domain_try}'s "
                                                      f"own mail server (protocol-level check, "
                                                      f"not a guess); name {name_basis}"}
                    smtp_rescued += 1
                    rescued = True
                elif probe["status"] == "catch_all" and CATCHALL_CONSTRUCT:
                    # The mail server accepts EVERY address, so SMTP can prove
                    # nothing here (43% of a measured 14-domain sample). Fall
                    # back to the company's OWN observed convention — real,
                    # citable evidence off its own pages — or leave it dropped.
                    built, note = build_from_site_format(first_try, last_try, domain_try)
                    if built:
                        result = {**r, "found": True,
                                  "first_name": first_try, "last_name": last_try,
                                  "email": built,
                                  "email_basis": "pattern_inferred", "confidence": "medium",
                                  "email_source_url": r.get("source_url", ""),
                                  "email_evidence_note": f"{note}; domain is catch-all so SMTP "
                                                          f"cannot verify individuals; name {name_basis}"}
                        catchall_built += 1
                        rescued = True
                    else:
                        result = {**r, "smtp_probe_note":
                                  f"catch-all domain and no personal-email format observable on "
                                  f"its own site, so nothing citable to construct from; "
                                  f"name {name_basis} as {first_try} {last_try}".rstrip()}
                else:
                    result = {**r, "smtp_probe_note": f"SMTP rescue also failed: {probe['status']} "
                                                        f"({probe['note']}); name {name_basis} "
                                                        f"as {first_try} {last_try}".rstrip()}
            if not rescued:
                dropped_no_match += 1
                _record_drop(lead, "no_match", result)
                continue
        first = (result.get("first_name") or "").strip()
        last = (result.get("last_name") or "").strip()
        title = (result.get("title") or "").strip()
        # One name component is enough (2026-07-22 directive) — the verified
        # direct email is the real bar. Salutation forms:
        #   last + Mr./Mrs.  -> "Hello Mr./Mrs. <Surname>,"
        #   first (any title)-> "Hello <First>," (gender not needed)
        surname_ok = bool(last) and title in {"Mr.", "Mrs."}
        if not (surname_ok or first):
            dropped_no_name += 1
            _record_drop(lead, "name_or_gender_missing", result)
            continue
        if title not in {"Mr.", "Mrs."}:
            title = ""
        # The gate below exists to catch an agent inventing a surname out of the
        # brand ("Maida Law Firm" -> a fictional "Mr. Maida"). But eponymous
        # firms are the NORM in law, clinics and trades, so on 2026-08-26-us-law-firms
        # it killed 13 leads of which 9 were correct founder identifications
        # (Angelyne Lisinski of Lisinski Law Firm, Brent Gunderson of Gunderson
        # Law Group, ...) that the agent had already evidenced. The address is the
        # tiebreaker: a local-part that carries the surname AND something more
        # (`larry.schultis@`, `lschultis@`) is a person's mailbox, whereas a bare
        # `maida@maidalawfirm.com` is indistinguishable from a brand mailbox and
        # still fails the gate.
        eponym_email_proof = False
        if last:
            local = (result.get("email") or "").strip().lower().split("@")[0]
            local = "".join(ch for ch in local if ch.isalnum())
            surname = "".join(ch for ch in last.lower() if ch.isalnum())
            given = "".join(ch for ch in first.lower() if ch.isalnum())
            # Either component in the local-part proves a person's mailbox, per the
            # one-name-is-enough contract: `larry.schultis@` / `bgunderson@` carry the
            # surname plus more, `nomaan@husainlaw.com` carries the first name. A bare
            # `maida@maidalawfirm.com` carries only the brand and stays dropped.
            eponym_email_proof = (bool(surname) and surname in local and local != surname) \
                or (bool(given) and given in local)
        if (last and looks_like_business_name(last, lead.get("name", ""))
                and result.get("confidence") != "high" and not eponym_email_proof):
            dropped_business_as_surname += 1
            _record_drop(lead, "business_as_surname", result)
            continue
        phone = (result.get("phone") or "").strip()
        # Strip everything but digits, then re-prefix `+` for storage. Empty stays empty.
        digits = "".join(ch for ch in phone if ch.isdigit())
        phone_stored = f"+{digits}" if digits else ""

        # Email gate. Normally a failure here kills the lead. With --wa-fallback
        # (dual-channel runs), a lead that honors the name contract AND carries a
        # resolved phone is kept as a WhatsApp-only lead instead: no verified
        # direct email for the owner/CEO -> reach the owner on WhatsApp.
        wa_only = False
        wa_gate = ""
        email = (result.get("email") or "").strip().lower()
        if not is_direct_email(email):
            if args.wa_fallback and phone_stored:
                wa_only, wa_gate = True, "no_direct_email"
            else:
                dropped_no_email += 1
                _record_drop(lead, "no_direct_email", result)
                continue
        if not wa_only:
            basis = (result.get("email_basis") or "").strip()
            evidence_url = (result.get("email_source_url") or result.get("source_url") or "").strip()
            if basis in CONSTRUCTED_BASES:
                # A constructed address needs REAL, auditable format evidence — a URL,
                # not prose ("RocketReach analysis suggests 93.8%…"). Constructed
                # addresses were 66% of sends and drove the 21% hard-bounce rate.
                # VOLUME-SAVING FALLBACK: when the agent cited prose instead of the
                # URL it visited, a same-domain person-format email harvested from
                # the business's OWN scraped pages is equally real format evidence —
                # accept on that basis instead of throwing the lead away.
                if not evidence_url.lower().startswith(("http://", "https://")):
                    if _site_has_person_format(email.split("@", 1)[1]):
                        result["email_evidence_note"] = "site-harvested same-domain person email (merge fallback)"
                    elif args.wa_fallback and phone_stored:
                        wa_only, wa_gate = True, "constructed_no_url_evidence"
                    else:
                        dropped_no_evidence_url += 1
                        _record_drop(lead, "constructed_no_url_evidence", result)
                        continue
                # The spec caps reconstructed addresses at medium confidence.
                if not wa_only and (result.get("confidence") or "").strip() == "high":
                    result["confidence"] = "medium"
        # DNS gate: the recipient domain must actually accept mail (MX or A).
        if not wa_only and not domain_accepts_mail(email.split("@", 1)[1]):
            if args.wa_fallback and phone_stored:
                wa_only, wa_gate = True, "dead_domain"
            else:
                dropped_dead_domain += 1
                _record_drop(lead, "dead_domain", result)
                continue
        if wa_only:
            email = ""          # never email a lead that failed the email gate
            wa_fallback_kept += 1
        lead = {**lead,
                "contact_first_name": first,
                "contact_last_name": last,
                "contact_title": title,
                "contact_role": result.get("role", ""),
                "contact_source_url": result.get("source_url", ""),
                "contact_email": email,
                "contact_email_basis": result.get("email_basis", ""),
                "contact_email_source_url": (result.get("email_source_url") or result.get("source_url") or ""),
                "contact_phone": phone_stored,
                "contact_phone_source_url": (result.get("phone_source_url") or "") if phone_stored else "",
                "contact_channel": "whatsapp_fallback" if wa_only else "email",
                "contact_wa_fallback_reason": wa_gate,
                "contact_confidence": result.get("confidence", "low")}
        survivors.append(lead)
        # Learn this domain's convention from a lead that PASSED every gate, so
        # the next fire replays it instead of paying to rediscover it. Only a
        # structurally name-anchored local part teaches anything (see
        # infer_template), so a shared mailbox that slipped a gate teaches
        # nothing rather than poisoning the ledger.
        if email:
            _local, _, _dom = email.partition("@")
            _tpl = infer_template(first, last, _local)
            if _tpl and _dom:
                learned_formats[_dom.lower()] = _tpl, (
                    result.get("email_source_url") or result.get("source_url") or "")

    # ONE address may only represent ONE business (added 2026-08-06).
    # A hotel group's shared mailbox gets attributed to every property the agent
    # researches: on 2026-08-05 one group mailbox came back as the decision-maker
    # for two different member properties, and a second group's two directors
    # covered two more between them. Pitching
    # the same person once per property is the spam pattern the sent-log exists
    # to prevent, and it burns the address for every future campaign.
    # Keep the highest-scoring lead per address; drop the rest.
    seen_addr: dict[str, dict] = {}
    dup_dropped = 0
    for l in sorted(survivors, key=lambda x: -x["score"]):
        a = (l.get("contact_email") or "").strip().lower()
        if not a:                       # WhatsApp-only leads carry no address
            continue
        if a in seen_addr:
            l["_dup_of"] = seen_addr[a]["lead_id"]
            dup_dropped += 1
        else:
            seen_addr[a] = l
    if dup_dropped:
        for l in survivors:
            if l.get("_dup_of"):
                _record_drop(l, "duplicate_contact_email",
                             {"reason": f"contact_email already used for {l['_dup_of']} "
                                        f"(shared group mailbox, not this business's own)"})
        survivors = [l for l in survivors if not l.get("_dup_of")]
        print(f"  dropped duplicate contact_email: {dup_dropped} "
              f"(same address resolved for more than one business)")

    survivors.sort(key=lambda l: -l["score"])
    WITH_CONTACT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in survivors) + "\n")

    from collections import Counter
    basis = Counter(s.get("contact_email_basis") or "unknown" for s in survivors)
    verbatim = basis.get("verbatim", 0)
    n_from_email = sum(1 for s in survivors
                       if (enriched.get(s["lead_id"]) or {}).get("name_basis"))
    print(f"Merged contact-person enrichment for {len(leads)} leads:")
    print(f"  name parsed from a site address (no agent): {n_derived} "
          f"-> {n_from_email} of them survived every gate")
    print(f"  survived (name + Mr./Mrs. + direct email): {len(survivors)}")
    if args.wa_fallback:
        print(f"  of which WhatsApp-fallback (no verified email, phone kept): {wa_fallback_kept}")
    if survivors:
        print(f"  email basis: {dict(basis)} "
              f"({verbatim}/{len(survivors)} verbatim, rest inferred — watch Brevo bounces)")
    known_before = load_format_ledger()
    new_formats = {d: v for d, v in learned_formats.items()
                   if known_before.get(d, ("", ""))[0] != v[0]}
    for _d, (_t, _u) in new_formats.items():
        remember_format(_d, _t, _u)
    if new_formats:
        print(f"  email-format ledger: learned {len(new_formats)} new domain convention(s) "
              f"-> {FORMAT_LEDGER} (replayed by every future fire, no search needed)")

    n_retired = _retire_unreachable(unreachable)
    if smtp_probed:
        print(f"  SMTP rescue: {smtp_probed} name-found-no-email lead(s) probed, "
              f"{smtp_rescued} SMTP-verified"
              + (f", {catchall_built} built from the site's own observed email format "
                 f"on catch-all domains" if catchall_built else "")
              + f" -> {smtp_rescued + catchall_built} rescued")
    print(f"  dropped no-match:                {dropped_no_match}")
    print(f"  dropped name-or-gender-missing:  {dropped_no_name}")
    print(f"  dropped business-name-as-surname: {dropped_business_as_surname}")
    print(f"  dropped no-direct-email:         {dropped_no_email}")
    print(f"  dropped constructed-no-URL-evidence: {dropped_no_evidence_url}")
    print(f"  dropped dead-domain (no MX/A):   {dropped_dead_domain}")
    print(f"  enrich-out parse errors:         {parse_errors}")
    print(f"  RETIRED to disqualified-log:     {n_retired} domains proven unreachable "
          f"(no direct decision-maker email) — never sourced or enriched again")
    print(f"Wrote {WITH_CONTACT}")
    # Per-lead drop audit trail. enrich-summary.json below only has counts;
    # this is what lets a future run actually diagnose WHY leads died instead
    # of just how many (this file survives cleanup — matches the leads-*.json
    # glob that Step 9.5 explicitly keeps).
    (ROOT / "leads-dropped.json").write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in dropped) + ("\n" if dropped else ""))
    # Persist the gate counters — post-run cleanup deletes the per-agent
    # enrich-out files, so this summary is the only surviving diagnostic of
    # WHERE leads died in enrichment.
    (ROOT / "enrich-summary.json").write_text(json.dumps({
        "batches": n_batches, "agent_outputs": n_outs,
        "derived_from_email": n_derived,
        "derived_survived": n_from_email,
        "agents_saved_by_derivation": n_derived,
        "survived": len(survivors), "email_basis": dict(basis),
        "smtp_probed": smtp_probed, "smtp_rescued": smtp_rescued,
        "catchall_built_from_site_format": catchall_built,
        "dropped_no_match": dropped_no_match,
        "dropped_name_or_gender": dropped_no_name,
        "dropped_business_as_surname": dropped_business_as_surname,
        "dropped_no_direct_email": dropped_no_email,
        "dropped_constructed_no_url_evidence": dropped_no_evidence_url,
        "dropped_dead_domain": dropped_dead_domain,
        "wa_fallback_kept": wa_fallback_kept,
        "parse_errors": parse_errors,
        "retired_unreachable": n_retired,
    }, indent=2) + "\n")
    if not survivors:
        print("ABORT: Stage 5.5 produced 0 leads with resolved contact + gender.")
        raise SystemExit(7)


def phase_rescue() -> None:
    """Stage 5.6 (opt-in, `linkedin-email-lookup` channel) — a lead that died at
    Stage 5.5 with a NAME but no email gets one more chance: some decision-makers
    list a personal email in LinkedIn's Contact Info panel even when their
    employer's site never publishes one anywhere. linkedin/scripts/
    lookup_contact_email.js already did the browser work; this phase applies the
    exact same direct-email gate phase_merge uses (is_direct_email,
    looks_like_business_name, domain_accepts_mail) so a rescued lead is held to
    the identical bar, then appends survivors to leads-with-contact.json.

    Added 2026-08-20 after 2026-08-20-eu-hotels: 80 of 99 qualified leads died
    for exactly one reason — "name found, only a generic mailbox available" —
    and the name/role had already been paid for by that point.
    """
    if not args.linkedin_results:
        print("Stage 5.6 rescue: no --linkedin-results given, nothing to do.")
        return
    results_path = Path(args.linkedin_results)
    if not results_path.exists():
        print(f"Stage 5.6 rescue: {results_path} does not exist, nothing to do.")
        return
    try:
        li_results = json.loads(results_path.read_text())
    except Exception:
        print(f"Stage 5.6 rescue: {results_path} unreadable, nothing to do.")
        return

    dropped_path = ROOT / "leads-dropped.json"
    dropped_by_id = {}
    if dropped_path.exists():
        for line in dropped_path.read_text().splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("drop_stage") == "no_match" and (d.get("found_first_name") or d.get("found_last_name")):
                dropped_by_id[d["lead_id"]] = d

    leads_by_id = {l["lead_id"]: l for l in load_leads()}

    existing = []
    if WITH_CONTACT.exists():
        for line in WITH_CONTACT.read_text().splitlines():
            if line.strip():
                existing.append(json.loads(line))
    used_addrs = {(l.get("contact_email") or "").strip().lower() for l in existing if l.get("contact_email")}

    rescued, dup, no_candidate, gate_failed = 0, 0, 0, 0
    for r in li_results:
        if not r.get("email_found"):
            continue
        lead_id = r.get("lead_id")
        drop = dropped_by_id.get(lead_id)
        lead = leads_by_id.get(lead_id)
        if not drop or not lead:
            no_candidate += 1
            continue
        email = (r.get("email") or "").strip().lower()
        if not is_direct_email(email):
            gate_failed += 1
            continue
        if not domain_accepts_mail(email.split("@", 1)[1]):
            gate_failed += 1
            continue
        first = drop.get("found_first_name", "")
        last = drop.get("found_last_name", "")
        title = drop.get("found_title", "")
        if title not in {"Mr.", "Mrs."}:
            title = ""
        surname_ok = bool(last) and title in {"Mr.", "Mrs."}
        if not (surname_ok or first):
            gate_failed += 1
            continue
        if last and looks_like_business_name(last, lead.get("name", "")):
            gate_failed += 1
            continue
        if email in used_addrs:
            dup += 1
            continue
        used_addrs.add(email)
        existing.append({
            **lead,
            "contact_first_name": first,
            "contact_last_name": last,
            "contact_title": title,
            "contact_role": drop.get("found_role", ""),
            "contact_source_url": drop.get("found_source_url", ""),
            "contact_email": email,
            "contact_email_basis": "linkedin_contact_info",
            "contact_email_source_url": r.get("url", ""),
            "contact_phone": "",
            "contact_phone_source_url": "",
            "contact_channel": "email",
            "contact_wa_fallback_reason": "",
            "contact_confidence": "medium",
        })
        rescued += 1

    existing.sort(key=lambda l: -l.get("score", 0))
    WITH_CONTACT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in existing) + ("\n" if existing else ""))
    print(f"Stage 5.6 rescue: {len(li_results)} LinkedIn lookup(s), "
          f"{rescued} rescued into leads-with-contact.json "
          f"({dup} duplicate address, {gate_failed} failed the direct-email gate, "
          f"{no_candidate} had no matching drop record)")


def _retire_unreachable(pairs: list[tuple[str, str]]) -> int:
    """Write PROVEN-unreachable domains to the permanent disqualified ledger.

    This replaces the qualified-pending backlog (removed 2026-08-05). A lead that
    qualifies but whose decision-maker publishes no direct email is not "waiting
    to be contacted" — it is unreachable under this pipeline's email contract, and
    every retry re-proved that at the cost of a full name-finder agent. Measured
    across all runs on disk: 3,760 of 4,095 enrichment drops were exactly this.

    Retiring the domain here means merge_candidates.py's existing disqualified
    net drops it at Stage 3 of every future run, so it is never sourced, fetched,
    extracted or enriched again.

    Same append-once, never-rewrite discipline as qualify_leads.py's ledger.
    """
    if not pairs:
        return 0
    ledger = Path("vault/lead-outreach/disqualified-log.txt")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    from datetime import date
    today = date.today().isoformat()
    seen = set()
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            part = line.split("|", 1)[0].strip().lower()
            if part:
                seen.add(part)
    added = 0
    with ledger.open("a") as f:
        for dom, reason in pairs:
            dom = dom.strip().lower()
            if not dom or dom in seen:
                continue
            seen.add(dom)
            f.write(f"{dom}|{today}|{ROOT.name}|{reason}\n")
            added += 1
    return added


# Guarded so the module can be IMPORTED (by tests/test_enrich_name_recovery.py,
# which exercises the prose parser and the direct-email gate) without executing
# a pipeline phase against the cwd. Invoking the script from the CLI is
# unchanged — that path always has __name__ == "__main__".
if __name__ == "__main__":
    if args.phase == "prep":
        phase_prep()
    elif args.phase == "merge":
        phase_merge()
    else:
        phase_rescue()
