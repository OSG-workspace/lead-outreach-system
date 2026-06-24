#!/usr/bin/env python3
"""
Email enrichment via MailScout pattern generation + SMTP validation.

For leads without a high-confidence person-class email, uses MailScout to:
  - Generate email pattern candidates (firstname@, firstname.lastname@, etc.)
  - SMTP-validate each candidate against the company domain
  - Return ranked candidates by pattern popularity

This is a free, local alternative to expensive APIs. Works best when company
name/domain is available (MailScout generates patterns, no web scraping needed).

Faster than crawl4ai (no browser startup), complementary to crawl4ai (pattern
generation vs regex extraction). Can be run in parallel or as a post-enrichment step.

Usage:
    ./tools/run.sh tools/scripts/enrich_emails_mailscout.py \\
        --input  runs/<slug>/leads-source-fit.json \\
        --output runs/<slug>/leads-enriched-mailscout.json \\
        [--smtp-threads 5] [--smtp-timeout 2]

Requirements:
    - Port 25 (SMTP) must be open outbound. Cloud services often block this.
    - dnspython, unidecode (installed via: pip install mailscout)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from mailscout import Scout
except ImportError:
    print("error: mailscout not installed. Run: pip install mailscout", file=sys.stderr)
    sys.exit(1)


def domain_of(url: str) -> str:
    """Extract domain from URL (e.g., 'example.com' from 'https://example.com')."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def extract_name_tokens(name: str) -> list[str]:
    """Extract name tokens from company/person name.

    Returns list of name parts that can be used for email pattern generation.
    Examples:
      'Care & Beauty Medical Complex' → ['care', 'beauty', 'medical', 'complex']
      'John Smith' → ['john', 'smith']
      'Al Salmaniyah' → ['al', 'salmaniyah']
    """
    if not name:
        return []
    # Remove special chars, split, filter short tokens
    tokens = []
    for token in name.replace("&", " ").replace("-", " ").split():
        clean = "".join(c for c in token.lower() if c.isalnum())
        if clean and len(clean) > 2:  # Skip 'al', 'of', etc.
            if clean not in tokens:  # Deduplicate
                tokens.append(clean)
    return tokens


def classify_email(email: str) -> str:
    """Classify email as person/role/junk for filtering."""
    e = email.strip().lower() if email else ""
    if not e or "@" not in e or e.count("@") != 1:
        return "junk"

    local = e.split("@", 1)[0]
    if len(local) < 3:
        return "junk"

    # Role inbox pattern.
    role_patterns = {
        "info", "contact", "hello", "hi", "sales", "admin", "support",
        "noreply", "marketing", "hr", "jobs", "careers", "booking",
        "appointment", "reception", "office", "team", "enquiry",
        "inquiry", "mail", "email", "service", "help", "feedback",
        "webmaster", "finance", "accounts", "billing", "news",
        "newsletter", "abuse", "postmaster", "general", "operations",
        "ops", "frontdesk", "press", "media", "pr",
    }
    if local in role_patterns:
        return "role"

    # Freemail (personal).
    freemail_domains = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "protonmail.com", "live.com", "aol.com",
    }
    dom = e.split("@", 1)[1]
    if dom in freemail_domains:
        return "personal"

    # Company domain.
    return "person"


def needs_enrichment(lead: dict) -> bool:
    """Skip leads that already have a good person-class email."""
    email = (lead.get("email") or "").strip().lower()
    if email and classify_email(email) == "person":
        return False
    return True


def enrich_with_mailscout(leads: list[dict], num_threads: int = 5,
                          smtp_timeout: int = 2) -> list[dict]:
    """Enrich leads using MailScout pattern generation + SMTP validation."""
    work_leads = [l for l in leads if needs_enrichment(l)]
    if not work_leads:
        return leads

    # Shared Scout instance for SMTP validation.
    scout = Scout(
        num_threads=num_threads,
        smtp_timeout=smtp_timeout,
        check_variants=True,        # Generate first.last, firstlast, etc.
        check_prefixes=False,       # Skip info@, contact@, etc. (not person emails)
        check_catchall=True,        # Detect catch-all domains
    )

    enriched_count = 0
    start_time = time.time()

    for idx, lead in enumerate(work_leads):
        if (idx + 1) % max(10, len(work_leads) // 10) == 0:
            print(f"  mailscout: {idx + 1}/{len(work_leads)} processed",
                  file=sys.stderr, flush=True)

        # Extract domain and name tokens.
        url = lead.get("url") or lead.get("website") or ""
        domain = domain_of(url)
        if not domain:
            continue

        company_name = lead.get("name", "")
        name_tokens = extract_name_tokens(company_name)
        if not name_tokens:
            continue

        # Call MailScout to generate + validate email patterns.
        try:
            candidates = scout.find_valid_emails(domain, name_tokens)
        except Exception as e:
            # SMTP errors (timeouts, etc.) are common and non-fatal.
            # MailScout still returns pattern-based candidates.
            candidates = []

        if not candidates:
            continue

        # Pick best email: prefer person-class, rank by pattern popularity
        # (MailScout returns candidates ranked by pattern popularity already).
        best_email = None
        best_class = None
        for candidate in candidates:
            klass = classify_email(candidate)
            if klass == "person":
                best_email = candidate
                best_class = klass
                break  # First person-class email is best (highest pattern ranking)

        # Fallback to first personal if no person-class found.
        if not best_email:
            for candidate in candidates:
                klass = classify_email(candidate)
                if klass == "personal":
                    best_email = candidate
                    best_class = klass
                    break

        if best_email:
            lead["email"] = best_email
            lead["email_class"] = best_class
            lead["email_source"] = "mailscout_pattern"
            lead["email_confidence"] = 75  # Pattern-based, not website-verified
            if best_class == "person":
                lead["dm_email"] = best_email
            enriched_count += 1

            # Store top candidates for reference.
            if "raw" not in lead:
                lead["raw"] = {}
            lead["raw"]["mailscout_candidates"] = candidates[:5]

    elapsed = time.time() - start_time
    print(f"  mailscout: {enriched_count}/{len(work_leads)} "
          f"({100*enriched_count//len(work_leads) if work_leads else 0}%) "
          f"in {elapsed:.1f}s", file=sys.stderr, flush=True)

    return leads


def main() -> None:
    p = argparse.ArgumentParser(description="Email enrichment via MailScout pattern generation.")
    p.add_argument("--input", required=True, help="Input leads JSONL")
    p.add_argument("--output", required=True, help="Output leads JSONL")
    p.add_argument("--report", default=None, help="Report JSON file")
    p.add_argument("--smtp-threads", type=int, default=5,
                   help="Parallel SMTP validation threads")
    p.add_argument("--smtp-timeout", type=int, default=2,
                   help="SMTP timeout in seconds (shorter = faster, longer = more reliable)")
    p.add_argument("--limit", type=int, default=0, help="Limit to N leads for testing")
    args = p.parse_args()

    # Read input leads.
    input_path = Path(args.input)
    output_path = Path(args.output)
    leads: list[dict] = []
    for line in input_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            leads.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if args.limit > 0:
        leads = leads[:args.limit]

    start_time = time.time()

    # Enrich with MailScout.
    enriched_leads = enrich_with_mailscout(
        leads,
        num_threads=args.smtp_threads,
        smtp_timeout=args.smtp_timeout,
    )

    elapsed = time.time() - start_time

    # Write output.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in enriched_leads))

    # Count results.
    enriched_count = sum(1 for l in enriched_leads if l.get("email_source") == "mailscout_pattern")
    needing = sum(1 for l in leads if needs_enrichment(l))

    report = {
        "input": len(leads),
        "needing_enrichment": needing,
        "enriched": enriched_count,
        "elapsed_s": round(elapsed, 2),
        "output": str(output_path),
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
