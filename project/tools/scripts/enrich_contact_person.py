#!/usr/bin/env python3
"""Stage 5.5 — contact-person enrichment.

Two phases, both run by the /fire orchestrator:

  prep   reads leads-extracted.json, writes one enrich-batch-NNN.txt per lead.
         Orchestrator then dispatches one Haiku `name-finder` sub-agent per
         batch file (max parallelism, single message).

  merge  reads all enrich-out-*.json, joins to leads-extracted.json, drops
         leads missing any of:
           - first + last name
           - title (Mr./Mrs.)
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

p = argparse.ArgumentParser()
p.add_argument("--phase", required=True, choices=["prep", "merge"])
p.add_argument("--run-dir", required=True)
p.add_argument("--enrich-phone", action="store_true",
               help="resolve a mobile/WhatsApp number per lead (name-finder step 3). "
                    "OFF by default — only WhatsApp runs need it; an email-only run "
                    "never uses a phone, and the phone ladder is the biggest source "
                    "of wasted searches.")
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
# Read the qualified+capped set when Stage 5.3 produced one, so we only enrich
# the top-N fit leads instead of every extracted lead. Falls back to the raw
# extract for older runs that predate the qualify gate.
QUALIFIED = ROOT / "leads-qualified.json"
EXTRACTED = QUALIFIED if QUALIFIED.exists() else (ROOT / "leads-extracted.json")
WITH_CONTACT = ROOT / "leads-with-contact.json"

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

    raw_dir = ROOT / "raw_html"
    with_personal = 0
    with_pages = 0
    for i, lead in enumerate(leads, start=1):
        nnn = f"{i:03d}"
        batch_path = ROOT / f"enrich-batch-{nnn}.txt"
        out_path = ROOT / f"enrich-out-{nnn}.json"
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
            f"OutputFile: {out_path}\n"
            f"\n"
            f"SitePages (already-scraped text — READ THIS BEFORE any web fetch):\n"
            f"{pages_block}\n"
        )
    print(f"Prepped {len(leads)} enrich batches in {ROOT}")
    print(f"  {with_personal}/{len(leads)} have >=1 person-format email harvested from the site "
          f"(verbatim candidates / format examples for the name-finder).")
    print(f"  {with_pages}/{len(leads)} have pre-scraped about/team/contact text injected "
          f"(agent reads these instead of re-fetching the web).")
    print(f"Dispatch this many parallel name-finder Haiku agents from the orchestrator.")


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

    enriched = {}
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
    }
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
    dropped_no_name = 0
    dropped_no_match = 0
    dropped_no_email = 0
    dropped_business_as_surname = 0
    dropped_no_evidence_url = 0
    dropped_dead_domain = 0

    for lead in leads:
        result = enriched.get(lead["lead_id"])
        if not result or not result.get("found"):
            dropped_no_match += 1
            continue
        first = (result.get("first_name") or "").strip()
        last = (result.get("last_name") or "").strip()
        title = (result.get("title") or "").strip()
        if not first or not last or title not in {"Mr.", "Mrs."}:
            dropped_no_name += 1
            continue
        if looks_like_business_name(last, lead.get("name", "")) and result.get("confidence") != "high":
            dropped_business_as_surname += 1
            continue
        email = (result.get("email") or "").strip().lower()
        if not is_direct_email(email):
            dropped_no_email += 1
            continue
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
                else:
                    dropped_no_evidence_url += 1
                    continue
            # The spec caps reconstructed addresses at medium confidence.
            if (result.get("confidence") or "").strip() == "high":
                result["confidence"] = "medium"
        # DNS gate: the recipient domain must actually accept mail (MX or A).
        if not domain_accepts_mail(email.split("@", 1)[1]):
            dropped_dead_domain += 1
            continue
        phone = (result.get("phone") or "").strip()
        # Strip everything but digits, then re-prefix `+` for storage. Empty stays empty.
        digits = "".join(ch for ch in phone if ch.isdigit())
        phone_stored = f"+{digits}" if digits else ""
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
                "contact_confidence": result.get("confidence", "low")}
        survivors.append(lead)

    survivors.sort(key=lambda l: -l["score"])
    WITH_CONTACT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in survivors) + "\n")

    from collections import Counter
    basis = Counter(s.get("contact_email_basis") or "unknown" for s in survivors)
    verbatim = basis.get("verbatim", 0)
    print(f"Merged contact-person enrichment for {len(leads)} leads:")
    print(f"  survived (name + Mr./Mrs. + direct email): {len(survivors)}")
    if survivors:
        print(f"  email basis: {dict(basis)} "
              f"({verbatim}/{len(survivors)} verbatim, rest inferred — watch Brevo bounces)")
    print(f"  dropped no-match:                {dropped_no_match}")
    print(f"  dropped name-or-gender-missing:  {dropped_no_name}")
    print(f"  dropped business-name-as-surname: {dropped_business_as_surname}")
    print(f"  dropped no-direct-email:         {dropped_no_email}")
    print(f"  dropped constructed-no-URL-evidence: {dropped_no_evidence_url}")
    print(f"  dropped dead-domain (no MX/A):   {dropped_dead_domain}")
    print(f"  enrich-out parse errors:         {parse_errors}")
    print(f"Wrote {WITH_CONTACT}")
    # Persist the gate counters — post-run cleanup deletes the per-agent
    # enrich-out files, so this summary is the only surviving diagnostic of
    # WHERE leads died in enrichment.
    (ROOT / "enrich-summary.json").write_text(json.dumps({
        "batches": n_batches, "agent_outputs": n_outs,
        "survived": len(survivors), "email_basis": dict(basis),
        "dropped_no_match": dropped_no_match,
        "dropped_name_or_gender": dropped_no_name,
        "dropped_business_as_surname": dropped_business_as_surname,
        "dropped_no_direct_email": dropped_no_email,
        "dropped_constructed_no_url_evidence": dropped_no_evidence_url,
        "dropped_dead_domain": dropped_dead_domain,
        "parse_errors": parse_errors,
    }, indent=2) + "\n")
    if not survivors:
        print("ABORT: Stage 5.5 produced 0 leads with resolved contact + gender.")
        raise SystemExit(7)


if args.phase == "prep":
    phase_prep()
else:
    phase_merge()
