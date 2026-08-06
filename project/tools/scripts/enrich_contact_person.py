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
import shutil
import subprocess
import sys
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

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge"])
p.add_argument("--run-dir", required=True)
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
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
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
    for f in ROOT.glob("enrich-batch-*.txt"):
        f.unlink()
    for f in ROOT.glob("enrich-out-*.json"):
        f.unlink()

    for f in ROOT.glob("derived-contacts.json"):
        f.unlink()

    raw_dir = ROOT / "raw_html"
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
        batch_path = ROOT / f"enrich-batch-{nnn}.txt"
        out_path = ROOT / f"enrich-out-{nnn}.json"
        target_roles_line = f"TargetRoles: {TARGET_ROLES}\n" if TARGET_ROLES else ""
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
            f"OutputFile: {out_path}\n"
            f"\n"
            f"SitePages (already-scraped text — READ THIS BEFORE any web fetch):\n"
            f"{pages_block}\n"
        )
    if derived:
        DERIVED.write_text(json.dumps(derived, ensure_ascii=False, indent=2) + "\n")
    print(f"Prepped {n_batches} enrich batches in {ROOT}")
    if TARGET_ROLES:
        print(f"  TargetRoles override active: {TARGET_ROLES}")
    if derive_on:
        from collections import Counter
        how = Counter(d["name_basis"] for d in derived)
        print(f"  name-from-email: {len(derived)}/{len(leads)} decision-makers parsed "
              f"straight off a site address — {len(derived)} agents NOT dispatched "
              f"({dict(how) if how else 'none matched'}).")
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


def phase_merge() -> None:
    """Read enrich-out-*.json, join to leads-extracted.json, drop leads with
    no name+gender, write leads-with-contact.json."""
    leads = load_leads()
    by_id = {l["lead_id"]: l for l in leads}

    # --- fan-out completion guard (kill-on-fallback) ---------------------
    # 2026-06-25-eu-hotels prepped 94 batches, only 24 agents ever wrote an
    # output, and the run shipped 15 sends as if nothing was wrong. A partial
    # fan-out is a degraded run: halt instead of silently sending a fraction.
    n_batches = len(list(ROOT.glob("enrich-batch-*.txt")))
    n_outs = len(list(ROOT.glob("enrich-out-*.json")))
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

    out_files = sorted(ROOT.glob("enrich-out-*.json"))
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

    survivors = []
    dropped = []  # lightweight audit trail — see note above WITH_CONTACT.write_text below
    dropped_no_name = 0
    dropped_no_match = 0
    dropped_no_email = 0
    dropped_business_as_surname = 0
    dropped_no_evidence_url = 0
    dropped_dead_domain = 0
    wa_fallback_kept = 0

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
            "found_role": (r.get("role") or "").strip(),
            "found_source_url": (r.get("source_url") or "").strip(),
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

    for lead in leads:
        result = enriched.get(lead["lead_id"])
        if not result or not result.get("found"):
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
        if last and looks_like_business_name(last, lead.get("name", "")) and result.get("confidence") != "high":
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
    n_retired = _retire_unreachable(unreachable)
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


if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
