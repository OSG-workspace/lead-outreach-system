#!/usr/bin/env python3
"""Free web-traffic / demand signals, derived from HTML the pipeline ALREADY fetched.

Why this module exists
----------------------
Stage 5.5 enrichment dispatches one sub-agent per lead and is capped at 250
(ENRICH_MAX_LEADS). Measured across 2,133 leads that entered that stage, 80%
die at `no_match`. Which leads get one of those scarce slots is therefore a
high-leverage decision, and until now it was made with no notion of how busy
the business actually is.

A site a lot of people open means more customers, which means the missed-call
pain our offer solves is real. This module scores that, using ONLY pages
already on disk under runs/<slug>/raw_html/ — no network call, no new
dependency, no paid API.

Determinism is a hard requirement: extract_leads.pick_best once had a
PYTHONHASHSEED bug that changed to_email for 11/279 leads between re-extracts.
Never iterate a set into output; sort first.

Input convention: every function takes ALREADY-LOWERCASED html (the caller,
extract_leads.py:296, computes html_lower = html.lower()), so all patterns
match lowercase keys — `reviewcount`, never `reviewCount`.
"""
from __future__ import annotations

import re

# A single scraped junk number must not dominate the ordering.
REVIEW_CLAMP = 50_000

# JSON-LD: "reviewcount": 1247   |   "ratingcount":"892"
_REVIEW_JSONLD_RE = re.compile(
    r'"(?:reviewcount|ratingcount)"\s*:\s*"?(\d{1,9})"?'
)
# Microdata, both attribute orders.
_REVIEW_MICRODATA_RE = re.compile(
    r'itemprop\s*=\s*["\'](?:reviewcount|ratingcount)["\'][^>]{0,120}?'
    r'content\s*=\s*["\'](\d{1,9})["\']'
)
_REVIEW_MICRODATA_REV_RE = re.compile(
    r'content\s*=\s*["\'](\d{1,9})["\'][^>]{0,120}?'
    r'itemprop\s*=\s*["\'](?:reviewcount|ratingcount)["\']'
)
# Visible text, multilingual. The number may carry , or . as a thousands
# separator; _to_int rejects anything that is really a decimal rating.
_REVIEW_WORDS = r"(?:reviews?|ratings?|opiniones|avis|recensioni|bewertungen)"
_REVIEW_TEXT_RE = re.compile(r"([\d][\d.,]{0,11})\s*" + _REVIEW_WORDS + r"\b")


def _to_int(raw: str) -> int:
    """Parse a review-count token, rejecting decimal ratings.

    '1,247' -> 1247    '1.247' -> 1247 (european thousands)    '4.8' -> 0
    A '.' or ',' is a thousands separator only when followed by exactly 3
    digits; otherwise the token is a decimal number and not a count.
    """
    token = raw.strip()
    if re.fullmatch(r"\d+", token):
        return int(token)
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", token):
        return int(re.sub(r"[.,]", "", token))
    return 0


def detect_review_count(html_lower: str) -> tuple[int, list[str]]:
    """Return (max review count found, sorted signal names)."""
    best = 0
    signals: set[str] = set()

    for m in _REVIEW_JSONLD_RE.finditer(html_lower):
        v = _to_int(m.group(1))
        if v > 0:
            signals.add("reviews_jsonld")
            best = max(best, v)

    for rx in (_REVIEW_MICRODATA_RE, _REVIEW_MICRODATA_REV_RE):
        for m in rx.finditer(html_lower):
            v = _to_int(m.group(1))
            if v > 0:
                signals.add("reviews_microdata")
                best = max(best, v)

    for m in _REVIEW_TEXT_RE.finditer(html_lower):
        v = _to_int(m.group(1))
        if v > 0:
            signals.add("reviews_text")
            best = max(best, v)

    return min(best, REVIEW_CLAMP), sorted(signals)
