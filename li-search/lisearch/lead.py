"""The lead record, its canonical identity, and how two sources merge into one.

IDENTITY IS THE LINKEDIN ACCOUNT NAME
Everything keys off the /in/ slug — the "linkedin account name" the operator
asked for as the output. It is the only identifier every provider agrees on, it
survives the person changing job, and it is what outreach actually needs.
Matching on person-name would merge two different Mohammed Al-Rashids; matching
on email would drop everyone whose email we never learn.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from .compliance import is_eea

# /in/<slug> with the locale prefixes LinkedIn hands out on regional domains.
SLUG_RE = re.compile(r"/in/([^/?#]+)", re.I)


def canonical_account(url_or_slug: str) -> Optional[str]:
    """-> the bare account name, or None if this is not a real profile URL.

    Rejects company pages, school pages, and post URLs. A guessed slug from a
    person's name is NOT a profile; the research brief is explicit that a wrong
    profile burns a real person, so anything that does not look like a slug we
    were actually shown is refused upstream in the adapters.
    """
    s = (url_or_slug or "").strip()
    if not s:
        return None
    if "/" not in s and " " not in s:            # already a bare slug
        slug = s
    else:
        if "linkedin.com" not in s.lower():
            return None
        if re.search(r"linkedin\.com/(company|school|showcase|posts|pulse|jobs)/", s, re.I):
            return None
        m = SLUG_RE.search(urllib.parse.urlsplit(s).path)
        if not m:
            return None
        slug = m.group(1)
    slug = urllib.parse.unquote(slug).strip().strip("/").lower()
    slug = slug.split("?")[0].split("#")[0]
    if not slug or len(slug) < 3 or slug in ("me", "unavailable", "in"):
        return None
    if not re.match(r"^[a-z0-9\-%._À-ɏ؀-ۿ]+$", slug):
        return None
    return slug


def profile_url(account: str) -> str:
    return "https://www.linkedin.com/in/%s" % account


# ae.linkedin.com/in/... -> "AE". LinkedIn picks the subdomain from the member's
# own stated location, which makes it the cheapest reliable country signal a web
# index gives us — it feeds the geo score and the EEA flag without opening the
# profile. uk.linkedin.com is the one non-ISO subdomain in this operator's map.
_HOST_CC_RE = re.compile(r"^([a-z]{2})\.linkedin\.com$", re.I)
_SUBDOMAIN_CC = {"uk": "GB"}


def country_from_url(url: str) -> str:
    s = (url or "").strip()
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    host = (urllib.parse.urlsplit(s).hostname or "").lower()
    m = _HOST_CC_RE.match(host)
    if not m:
        return ""
    return _SUBDOMAIN_CC.get(m.group(1), m.group(1).upper())


# Bidi/format control characters. DuckDuckGo and several providers wrap
# Arabic-adjacent names in RLM/LRM marks; left in place they corrupt every
# downstream CSV, and an Arabic name is the common case in this operator's
# market, not an edge case.
_CTRL_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean_text(s: str) -> str:
    return _CTRL_RE.sub("", s or "").strip()


def make_lead(
    source: str,
    account: str,
    full_name: str = "",
    title: str = "",
    company: str = "",
    location: str = "",
    country: str = "",
    email: str = "",
    summary: str = "",
    collected_at: Optional[str] = None,
    freshness_days: Optional[int] = None,
    raw_query: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = collected_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "linkedin_account": account,
        "linkedin_url": profile_url(account),
        "full_name": clean_text(full_name),
        "title": clean_text(title),
        "company": clean_text(company),
        "location": clean_text(location),
        "country": clean_text(country).upper(),
        "email": clean_text(email).lower(),
        "summary": clean_text(summary)[:600],
        # --- provenance: mandated by the brief, carried on every record ---
        "sources": [source],
        "collected_at": now,
        "freshness_days": freshness_days,
        "freshness": freshness_flag(freshness_days),
        "eu_flag": is_eea(country),
        "raw_query": raw_query,
        "extra": extra or {},
    }


def freshness_flag(days: Optional[int]) -> str:
    """Coarse buckets, because the honest resolution is coarse. Several batch
    providers serve records that are 3-4 months old at access time, so 'live'
    must mean request-time and nothing else."""
    if days is None:
        return "unknown"
    if days <= 1:
        return "live"
    if days <= 30:
        return "fresh"
    if days <= 120:
        return "aging"
    return "stale"


def merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Two providers found the same account. Keep the richer field from each and
    union the provenance, so the record shows it was independently corroborated."""
    out = dict(a)
    for k in ("full_name", "title", "company", "location", "country", "email", "summary"):
        if not out.get(k) and b.get(k):
            out[k] = b[k]
        elif b.get(k) and len(str(b[k])) > len(str(out.get(k, ""))) and k in ("title", "company", "summary"):
            out[k] = b[k]
    out["sources"] = sorted(set(out.get("sources", [])) | set(b.get("sources", [])))
    # The freshest claim wins; unknown never beats a known number.
    fa, fb = out.get("freshness_days"), b.get("freshness_days")
    if fb is not None and (fa is None or fb < fa):
        out["freshness_days"] = fb
        out["collected_at"] = b.get("collected_at", out["collected_at"])
    out["freshness"] = freshness_flag(out.get("freshness_days"))
    out["eu_flag"] = out["eu_flag"] or b.get("eu_flag", False)
    return out


def dedupe(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_account: Dict[str, Dict[str, Any]] = {}
    for l in leads:
        acct = l.get("linkedin_account")
        if not acct:
            continue
        if acct in by_account:
            by_account[acct] = merge(by_account[acct], l)
        else:
            by_account[acct] = l
    return list(by_account.values())
