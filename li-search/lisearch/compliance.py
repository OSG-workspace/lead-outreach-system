"""The line this tool will not cross, enforced in code rather than in a README.

WHY THIS IS A MODULE AND NOT A PARAGRAPH
The 2026 provider research that commissioned this tool is unambiguous about the
failure mode: the cheapest way to get LinkedIn data is to drive the operator's
own logged-in session, and that is exactly what got Proxycurl permanently
enjoined (LinkedIn v. Nubela, N.D. Cal. 3:25-cv-00828, shut down 2025-07-04) and
what gets individual accounts restricted. A rule written only in prose gets
quietly broken the first time recall looks disappointing. So:

  1. No request may go to linkedin.com. Enforced on every outbound call.
  2. No adapter may hold a session cookie (li_at / JSESSIONID). Enforced on
     adapter registration.
  3. Every emitted record carries provenance (source, collected_at, freshness)
     so a stale or thin record can be down-weighted rather than trusted.
  4. A suppression list is applied at output, so an Article 14 / erasure
     request is honourable in one place.

WHAT THIS MODULE DOES NOT DO
It is not legal advice and it does not make the operator compliant. GDPR Art.
3(2) reaches a Lebanon-based controller processing EU-resident data; the
lawful-basis assessment, the Art. 14 notice and the Art. 27 representative are
paperwork this code cannot produce. `eu_flag` exists so you can see which
records carry that exposure before you act on them.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Iterable, List, Set

ROOT = Path(__file__).resolve().parent.parent

# Hosts this tool refuses to contact, no matter which adapter asks.
FORBIDDEN_HOST_RE = re.compile(r"(^|\.)linkedin\.com$", re.I)

# Cookie names that mean "you are driving somebody's authenticated session".
SESSION_COOKIE_NAMES = ("li_at", "jsessionid", "li_a", "liap", "bcookie", "bscookie")

# EEA + UK. Not a legal boundary, a "this record carries GDPR exposure" flag.
EEA = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IS", "IE", "IT", "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL",
    "PT", "RO", "SK", "SI", "ES", "SE", "GB", "UK",
}


class ComplianceError(Exception):
    """Raised instead of doing the forbidden thing. Never caught internally."""


def assert_allowed_host(url: str) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if FORBIDDEN_HOST_RE.search(host):
        raise ComplianceError(
            "refusing to contact %s. This tool sources people from licensed and "
            "indexed people-data providers; it does not scrape linkedin.com and "
            "does not drive a logged-in session. See lisearch/compliance.py." % host
        )


def assert_no_session_auth(adapter_name: str, headers: dict) -> None:
    blob = " ".join(str(k) + "=" + str(v) for k, v in (headers or {}).items()).lower()
    for name in SESSION_COOKIE_NAMES:
        if re.search(r"\b%s\b" % re.escape(name), blob):
            raise ComplianceError(
                "adapter %r is carrying a LinkedIn session cookie (%s). "
                "Authenticated-session access is a User Agreement 8.2 breach and "
                "puts the operator's own account at risk; it is refused here."
                % (adapter_name, name)
            )


def is_eea(country_code: str) -> bool:
    return (country_code or "").strip().upper() in EEA


def load_suppression() -> Set[str]:
    """Slugs and emails that must never be emitted again.

    One file, one purpose. `li-search suppress <slug|url>` appends to it, so an
    erasure request is a single command rather than a hunt through run outputs.
    """
    f = ROOT / "suppression.txt"
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.add(line)
    return out


def apply_suppression(leads: Iterable[dict]) -> List[dict]:
    sup = load_suppression()
    if not sup:
        return list(leads)
    kept = []
    for l in leads:
        keys = {
            (l.get("linkedin_account") or "").lower(),
            (l.get("linkedin_url") or "").lower(),
            (l.get("email") or "").lower(),
        }
        if keys & sup:
            continue
        kept.append(l)
    return kept
