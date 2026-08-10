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
# Microdata where the count is the element's TEXT CONTENT rather than a
# `content` attribute, e.g. <span itemprop="reviewcount">1247</span> or
# <span itemprop="ratingcount">1,247</span> reviews. Same reviews_microdata
# signal, same _to_int parsing.
#
# Void elements (<meta>, <link>, <img>, <br>, <hr>, <input>) have no closing
# tag, so the digits immediately after their ">" are unrelated trailing prose
# ("<meta itemprop=\"reviewcount\" content=\"0\">1,204 people follow this
# page"), not a text-content value — a void element never legitimately
# carries Schema.org text content, so it is excluded here and left to
# _REVIEW_MICRODATA_RE / _REVIEW_MICRODATA_REV_RE (the attribute-form
# regexes), which are the correct and only handler for those.
_VOID_ELEMENTS = frozenset({"meta", "link", "img", "br", "hr", "input"})
# Tag name uses [a-z0-9-]+, not \w+: \w+ stops at a hyphen, which would
# truncate a custom element like <input-group> down to "input" and wrongly
# match it against _VOID_ELEMENTS. Input is already lowercased by the caller,
# so no A-Z needed.
_REVIEW_MICRODATA_TEXT_RE = re.compile(
    r'<([a-z0-9-]+)\b[^>]*itemprop\s*=\s*["\'](?:reviewcount|ratingcount)["\'][^>]*>'
    r'\s*(\d[\d.,]{0,11})'
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

    for m in _REVIEW_MICRODATA_TEXT_RE.finditer(html_lower):
        tag, digits = m.group(1), m.group(2)
        if tag in _VOID_ELEMENTS:
            continue
        v = _to_int(digits)
        if v > 0:
            signals.add("reviews_microdata")
            best = max(best, v)

    for m in _REVIEW_TEXT_RE.finditer(html_lower):
        v = _to_int(m.group(1))
        if v > 0:
            signals.add("reviews_text")
            best = max(best, v)

    return min(best, REVIEW_CLAMP), sorted(signals)


# Traffic-DRIVING stack. Presence means the business is actively spending to
# bring people to the site, and has enough volume to justify measuring it.
# Patterns are matched against lowercased html, so ids like GTM-ABC read gtm-abc.
_TRACKERS: dict[str, tuple[str, ...]] = {
    "ga4": (r"gtag/js\?id=g-", r"googletagmanager\.com/gtag"),
    "gtm": (r"gtm-[a-z0-9]{4,}", r"googletagmanager\.com/gtm\.js"),
    "meta_pixel": (r"fbq\s*\(\s*[\"']init[\"']", r"connect\.facebook\.net/[^\"']*fbevents\.js"),
    "google_ads": (r"googleadservices\.com", r"[\"']aw-\d{6,}"),
    "hotjar": (r"static\.hotjar\.com", r"hjid\s*[:=]"),
    "clarity": (r"clarity\.ms/tag",),
    "segment": (r"cdn\.segment\.com", r"analytics\.load\s*\("),
    "mixpanel": (r"cdn\.mxpnl\.com", r"mixpanel\.init\s*\("),
    "tiktok": (r"analytics\.tiktok\.com",),
    "linkedin_insight": (r"snap\.licdn\.com", r"_linkedin_partner_id"),
}
_TRACKER_RE = {
    vendor: re.compile("|".join(pats)) for vendor, pats in _TRACKERS.items()
}


def detect_tracker_depth(html_lower: str) -> tuple[int, list[str]]:
    """Return (distinct vendor count, sorted signal names).

    Counting is per DISTINCT vendor: a GTM snippet repeated on four pages of
    the same concatenated document is one vendor, not four.
    """
    signals = sorted(
        f"tracker_{vendor}"
        for vendor, rx in _TRACKER_RE.items()
        if rx.search(html_lower)
    )
    return len(signals), signals


# Operational load: investments a business only makes when inbound volume
# forces it to.
_CHAT_RE = re.compile(
    r"widget\.intercom\.io|embed\.tawk\.to|client\.crisp\.chat|js\.driftt\.com"
    r"|static\.zdassets\.com|cdn\.livechatinc\.com|code\.tidio\.co"
)
_HREFLANG_RE = re.compile(r'hreflang\s*=\s*["\']([a-z]{2})(?:-[a-z0-9]{2,8})*["\']')

_OP_CHAT_POINTS = 10
_OP_MULTILANG_POINTS = 8
_OP_PAGES_POINTS = 6
_OP_BRANCH_POINTS = 6
# Current weights sum to exactly 30 (10+8+6+6), so min() is unreachable today.
# Kept as a defensive guard against future weight changes.
_OP_MAX = 30

_MANY_PAGES_MIN = 6
_MULTI_BRANCH_MIN = 5


def detect_operational_load(
    html_lower: str,
    pages_scanned: list[str],
    branches_estimate: int,
) -> tuple[int, list[str]]:
    """Return (points 0-30, sorted signal names)."""
    points = 0
    signals: list[str] = []

    if _CHAT_RE.search(html_lower):
        points += _OP_CHAT_POINTS
        signals.append("chat_widget")

    # Region variants of one language (en-us, en-gb) are NOT two languages.
    languages = {m.group(1) for m in _HREFLANG_RE.finditer(html_lower)}
    if len(languages) >= 2:
        points += _OP_MULTILANG_POINTS
        signals.append("multilang")

    if len(pages_scanned) >= _MANY_PAGES_MIN:
        points += _OP_PAGES_POINTS
        signals.append("many_pages")

    if branches_estimate >= _MULTI_BRANCH_MIN:
        points += _OP_BRANCH_POINTS
        signals.append("multi_branch")

    return min(points, _OP_MAX), sorted(signals)


# Below this many characters of concatenated page text we cannot judge the
# business at all. That state is `unknown`, which is NEVER dropped — a site we
# failed to crawl is our fetch failing, not a quiet business. Lifetime
# fetch->extract survival is 50.7% (16,572/32,681), so this case is common.
MIN_HTML_FOR_JUDGMENT = 2_000

# Review-count -> points. Buckets, not a curve: the raw number is noisy
# (different platforms, different aggregation windows) but its ORDER OF
# MAGNITUDE is meaningful.
_REVIEW_BUCKETS: tuple[tuple[int, int], ...] = (
    (500, 40),
    (100, 30),
    (25, 20),
    (1, 10),
)
_TRACKER_POINTS_EACH = 8
_TRACKER_MAX = 30

# PROVISIONAL thresholds — Task 5 recalibrates these against the 5,368
# already-fetched HTML files on disk and records the measurement here.
TIER_HIGH_MIN = 55
TIER_MEDIUM_MIN = 25


def _review_points(count: int) -> int:
    for threshold, points in _REVIEW_BUCKETS:
        if count >= threshold:
            return points
    return 0


def detect_traffic(
    html_lower: str,
    pages_scanned: list[str],
    branches_estimate: int,
) -> tuple[str, int, str, list[str]]:
    """Score how busy a business is, from pages we already fetched.

    Returns (tier, score, evidence, signals):
      tier   — high | medium | low | unknown
      score  — 0-100, for fine-grained ordering WITHIN a tier
      evidence — human-readable, e.g. "1247 reviews; 3 trackers; live chat"
      signals  — sorted machine-readable keys, for later threshold tuning

    `unknown` means "not enough page text to judge", and is deliberately
    distinct from `low`. Callers must never drop an `unknown`.
    """
    if len(html_lower.strip()) < MIN_HTML_FOR_JUDGMENT:
        return ("unknown", 0, "insufficient html to judge traffic", [])

    reviews, review_signals = detect_review_count(html_lower)
    depth, tracker_signals = detect_tracker_depth(html_lower)
    op_points, op_signals = detect_operational_load(
        html_lower, pages_scanned, branches_estimate
    )

    score = (
        _review_points(reviews)
        + min(depth * _TRACKER_POINTS_EACH, _TRACKER_MAX)
        + op_points
    )
    score = max(0, min(score, 100))

    if score >= TIER_HIGH_MIN:
        tier = "high"
    elif score >= TIER_MEDIUM_MIN:
        tier = "medium"
    else:
        tier = "low"

    bits: list[str] = []
    if reviews:
        bits.append(f"{reviews} reviews")
    if depth:
        bits.append(f"{depth} tracker{'s' if depth != 1 else ''}")
    if "chat_widget" in op_signals:
        bits.append("live chat")
    if "multilang" in op_signals:
        bits.append("multi-language")
    if "many_pages" in op_signals:
        bits.append(f"{len(pages_scanned)} pages")
    if "multi_branch" in op_signals:
        bits.append(f"{branches_estimate} branches")
    evidence = "; ".join(bits) if bits else "no demand signals found"

    signals = sorted(review_signals + tracker_signals + op_signals)
    return (tier, score, evidence, signals)
