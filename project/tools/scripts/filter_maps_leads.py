#!/usr/bin/env python3
"""
Filter and trim raw output from gosom/google-maps-scraper.

Reads raw JSONL where each line is a full Maps scraper record (~1-3KB), applies
hard filters that don't need Claude's judgment (closed, low rating, chain
detection, no contact channel, outside geo), and writes a slim canonical-schema
JSONL (~300 bytes/lead) that the orchestrator hands to scoring.

This keeps Claude out of bulk row-by-row filtering — pure mechanical rules
belong in Python, not in context tokens.

Usage:
    python3 filter_maps_leads.py \\
        --input runs/<slug>/maps-raw.json \\
        --output runs/<slug>/leads-raw.json \\
        [--city "Beirut"] \\
        [--min-rating 3.0] \\
        [--require-website]
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

CHAIN_KEYWORDS = {
    "starbucks", "mcdonald", "subway", "dunkin", "kfc",
    "pizza hut", "domino", "burger king", "costa coffee",
}


def is_chain(record: dict) -> bool:
    """Heuristic chain detection. Cheap signals only."""
    name = (record.get("title") or "").lower()
    if any(k in name for k in CHAIN_KEYWORDS):
        return True
    related = record.get("related_places") or []
    if isinstance(related, list) and len(related) >= 4:
        return True
    return False


def best_email(emails) -> Optional[str]:
    """Pick the highest-quality email from the scraper's email list."""
    if not emails:
        return None
    if isinstance(emails, str):
        emails = [emails]
    if not isinstance(emails, list):
        return None

    skip = {"noreply", "no-reply", "donotreply", "mailer-daemon",
            "postmaster", "webmaster"}
    low = {"info", "admin", "support", "help", "office",
           "contact", "hello", "hi", "team"}

    valid = []
    for e in emails:
        if not e or "@" not in e:
            continue
        local = e.split("@")[0].lower()
        if local in skip:
            continue
        valid.append(e)

    def rank(e):
        local = e.split("@")[0].lower()
        if local in low:
            return 2
        if "." in local or "_" in local or "-" in local:
            return 0
        if len(local) > 4:
            return 1
        return 2

    valid.sort(key=rank)
    return valid[0] if valid else None


def to_canonical(record: dict) -> dict:
    """Map scraper output to the slim canonical lead schema."""
    place_id = record.get("place_id") or ""
    slug_base = (record.get("title") or place_id or "unknown").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug_base).strip("-")[:60]

    emails = record.get("emails") or []
    chosen_email = best_email(emails)

    footer = record.get("footer") or ""
    year_match = re.search(r"©\s*(20\d\d)", footer)
    last_year = int(year_match.group(1)) if year_match else None

    addr = record.get("complete_address")
    if isinstance(addr, dict):
        addr = addr.get("complete") or addr.get("address") or addr.get("borough") or ""
    elif not isinstance(addr, str):
        addr = ""
    if not addr:
        addr = record.get("address") or ""

    return {
        "id": f"maps-{slug or place_id[:12]}",
        "name": record.get("title"),
        "source": "google_maps",
        "url": record.get("web_site") or record.get("website") or record.get("link"),
        "email": chosen_email,
        "phone": record.get("phone"),
        "location": addr,
        "raw": {
            "place_id": place_id,
            "rating": record.get("review_rating"),
            "review_count": record.get("review_count"),
            "category": record.get("category"),
            "is_chain": is_chain(record),
            "all_emails": emails if isinstance(emails, list) else [emails] if emails else [],
            "footer_year": last_year,
            "google_maps_url": record.get("link"),
        },
    }


def passes_filters(canonical: dict, args) -> tuple[bool, str]:
    """Return (keep, reason)."""
    name = (canonical.get("name") or "").lower()
    if not name:
        return False, "no_name"
    if "permanently closed" in name or "temporarily closed" in name:
        return False, "closed"

    raw = canonical.get("raw") or {}
    rating = raw.get("rating")
    review_count = raw.get("review_count") or 0
    if rating is not None and rating < args.min_rating and review_count > 5:
        return False, f"low_rating_{rating}"

    if raw.get("is_chain"):
        return False, "chain"

    if args.require_website and not canonical.get("url"):
        return False, "no_website"

    if args.city:
        loc = (canonical.get("location") or "").lower()
        if args.city.lower() not in loc:
            return False, "outside_geo"

    if not canonical.get("email") and not canonical.get("url") and not canonical.get("phone"):
        return False, "no_contact"

    return True, ""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--city", default=None,
                   help="If set, drop leads whose address doesn't contain this string")
    p.add_argument("--min-rating", type=float, default=3.0)
    p.add_argument("--require-website", action="store_true")
    args = p.parse_args()

    in_path = Path(args.input)
    out_path = Path(args.output)
    if not in_path.exists():
        print(f"error: {in_path} does not exist", file=sys.stderr)
        sys.exit(1)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    counts = {"input": 0, "kept": 0}
    drops: dict[str, int] = {}

    with in_path.open() as f_in, out_path.open("w") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            counts["input"] += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                drops["bad_json"] = drops.get("bad_json", 0) + 1
                continue

            canonical = to_canonical(record)
            keep, reason = passes_filters(canonical, args)
            if not keep:
                drops[reason] = drops.get(reason, 0) + 1
                continue

            f_out.write(json.dumps(canonical, ensure_ascii=False) + "\n")
            counts["kept"] += 1

    summary = {
        "input": counts["input"],
        "kept": counts["kept"],
        "dropped": dict(sorted(drops.items(), key=lambda x: -x[1])),
        "output_file": str(out_path),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
