# Web-Traffic Qualification Signal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score how busy each business actually is, using HTML the pipeline already fetched, and use that score to order the scarce Stage 5.5 enrichment slots on every campaign — with an optional per-campaign floor that drops clearly-dead sites.

**Architecture:** A new pure module `traffic_signals.py` exposes three small detectors (review counts, tracker-stack depth, operational load) composed by one public `detect_traffic()`. `extract_leads.py` calls it once per domain and writes four additive fields onto each lead. `qualify_leads.py` uses those fields as the leading sort key for all runs, plus an opt-in `min_traffic_tier` floor read from `<run>/qualify.json`.

**Tech Stack:** Python 3.14 (`project/tools/venv/bin/python`), pytest 9.0.3, stdlib only — `re`, `json`, `pathlib`, `argparse`. No new dependency.

## Global Constraints

- **No new network calls, no new dependency, no paid API.** The module reads only HTML already on disk under `runs/<slug>/raw_html/`. The free-stack constraint is non-negotiable.
- **`detect_traffic` must be pure and deterministic.** Same input always returns an identical tuple, across processes with differing `PYTHONHASHSEED`. `extract_leads.py` had a real nondeterminism bug in `pick_best` (11/279 leads changed `to_email` between re-extracts); never iterate a `set` into output — sort first.
- **`unknown` is never dropped.** A site we could not measure is our fetch failing, not a quiet business.
- **Traffic drops must NOT be written to `vault/lead-outreach/disqualified-log.txt`.** That ledger blocks a domain permanently across all channels and all future runs; a same-day crawl quality judgment is not that kind of proof.
- **Absent `min_traffic_tier` means no floor.** 20 of 21 fixtures have no `qualify.json`; they must be unaffected.
- **Additive schema only.** Never remove or rename an existing lead field.
- All commands run from `/Users/davidsmac/Downloads/lead-outreach-system 2/project`. Tests run with `tools/venv/bin/python -m pytest`.

---

### Task 1: `detect_review_count` — real customer counts

A review count is a literal tally of people who transacted; it is the strongest free traffic proxy for a small business.

**Files:**
- Create: `project/tools/scripts/traffic_signals.py`
- Test: `project/tests/test_traffic_signals.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `detect_review_count(html_lower: str) -> tuple[int, list[str]]` returning `(count, signals)`. `count` is clamped to `REVIEW_CLAMP = 50_000`. `signals` is a sorted list of the matcher names that fired, drawn from `{"reviews_jsonld", "reviews_microdata", "reviews_text"}`.

**Important:** the caller passes **already-lowercased** HTML (`extract_leads.py:296` computes `html_lower = html.lower()`). All patterns must therefore match lowercase keys — `reviewcount`, not `reviewCount`.

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_traffic_signals.py`:

```python
"""Traffic signal detection — free demand proxies from already-fetched HTML.

The caller (extract_leads.py:296) passes html.lower(), so every pattern here
must match LOWERCASE keys: reviewcount, not reviewCount.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from traffic_signals import REVIEW_CLAMP, detect_review_count


def test_jsonld_review_count():
    html = '<script type="application/ld+json">{"@type":"aggregaterating","reviewcount":1247}</script>'
    count, signals = detect_review_count(html)
    assert count == 1247
    assert "reviews_jsonld" in signals


def test_jsonld_review_count_quoted():
    html = '{"aggregaterating":{"ratingvalue":"4.8","reviewcount":"892"}}'
    count, _ = detect_review_count(html)
    assert count == 892


def test_microdata_review_count():
    html = '<span itemprop="reviewcount" content="431">431 reviews</span>'
    count, signals = detect_review_count(html)
    assert count == 431
    assert "reviews_microdata" in signals


def test_microdata_reversed_attribute_order():
    html = '<meta content="512" itemprop="ratingcount">'
    count, _ = detect_review_count(html)
    assert count == 512


def test_visible_text_review_count():
    html = "<p>rated 4.8 based on 1,247 reviews</p>"
    count, signals = detect_review_count(html)
    assert count == 1247
    assert "reviews_text" in signals


@pytest.mark.parametrize("html,expected", [
    ("basado en 320 opiniones", 320),
    ("basé sur 210 avis", 210),
    ("basata su 145 recensioni", 145),
    ("basierend auf 87 bewertungen", 87),
])
def test_visible_text_multilingual(html, expected):
    count, _ = detect_review_count(html)
    assert count == expected


def test_european_thousands_separator():
    count, _ = detect_review_count("basata su 1.247 recensioni")
    assert count == 1247


def test_decimal_rating_is_not_a_count():
    """4.8 is a rating, not 48 reviews."""
    count, _ = detect_review_count("rating 4.8 stars")
    assert count == 0


def test_takes_the_maximum_match():
    html = '{"reviewcount":12} <p>based on 980 reviews</p>'
    count, _ = detect_review_count(html)
    assert count == 980


def test_absurd_count_is_clamped():
    count, _ = detect_review_count('{"reviewcount":999999999}')
    assert count == REVIEW_CLAMP


def test_no_reviews_returns_zero_and_no_signals():
    count, signals = detect_review_count("<html><body>welcome</body></html>")
    assert count == 0
    assert signals == []


def test_malformed_jsonld_does_not_raise():
    html = '<script type="application/ld+json">{"reviewcount": ,,, broken</script>'
    count, signals = detect_review_count(html)
    assert isinstance(count, int)
    assert isinstance(signals, list)


def test_signals_are_sorted_for_determinism():
    html = '{"reviewcount":50} <span itemprop="ratingcount" content="60"></span> based on 70 reviews'
    _, signals = detect_review_count(html)
    assert signals == sorted(signals)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'traffic_signals'`

- [ ] **Step 3: Write minimal implementation**

Create `project/tools/scripts/traffic_signals.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS — 16 passed

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/traffic_signals.py project/tests/test_traffic_signals.py
git commit -m "feat(traffic): detect review counts as a free demand proxy"
```

---

### Task 2: `detect_tracker_depth` — traffic-driving stack

Nobody installs a retargeting stack for a site nobody visits. Paying to drive traffic is itself the signal.

**Files:**
- Modify: `project/tools/scripts/traffic_signals.py` (append)
- Test: `project/tests/test_traffic_signals.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1 (independent detector in the same module).
- Produces: `detect_tracker_depth(html_lower: str) -> tuple[int, list[str]]` returning `(distinct_vendor_count, signals)`. Signals are `"tracker_<vendor>"` names, sorted. Counting is by **distinct vendor**, not by occurrence — one GTM snippet repeated four times on a page is one vendor.

- [ ] **Step 1: Write the failing test**

Append to `project/tests/test_traffic_signals.py`:

```python
from traffic_signals import detect_tracker_depth


@pytest.mark.parametrize("html,vendor", [
    ('<script src="https://www.googletagmanager.com/gtag/js?id=g-ab12cd34"></script>', "ga4"),
    ('<script>(function(w,d){})(window,document);var x="gtm-abc1234";</script>', "gtm"),
    ('<script>fbq("init","123456");</script>', "meta_pixel"),
    ('<script src="https://connect.facebook.net/en_us/fbevents.js"></script>', "meta_pixel"),
    ('<script src="https://www.googleadservices.com/pagead/conversion.js"></script>', "google_ads"),
    ('<script>var aw="aw-987654321";</script>', "google_ads"),
    ('<script src="https://static.hotjar.com/c/hotjar-123.js"></script>', "hotjar"),
    ('<script src="https://www.clarity.ms/tag/abcd"></script>', "clarity"),
    ('<script src="https://cdn.segment.com/analytics.js"></script>', "segment"),
    ('<script src="https://cdn.mxpnl.com/libs/mixpanel.js"></script>', "mixpanel"),
    ('<script src="https://analytics.tiktok.com/i18n/pixel/events.js"></script>', "tiktok"),
    ('<script src="https://snap.licdn.com/li.lms-analytics/insight.min.js"></script>', "linkedin_insight"),
])
def test_each_tracker_vendor_detected(html, vendor):
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == [f"tracker_{vendor}"]


def test_distinct_vendors_counted_not_occurrences():
    """One vendor repeated four times is depth 1, not 4."""
    html = 'gtm-aaa1111 gtm-bbb2222 gtm-ccc3333 gtm-ddd4444'
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_gtm"]


def test_full_marketing_stack():
    html = (
        '<script src="https://www.googletagmanager.com/gtag/js?id=g-xx"></script>'
        'gtm-yyy1111'
        '<script>fbq("init","1");</script>'
        '<script src="https://www.googleadservices.com/pagead/conversion.js"></script>'
    )
    depth, signals = detect_tracker_depth(html)
    assert depth == 4
    assert signals == sorted(signals)


def test_no_trackers():
    depth, signals = detect_tracker_depth("<html><body>plain site</body></html>")
    assert depth == 0
    assert signals == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -k tracker -v`
Expected: FAIL — `ImportError: cannot import name 'detect_tracker_depth'`

- [ ] **Step 3: Write minimal implementation**

Append to `project/tools/scripts/traffic_signals.py`:

```python
# Traffic-DRIVING stack. Presence means the business is actively spending to
# bring people to the site, and has enough volume to justify measuring it.
# Patterns are matched against lowercased html, so ids like GTM-ABC read gtm-abc.
_TRACKERS: dict[str, tuple[str, ...]] = {
    "ga4": (r"gtag/js\?id=g-", r"googletagmanager\.com/gtag"),
    "gtm": (r"gtm-[a-z0-9]{4,}", r"googletagmanager\.com/gtm\.js"),
    "meta_pixel": (r"fbq\s*\(", r"connect\.facebook\.net/[^\"']*fbevents\.js"),
    "google_ads": (r"googleadservices\.com", r"\baw-\d{6,}"),
    "hotjar": (r"static\.hotjar\.com", r"\bhj\s*\(", r"hjid\s*:"),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS — all Task 1 and Task 2 tests

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/traffic_signals.py project/tests/test_traffic_signals.py
git commit -m "feat(traffic): detect marketing-stack depth as a traffic proxy"
```

---

### Task 3: `detect_operational_load` — inbound volume forcing investment

You only staff live chat, translate a site, or run many pages when inbound volume forces you to.

**Files:**
- Modify: `project/tools/scripts/traffic_signals.py` (append)
- Test: `project/tests/test_traffic_signals.py` (append)

**Interfaces:**
- Consumes: nothing from Tasks 1–2.
- Produces: `detect_operational_load(html_lower: str, pages_scanned: list[str], branches_estimate: int) -> tuple[int, list[str]]` returning `(points, signals)` where `points` is 0–30 and signals are drawn from `{"chat_widget", "multilang", "many_pages", "multi_branch"}`, sorted.

- [ ] **Step 1: Write the failing test**

Append to `project/tests/test_traffic_signals.py`:

```python
from traffic_signals import detect_operational_load


@pytest.mark.parametrize("html", [
    '<script src="https://widget.intercom.io/widget/abc"></script>',
    '<script src="https://embed.tawk.to/123/default"></script>',
    '<script src="https://client.crisp.chat/l.js"></script>',
    '<script src="https://js.driftt.com/include/drift.js"></script>',
    '<script src="https://static.zdassets.com/ekr/snippet.js"></script>',
    '<script src="https://cdn.livechatinc.com/tracking.js"></script>',
    '<script src="https://code.tidio.co/abc.js"></script>',
])
def test_chat_widgets_detected(html):
    points, signals = detect_operational_load(html, [], 0)
    assert "chat_widget" in signals
    assert points >= 10


def test_multilang_needs_two_distinct_languages():
    html = ('<link rel="alternate" hreflang="en" href="/en">'
            '<link rel="alternate" hreflang="fr" href="/fr">')
    points, signals = detect_operational_load(html, [], 0)
    assert "multilang" in signals


def test_single_hreflang_is_not_multilang():
    html = '<link rel="alternate" hreflang="en" href="/en">'
    _, signals = detect_operational_load(html, [], 0)
    assert "multilang" not in signals


def test_hreflang_region_variants_are_one_language():
    """en-us and en-gb are the same language, not two."""
    html = ('<link rel="alternate" hreflang="en-us" href="/us">'
            '<link rel="alternate" hreflang="en-gb" href="/gb">')
    _, signals = detect_operational_load(html, [], 0)
    assert "multilang" not in signals


def test_many_pages():
    pages = [f"p{i}" for i in range(6)]
    _, signals = detect_operational_load("", pages, 0)
    assert "many_pages" in signals


def test_few_pages_is_not_many():
    _, signals = detect_operational_load("", ["a", "b"], 0)
    assert "many_pages" not in signals


def test_multi_branch():
    _, signals = detect_operational_load("", [], 5)
    assert "multi_branch" in signals


def test_operational_points_capped_at_30():
    html = ('<script src="https://widget.intercom.io/widget/abc"></script>'
            '<link rel="alternate" hreflang="en"><link rel="alternate" hreflang="de">')
    points, _ = detect_operational_load(html, [f"p{i}" for i in range(10)], 40)
    assert points == 30


def test_operational_empty_input():
    points, signals = detect_operational_load("", [], 0)
    assert points == 0
    assert signals == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -k operational -v`
Expected: FAIL — `ImportError: cannot import name 'detect_operational_load'`

- [ ] **Step 3: Write minimal implementation**

Append to `project/tools/scripts/traffic_signals.py`:

```python
# Operational load: investments a business only makes when inbound volume
# forces it to.
_CHAT_RE = re.compile(
    r"widget\.intercom\.io|embed\.tawk\.to|client\.crisp\.chat|js\.driftt\.com"
    r"|static\.zdassets\.com|cdn\.livechatinc\.com|code\.tidio\.co"
)
_HREFLANG_RE = re.compile(r'hreflang\s*=\s*["\']([a-z]{2})(?:-[a-z0-9]{2,3})?["\']')

_OP_CHAT_POINTS = 10
_OP_MULTILANG_POINTS = 8
_OP_PAGES_POINTS = 6
_OP_BRANCH_POINTS = 6
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS — all tests from Tasks 1–3

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/traffic_signals.py project/tests/test_traffic_signals.py
git commit -m "feat(traffic): detect operational-load signals"
```

---

### Task 4: `detect_traffic` — composition, scoring, tiering

**Files:**
- Modify: `project/tools/scripts/traffic_signals.py` (append)
- Test: `project/tests/test_traffic_signals.py` (append)

**Interfaces:**
- Consumes: `detect_review_count`, `detect_tracker_depth`, `detect_operational_load` from Tasks 1–3.
- Produces: the module's one public entry point, used by Tasks 5–8:
  ```python
  detect_traffic(
      html_lower: str,
      pages_scanned: list[str],
      branches_estimate: int,
  ) -> tuple[str, int, str, list[str]]
  ```
  Returns `(tier, score, evidence, signals)` — `tier` in `{"high","medium","low","unknown"}`, `score` int 0–100, `evidence` a human-readable string, `signals` a sorted `list[str]`.
- Also produces `MIN_HTML_FOR_JUDGMENT: int` and `TIER_HIGH_MIN` / `TIER_MEDIUM_MIN` thresholds, which Task 5 recalibrates.

**Thresholds are PROVISIONAL in this task.** Task 5 measures the real distribution over 5,368 files already on disk and tunes them, recording the measurement in the module.

- [ ] **Step 1: Write the failing test**

Append to `project/tests/test_traffic_signals.py`:

```python
from traffic_signals import MIN_HTML_FOR_JUDGMENT, detect_traffic


def _pad(core: str = "") -> str:
    """Real HTML long enough to be judged (short docs return 'unknown')."""
    return core + ("<p>lorem ipsum dolor sit amet consectetur. </p>" * 80)


def test_short_html_is_unknown_never_low():
    tier, score, evidence, signals = detect_traffic("<html></html>", [], 0)
    assert tier == "unknown"
    assert score == 0
    assert signals == []
    assert "insufficient" in evidence


def test_empty_html_is_unknown():
    assert detect_traffic("", [], 0)[0] == "unknown"


def test_whitespace_only_html_is_unknown():
    assert detect_traffic("   \n\t  ", [], 0)[0] == "unknown"


def test_min_html_threshold_is_the_boundary():
    assert detect_traffic("x" * (MIN_HTML_FOR_JUDGMENT - 1), [], 0)[0] == "unknown"
    assert detect_traffic("x" * MIN_HTML_FOR_JUDGMENT, [], 0)[0] != "unknown"


def test_busy_site_is_high():
    html = _pad(
        '{"reviewcount":1500}'
        '<script src="https://www.googletagmanager.com/gtag/js?id=g-x"></script>'
        'gtm-abc1234'
        '<script>fbq("init","1");</script>'
        '<script src="https://widget.intercom.io/widget/abc"></script>'
    )
    tier, score, evidence, signals = detect_traffic(html, [f"p{i}" for i in range(8)], 6)
    assert tier == "high"
    assert score >= 55
    assert "1500 reviews" in evidence
    assert signals == sorted(signals)


def test_quiet_site_is_low():
    tier, score, _, _ = detect_traffic(_pad("<p>welcome to our small shop</p>"), ["index"], 0)
    assert tier == "low"
    assert score < 25


def test_middling_site_is_medium():
    html = _pad('{"reviewcount":60}<script src="https://www.googletagmanager.com/gtag/js?id=g-x"></script>')
    tier, score, _, _ = detect_traffic(html, ["index", "contact"], 0)
    assert tier == "medium"
    assert 25 <= score < 55


def test_score_never_exceeds_100():
    html = _pad(
        '{"reviewcount":49000}'
        '<script src="https://www.googletagmanager.com/gtag/js?id=g-x"></script>'
        'gtm-abc1234 fbq( googleadservices.com static.hotjar.com clarity.ms/tag'
        'cdn.segment.com cdn.mxpnl.com analytics.tiktok.com snap.licdn.com'
        '<script src="https://widget.intercom.io/widget/abc"></script>'
        '<link rel="alternate" hreflang="en"><link rel="alternate" hreflang="de">'
    )
    _, score, _, _ = detect_traffic(html, [f"p{i}" for i in range(20)], 50)
    assert 0 <= score <= 100


def test_deterministic_across_repeated_calls():
    html = _pad('{"reviewcount":300}gtm-abc1234 fbq(')
    first = detect_traffic(html, ["a", "b"], 3)
    for _ in range(20):
        assert detect_traffic(html, ["a", "b"], 3) == first


def test_deterministic_across_hash_seeds():
    """Guards the PYTHONHASHSEED class of bug that hit pick_best."""
    import subprocess
    import sys as _sys
    snippet = (
        "import sys; sys.path.insert(0, %r);"
        "from traffic_signals import detect_traffic;"
        "print(detect_traffic('{\"reviewcount\":300}gtm-abc1234 fbq(' + 'x'*3000,"
        "['a','b'], 3))" % str(PROJECT / "tools" / "scripts")
    )
    outs = set()
    for seed in ("0", "1", "42", "12345"):
        r = subprocess.run([_sys.executable, "-c", snippet], capture_output=True,
                           text=True, env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert len(outs) == 1


def test_evidence_is_human_readable():
    html = _pad('{"reviewcount":420}gtm-abc1234')
    _, _, evidence, _ = detect_traffic(html, ["a"], 0)
    assert "420 reviews" in evidence
    assert "tracker" in evidence.lower() or "1" in evidence
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -k traffic -v`
Expected: FAIL — `ImportError: cannot import name 'detect_traffic'`

- [ ] **Step 3: Write minimal implementation**

Append to `project/tools/scripts/traffic_signals.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS — all tests from Tasks 1–4

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/traffic_signals.py project/tests/test_traffic_signals.py
git commit -m "feat(traffic): compose detectors into detect_traffic with tiering"
```

---

### Task 5: `--report` calibration mode, and tune the thresholds

Thresholds must come from measurement, not from my guess. There are **5,368 already-fetched HTML files** under `project/runs/`, including 790 in `2026-08-01-eu-hotels`. Reusing them costs nothing.

**Files:**
- Modify: `project/tools/scripts/traffic_signals.py` (append CLI)
- Test: `project/tests/test_traffic_signals.py` (append)

**Interfaces:**
- Consumes: `detect_traffic` from Task 4.
- Produces: a CLI `python3 tools/scripts/traffic_signals.py --report <run-dir>`, and **recalibrated** `TIER_HIGH_MIN` / `TIER_MEDIUM_MIN` constants that Tasks 7–9 depend on. Adds no new importable function beyond `main()`.

- [ ] **Step 1: Write the failing test**

Append to `project/tests/test_traffic_signals.py`:

```python
import json
import subprocess
import sys as _sys

SCRIPT = PROJECT / "tools" / "scripts" / "traffic_signals.py"


def test_report_mode_prints_distribution(tmp_path):
    raw = tmp_path / "raw_html"
    raw.mkdir()
    busy = ('{"reviewcount":1500}gtm-abc1234 fbq( '
            'https://www.googletagmanager.com/gtag/js?id=g-x '
            'https://widget.intercom.io/widget/abc') + ("<p>text</p>" * 400)
    quiet = "<p>a small quiet shop</p>" * 400
    (raw / "busy-com__index.html").write_text(busy)
    (raw / "quiet-com__index.html").write_text(quiet)
    (tmp_path / "candidates-all.txt").write_text(
        "busy.com|Busy|ES|hotel|0\nquiet.com|Quiet|ES|hotel|0\n"
    )

    r = subprocess.run(
        [_sys.executable, str(SCRIPT), "--report", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "high" in r.stdout
    assert "low" in r.stdout
    assert "hotel" in r.stdout


def test_report_mode_json_output(tmp_path):
    raw = tmp_path / "raw_html"
    raw.mkdir()
    (raw / "x-com__index.html").write_text("<p>hello</p>" * 400)
    (tmp_path / "candidates-all.txt").write_text("x.com|X|ES|hotel|0\n")

    r = subprocess.run(
        [_sys.executable, str(SCRIPT), "--report", str(tmp_path), "--json"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["total"] == 1
    assert set(payload["tiers"]) == {"high", "medium", "low", "unknown"}


def test_report_mode_missing_run_dir_exits_nonzero(tmp_path):
    r = subprocess.run(
        [_sys.executable, str(SCRIPT), "--report", str(tmp_path / "nope")],
        capture_output=True, text=True,
    )
    assert r.returncode != 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -k report -v`
Expected: FAIL — the script has no `--report` flag, exits non-zero on the first two tests

- [ ] **Step 3: Write minimal implementation**

Append to `project/tools/scripts/traffic_signals.py`:

```python
def _report(run_dir, as_json: bool) -> int:
    """Score every domain in a run's raw_html/ and print the tier distribution.

    Calibration only — reads pages already on disk, never fetches.
    """
    from collections import Counter
    from pathlib import Path as _Path

    root = _Path(run_dir).resolve()
    raw = root / "raw_html"
    if not raw.is_dir():
        print(f"ABORT: no raw_html/ under {root}")
        return 2

    verticals: dict[str, str] = {}
    branches: dict[str, int] = {}
    cand = root / "candidates-all.txt"
    if cand.exists():
        for line in cand.read_text().splitlines():
            parts = line.split("|")
            if len(parts) >= 5:
                verticals[parts[0].strip().lower()] = parts[3].strip().lower()
                try:
                    branches[parts[0].strip().lower()] = int(parts[4])
                except ValueError:
                    branches[parts[0].strip().lower()] = 0

    pages_by_domain: dict[str, list[_Path]] = {}
    for f in sorted(raw.glob("*.html")):
        domain = f.name.split("__", 1)[0].replace("-", ".")
        pages_by_domain.setdefault(domain, []).append(f)

    tiers: Counter[str] = Counter()
    by_vertical: dict[str, Counter[str]] = {}
    signal_freq: Counter[str] = Counter()
    scores: list[int] = []

    for domain, files in sorted(pages_by_domain.items()):
        chunks = []
        for f in files:
            try:
                if f.stat().st_size >= 500:
                    chunks.append(f.read_text(errors="ignore"))
            except OSError:
                continue
        html_lower = "\n".join(chunks).lower()
        tier, score, _, signals = detect_traffic(
            html_lower, [f.name for f in files], branches.get(domain, 0)
        )
        tiers[tier] += 1
        scores.append(score)
        signal_freq.update(signals)
        v = verticals.get(domain, "unknown")
        by_vertical.setdefault(v, Counter())[tier] += 1

    total = sum(tiers.values())
    if as_json:
        print(json.dumps({
            "run": root.name,
            "total": total,
            "tiers": {t: tiers.get(t, 0) for t in ("high", "medium", "low", "unknown")},
            "by_vertical": {v: dict(c) for v, c in sorted(by_vertical.items())},
            "signals": dict(signal_freq.most_common()),
            "score_min": min(scores) if scores else 0,
            "score_max": max(scores) if scores else 0,
        }, indent=2))
        return 0

    print(f"Traffic report for {root.name}: {total} domains")
    for t in ("high", "medium", "low", "unknown"):
        n = tiers.get(t, 0)
        pct = (100.0 * n / total) if total else 0.0
        print(f"  {t:<8} {n:>5}  {pct:5.1f}%")
    print("  by vertical:")
    for v, c in sorted(by_vertical.items()):
        row = " ".join(f"{t}={c.get(t, 0)}" for t in ("high", "medium", "low", "unknown"))
        print(f"    {v:<16} {row}")
    print("  signal frequency:")
    for sig, n in signal_freq.most_common():
        print(f"    {sig:<24} {n}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", metavar="RUN_DIR",
                   help="score every domain in RUN_DIR/raw_html and print the "
                        "tier distribution (calibration only, never fetches)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args()
    if not a.report:
        p.error("nothing to do: pass --report <run-dir>")
    return _report(a.report, a.json)


if __name__ == "__main__":
    import json
    raise SystemExit(main())
```

Move `import json` to the module's top-level import block (next to `import re`) so `_report` can use it in both paths, and delete the local `import json` under `__main__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS — all tests from Tasks 1–5

- [ ] **Step 5: Run the real calibration**

```bash
cd "/Users/davidsmac/Downloads/lead-outreach-system 2/project"
for d in runs/*/; do
  [ -d "$d/raw_html" ] || continue
  tools/venv/bin/python tools/scripts/traffic_signals.py --report "$d" --json
done > /tmp/traffic-calibration.json 2>/dev/null
tools/venv/bin/python tools/scripts/traffic_signals.py --report runs/2026-08-01-eu-hotels
```

- [ ] **Step 6: Tune the thresholds from what you measured**

A detector that labels 95% of leads `high`, or 95% `low`, is worthless. Adjust `TIER_HIGH_MIN` and `TIER_MEDIUM_MIN` so the distribution discriminates — roughly 15–30% `high`, 30–50% `medium`, the rest `low` — excluding `unknown` from that calculation.

Then, for any run that has **both** `raw_html/` and `enrich-summary.json` / `leads-dropped.json`, cross-tabulate tier against Stage 5.5 outcome. If higher tiers really do enrich better, that is direct evidence the signal is real.

Record the measurement as a comment above the constants, in the style of `_WEAK_SOLO_KEYWORDS` at `extract_leads.py:113-121`:

```python
# Calibrated <DATE> against <N> domains across <M> runs already on disk.
# Distribution: high <a>%, medium <b>%, low <c>%, unknown <d>%.
# Stage 5.5 enrichment success by tier: high <w>%, medium <x>%, low <y>%.
TIER_HIGH_MIN = <measured>
TIER_MEDIUM_MIN = <measured>
```

If the cross-tabulation shows **no** relationship between tier and enrichment success, stop and report that before continuing to Task 6 — it would mean the signal does not predict what we hoped, and the floors in Task 9 must not ship.

- [ ] **Step 7: Re-run tests after tuning**

Run: `tools/venv/bin/python -m pytest tests/test_traffic_signals.py -v`
Expected: PASS. If the tier-boundary tests (`test_busy_site_is_high`, `test_quiet_site_is_low`, `test_middling_site_is_medium`) now fail, update the **fixtures** to match the calibrated thresholds — never loosen an assertion to `>= 0`.

- [ ] **Step 8: Commit**

```bash
git add project/tools/scripts/traffic_signals.py project/tests/test_traffic_signals.py
git commit -m "feat(traffic): add --report calibration mode and tune tiers from real runs"
```

---

### Task 6: Wire `detect_traffic` into `extract_leads.py`

**Files:**
- Modify: `project/tools/scripts/extract_leads.py` — imports, and the lead dict at lines 335-353
- Test: `project/tests/test_extract_traffic_fields.py` (create)

**Interfaces:**
- Consumes: `detect_traffic` from Task 4.
- Produces: four additive fields on every lead in `leads-extracted.json`, which Tasks 7–8 read:
  `traffic_tier: str`, `traffic_score: int`, `traffic_evidence: str`, `traffic_signals: list[str]`.

`extract_leads.py` is a module-level script (no `main()`), and the loop already has everything needed in scope: `html_lower` (line 296), `pages` (line 293), `branches` (line 325).

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_extract_traffic_fields.py`:

```python
"""Every extracted lead carries the four traffic fields, on every campaign."""
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
SCRIPT = PROJECT / "tools" / "scripts" / "extract_leads.py"

BUSY = (
    '<a href="mailto:owner@busy.com">owner@busy.com</a>'
    '{"reviewcount":1500}gtm-abc1234'
    '<script src="https://www.googletagmanager.com/gtag/js?id=g-x"></script>'
    '<script>fbq("init","1");</script>'
    '<script src="https://widget.intercom.io/widget/abc"></script>'
    '<a href="tel:+34123456789">call</a>'
) + ("<p>lorem ipsum dolor sit amet consectetur adipiscing. </p>" * 200)

QUIET = (
    '<a href="mailto:owner@quiet.com">owner@quiet.com</a>'
    '<a href="tel:+34123456789">call</a>'
) + ("<p>a small quiet family shop in town. </p>" * 200)


def _run(tmp_path):
    raw = tmp_path / "raw_html"
    raw.mkdir()
    (raw / "busy-com__index.html").write_text(BUSY)
    (raw / "quiet-com__index.html").write_text(QUIET)
    (tmp_path / "candidates-all.txt").write_text(
        "busy.com|Busy Hotel|ES|hotel|6\nquiet.com|Quiet Inn|ES|hotel|0\n"
    )
    sent = tmp_path / "sent-log.md"
    sent.write_text("")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(tmp_path), "--sent-log", str(sent)],
        capture_output=True, text=True, cwd=str(PROJECT),
    )
    assert r.returncode == 0, r.stderr
    out = tmp_path / "leads-extracted.json"
    return {l["lead_slug"]: l for l in
            (json.loads(x) for x in out.read_text().splitlines() if x.strip())}


def test_all_four_traffic_fields_present(tmp_path):
    leads = _run(tmp_path)
    assert leads, "extraction produced no leads"
    for lead in leads.values():
        assert lead["traffic_tier"] in ("high", "medium", "low", "unknown")
        assert isinstance(lead["traffic_score"], int)
        assert isinstance(lead["traffic_evidence"], str)
        assert isinstance(lead["traffic_signals"], list)


def test_busy_site_outranks_quiet_site(tmp_path):
    leads = _run(tmp_path)
    assert leads["busy-com"]["traffic_score"] > leads["quiet-com"]["traffic_score"]


def test_existing_fields_are_untouched(tmp_path):
    """The change is ADDITIVE — nothing downstream may break."""
    leads = _run(tmp_path)
    lead = leads["busy-com"]
    for field in ("lead_id", "lead_slug", "name", "website", "country_code",
                  "vertical", "branches_estimate", "to_email", "to_name",
                  "email_class", "signal", "signal_evidence", "hotel_volume",
                  "hotel_volume_evidence", "score", "pages_scanned", "emails_found"):
        assert field in lead, f"existing field {field} disappeared"


def test_traffic_applies_to_every_vertical_not_just_hotels(tmp_path):
    """Unlike hotel_volume, traffic is computed for all campaigns."""
    raw = tmp_path / "raw_html"
    raw.mkdir()
    (raw / "busy-com__index.html").write_text(BUSY)
    (tmp_path / "candidates-all.txt").write_text("busy.com|Busy Law|US|law|6\n")
    sent = tmp_path / "sent-log.md"
    sent.write_text("")
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(tmp_path), "--sent-log", str(sent)],
        capture_output=True, text=True, cwd=str(PROJECT),
    )
    assert r.returncode == 0, r.stderr
    lead = json.loads((tmp_path / "leads-extracted.json").read_text().splitlines()[0])
    assert lead["vertical"] == "law"
    assert lead["hotel_volume"] == "na"        # hotel gate stays hotel-only
    assert lead["traffic_tier"] != "unknown"   # traffic is universal
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_extract_traffic_fields.py -v`
Expected: FAIL — `KeyError: 'traffic_tier'`

- [ ] **Step 3: Write minimal implementation**

In `project/tools/scripts/extract_leads.py`, add the import next to the other local-module imports near the top of the file:

```python
from traffic_signals import detect_traffic
```

Then, immediately after the hotel-volume block that currently ends at line 333, add:

```python
    # Traffic / demand signal — EVERY vertical, not just hotels. Reads only the
    # HTML already fetched above, so it costs no request and no token.
    traffic_tier, traffic_score, traffic_evidence, traffic_signals = detect_traffic(
        html_lower, pages, branches
    )
```

And extend the lead dict (currently lines 335-353) with four entries, placed after `"hotel_volume_evidence"` and before `"score"`:

```python
        "traffic_tier": traffic_tier,
        "traffic_score": traffic_score,
        "traffic_evidence": traffic_evidence,
        "traffic_signals": traffic_signals,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_extract_traffic_fields.py -v`
Expected: PASS — 4 passed

- [ ] **Step 5: Verify no regression in the existing suite**

Run: `tools/venv/bin/python -m pytest tests/ -v`
Expected: PASS — all pre-existing tests still green

- [ ] **Step 6: Commit**

```bash
git add project/tools/scripts/extract_leads.py project/tests/test_extract_traffic_fields.py
git commit -m "feat(traffic): emit traffic fields on every extracted lead"
```

---

### Task 7: Rank by traffic in `qualify_leads.py` — all runs, no config

This is the change that delivers the goal: the sort decides who gets one of the 250 enrichment slots.

**Files:**
- Modify: `project/tools/scripts/qualify_leads.py` — constants near line 67, sort at lines 164-169
- Test: `project/tests/test_qualify_traffic.py` (create)

**Interfaces:**
- Consumes: the `traffic_tier` / `traffic_score` fields from Task 6.
- Produces: `TRAFFIC_RANK: dict[str, int]` = `{"high": 3, "medium": 2, "unknown": 1, "low": 0}` in `qualify_leads.py`, used again by Task 8.

**`qualify_leads.py` parses argv at module level (lines 32-50), so it cannot be imported.** All tests here run it as a subprocess. Run with `cwd=tmp_path` so the relative ledger path `vault/lead-outreach/disqualified-log.txt` lands inside the tmp dir — that is what makes Task 8's Rule 2 testable.

- [ ] **Step 1: Write the failing test**

Create `project/tests/test_qualify_traffic.py`:

```python
"""Traffic ordering and the optional per-campaign floor.

qualify_leads.py parses argv at module level, so it cannot be imported — every
test runs it as a subprocess. cwd is tmp_path so the RELATIVE ledger path
vault/lead-outreach/disqualified-log.txt lands inside the tmp dir.
"""
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
SCRIPT = PROJECT / "tools" / "scripts" / "qualify_leads.py"


def _lead(slug, **over):
    base = {
        "lead_id": f"web-{slug}", "lead_slug": slug, "name": slug,
        "website": f"https://{slug.replace('-', '.')}", "country_code": "ES",
        "vertical": "hotel", "branches_estimate": 0,
        "to_email": f"owner@{slug.replace('-', '.')}", "to_name": slug,
        "email_class": "person", "signal": "phone_led", "signal_evidence": "",
        "hotel_volume": "na", "hotel_volume_evidence": "",
        "traffic_tier": "medium", "traffic_score": 40, "traffic_evidence": "",
        "traffic_signals": [], "score": 90, "pages_scanned": ["index"],
        "emails_found": 1,
    }
    base.update(over)
    return base


def _run(tmp_path, leads, qualify_cfg=None, extra_args=()):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "leads-extracted.json").write_text(
        "\n".join(json.dumps(l) for l in leads) + "\n"
    )
    if qualify_cfg is not None:
        (run_dir / "qualify.json").write_text(json.dumps(qualify_cfg))
    (tmp_path / "vault" / "lead-outreach").mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--run-dir", str(run_dir), *extra_args],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    out = run_dir / "leads-qualified.json"
    kept = ([json.loads(x) for x in out.read_text().splitlines() if x.strip()]
            if out.exists() else [])
    ledger = tmp_path / "vault" / "lead-outreach" / "disqualified-log.txt"
    return r, kept, (ledger.read_text() if ledger.exists() else "")


def test_high_traffic_ranks_before_low(tmp_path):
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("high-com", traffic_tier="high", traffic_score=80),
        _lead("medium-com", traffic_tier="medium", traffic_score=40),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["high-com", "medium-com", "low-com"]


def test_unknown_ranks_above_low_below_medium(tmp_path):
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("unknown-com", traffic_tier="unknown", traffic_score=0),
        _lead("medium-com", traffic_tier="medium", traffic_score=40),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["medium-com", "unknown-com", "low-com"]


def test_score_still_breaks_ties_within_a_tier(tmp_path):
    """Traffic leads at BUCKET granularity; score still orders inside a bucket."""
    leads = [
        _lead("weak-com", traffic_tier="high", traffic_score=60, score=82),
        _lead("strong-com", traffic_tier="high", traffic_score=60, score=96),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["strong-com", "weak-com"]


def test_traffic_ranking_needs_no_config(tmp_path):
    """No qualify.json at all — ordering still applies (all runs)."""
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("high-com", traffic_tier="high", traffic_score=80),
    ]
    _, kept, _ = _run(tmp_path, leads, qualify_cfg=None)
    assert [l["lead_slug"] for l in kept] == ["high-com", "low-com"]


def test_ranking_drops_nobody(tmp_path):
    leads = [
        _lead("a-com", traffic_tier="low", traffic_score=0),
        _lead("b-com", traffic_tier="high", traffic_score=90),
        _lead("c-com", traffic_tier="unknown", traffic_score=0),
    ]
    _, kept, ledger = _run(tmp_path, leads)
    assert len(kept) == 3
    assert "traffic" not in ledger


def test_lead_without_traffic_fields_is_treated_as_unknown(tmp_path):
    """Backwards compatibility with leads from an older extract_leads.py."""
    old = _lead("old-com")
    for f in ("traffic_tier", "traffic_score", "traffic_evidence", "traffic_signals"):
        old.pop(f)
    leads = [_lead("low-com", traffic_tier="low", traffic_score=5), old]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["old-com", "low-com"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_qualify_traffic.py -v`
Expected: FAIL — ordering tests fail; the current sort ignores traffic entirely

- [ ] **Step 3: Write minimal implementation**

In `project/tools/scripts/qualify_leads.py`, add after `VOLUME_RANK` (line 67):

```python
# Traffic / demand tier, produced for EVERY vertical by extract_leads.py.
# `unknown` sits ABOVE `low` deliberately: it means "we could not measure this
# site", which is our fetch failing, not the business being quiet. A lead we
# failed to measure must not be punished as though we had measured it.
TRAFFIC_RANK = {"high": 3, "medium": 2, "unknown": 1, "low": 0}


def _traffic_tier(lead: dict) -> str:
    """Missing field (lead from an older extract) reads as `unknown`, never `low`."""
    return str(lead.get("traffic_tier") or "unknown").lower()
```

Then replace the sort at lines 164-169 with:

```python
    # Rank by fit. Traffic leads at BUCKET granularity so the scarce enrichment
    # slots go to the busiest businesses; the existing score still orders WITHIN
    # a bucket, which preserves the person>role email ordering that governs
    # whether enrichment can succeed at all.
    kept.sort(key=lambda l: (
        -TRAFFIC_RANK.get(_traffic_tier(l), 1),
        -VOLUME_RANK.get(l.get("hotel_volume", "na"), -1) if l.get("hotel_volume", "na") != "na" else 0,
        -int(l.get("score", 0)),
        CLASS_RANK.get(l.get("email_class"), 9),
        -int(l.get("traffic_score", 0)),
        -int(l.get("branches_estimate", 0)),
    ))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_qualify_traffic.py -v`
Expected: PASS — 6 passed

- [ ] **Step 5: Commit**

```bash
git add project/tools/scripts/qualify_leads.py project/tests/test_qualify_traffic.py
git commit -m "feat(traffic): rank enrichment queue by traffic tier on all runs"
```

---

### Task 8: The opt-in `min_traffic_tier` floor, with both safety rules

**Files:**
- Modify: `project/tools/scripts/qualify_leads.py` — config read at lines 81-88, gate loop at 101-125, counters at 90, print at 176-183
- Test: `project/tests/test_qualify_traffic.py` (append)

**Interfaces:**
- Consumes: `TRAFFIC_RANK` and `_traffic_tier` from Task 7.
- Produces: the `min_traffic_tier` key contract that Task 9 writes into fixtures.

- [ ] **Step 1: Write the failing test**

Append to `project/tests/test_qualify_traffic.py`:

```python
def test_floor_drops_low_traffic(tmp_path):
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("high-com", traffic_tier="high", traffic_score=80),
    ]
    _, kept, _ = _run(tmp_path, leads, {"min_traffic_tier": "medium"})
    assert [l["lead_slug"] for l in kept] == ["high-com"]


def test_floor_keeps_at_and_above_the_tier(tmp_path):
    leads = [
        _lead("medium-com", traffic_tier="medium", traffic_score=40),
        _lead("high-com", traffic_tier="high", traffic_score=80),
    ]
    _, kept, _ = _run(tmp_path, leads, {"min_traffic_tier": "medium"})
    assert {l["lead_slug"] for l in kept} == {"medium-com", "high-com"}


def test_unknown_survives_any_floor(tmp_path):
    """RULE 1 — a site we failed to measure is our fetch failing, not a quiet business."""
    leads = [
        _lead("unknown-com", traffic_tier="unknown", traffic_score=0),
        _lead("low-com", traffic_tier="low", traffic_score=5),
    ]
    _, kept, _ = _run(tmp_path, leads, {"min_traffic_tier": "high"})
    assert [l["lead_slug"] for l in kept] == ["unknown-com"]


def test_traffic_drop_never_reaches_the_permanent_ledger(tmp_path):
    """RULE 2 — a same-day crawl verdict must not blacklist a domain forever."""
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("high-com", traffic_tier="high", traffic_score=80),
    ]
    _, kept, ledger = _run(tmp_path, leads, {"min_traffic_tier": "medium"})
    assert len(kept) == 1
    assert "low.com" not in ledger
    assert "traffic" not in ledger


def test_hotel_volume_drop_still_reaches_the_ledger(tmp_path):
    """Rule 2 is scoped to traffic — it must not disable the existing ledger."""
    leads = [
        _lead("small-com", vertical="hotel", hotel_volume="low", traffic_tier="high",
              traffic_score=80),
        _lead("big-com", vertical="hotel", hotel_volume="high", traffic_tier="high",
              traffic_score=80),
    ]
    _, kept, ledger = _run(
        tmp_path, leads,
        {"require_hotel_size_volume": True, "min_hotel_volume": "medium",
         "min_traffic_tier": "medium"},
    )
    assert [l["lead_slug"] for l in kept] == ["big-com"]
    assert "hotel-volume-too-low" in ledger
    assert "small.com" in ledger


def test_absent_key_means_no_floor(tmp_path):
    """Protects the 20 fixtures that have no qualify.json."""
    leads = [_lead("low-com", traffic_tier="low", traffic_score=5)]
    _, kept, _ = _run(tmp_path, leads, {})
    assert len(kept) == 1


def test_invalid_floor_value_behaves_as_absent(tmp_path):
    """A typo must never silently start dropping leads."""
    leads = [_lead("low-com", traffic_tier="low", traffic_score=5)]
    _, kept, _ = _run(tmp_path, leads, {"min_traffic_tier": "enormous"})
    assert len(kept) == 1


def test_floor_drop_is_counted_in_output(tmp_path):
    leads = [
        _lead("low-com", traffic_tier="low", traffic_score=5),
        _lead("high-com", traffic_tier="high", traffic_score=80),
    ]
    r, _, _ = _run(tmp_path, leads, {"min_traffic_tier": "medium"})
    assert "traffic" in r.stdout.lower()
    assert "1" in r.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `tools/venv/bin/python -m pytest tests/test_qualify_traffic.py -k floor -v`
Expected: FAIL — no floor exists; low-traffic leads are kept

- [ ] **Step 3: Write minimal implementation**

In `project/tools/scripts/qualify_leads.py`, after the `min_volume` line (88), add:

```python
    # Optional per-campaign traffic floor. ABSENT KEY = NO FLOOR, so the 20
    # fixtures with no qualify.json are untouched. An INVALID value also means
    # no floor: a typo must never silently start dropping leads.
    raw_floor = str(cfg.get("min_traffic_tier", "")).lower().strip()
    min_traffic = TRAFFIC_RANK.get(raw_floor) if raw_floor in TRAFFIC_RANK else None
    if raw_floor and min_traffic is None:
        print(f"  WARNING: ignoring invalid min_traffic_tier={raw_floor!r} "
              f"(expected one of high/medium/low) — no traffic floor applied")
    if min_traffic is not None and raw_floor == "unknown":
        print("  WARNING: min_traffic_tier='unknown' is meaningless — no floor applied")
        min_traffic = None
```

Change the counter line (90) to add `dropped_traffic = 0`:

```python
    dropped_freemail = dropped_score = dropped_signal = dropped_small_hotel = 0
    dropped_traffic = 0
```

Add the gate as the **last** check in the loop, immediately before `kept.append(l)` (line 125):

```python
        # Traffic floor. Deliberately does NOT append to `disqualified`: that
        # ledger blocks a domain permanently on every future run and every
        # channel, and a traffic verdict depends on how well we crawled the site
        # TODAY. `unknown` is exempt — we never punish a site we failed to measure.
        if min_traffic is not None:
            t = _traffic_tier(l)
            if t != "unknown" and TRAFFIC_RANK.get(t, 0) < min_traffic:
                dropped_traffic += 1
                continue
```

Add the report line after the hotel-volume print block (line 182):

```python
    if min_traffic is not None:
        print(f"  dropped low-traffic:     {dropped_traffic} "
              f"(need >= {raw_floor} traffic tier; not ledgered — this run only)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `tools/venv/bin/python -m pytest tests/test_qualify_traffic.py -v`
Expected: PASS — 14 passed

- [ ] **Step 5: Verify the whole suite is green**

Run: `tools/venv/bin/python -m pytest tests/ -v`
Expected: PASS — every test, old and new

- [ ] **Step 6: Commit**

```bash
git add project/tools/scripts/qualify_leads.py project/tests/test_qualify_traffic.py
git commit -m "feat(traffic): opt-in min_traffic_tier floor, unknown-safe and unledgered"
```

---

### Task 9: Set per-campaign floors from the calibration data

**Files:**
- Modify: `project/templates/eu-hotels/qualify.json`
- Create: `project/templates/<base>/qualify.json` for the campaigns that warrant a floor
- Modify: `project/ARCHITECTURE.md` (§3 run-control-files list, §6 safety-net table)

**Interfaces:**
- Consumes: `min_traffic_tier` from Task 8 and the measured distribution from Task 5.
- Produces: no code; configuration and documentation only.

**Do not guess a floor.** Use only the Task 5 numbers. Where a campaign has no calibration data — 19 of 21 fixtures have never fired a single city and therefore have no `raw_html/` at all — **leave the key absent.** An unmeasured campaign gets ranking (free, no risk) and no floor.

- [ ] **Step 1: Set the floor for calibrated campaigns only**

`eu-hotels` is the one campaign with substantial fetched HTML (790 files in `2026-08-01-eu-hotels`). Update `project/templates/eu-hotels/qualify.json`, keeping the existing keys:

```json
{
  "_comment": "EU hotels: only BIG, high-call-volume properties qualify. A hotel lead must show a size/volume signal (room count, 4-5 star, or resort/spa/conference scale) detected from its own site at extract. Small B&Bs and quiet guesthouses are dropped before any enrichment agent runs. Non-generic decision-maker email is enforced separately at Stage 5.5. min_traffic_tier added 2026-08-10: calibrated against <N> domains in runs/2026-08-01-eu-hotels — see traffic_signals.py for the measured distribution. `unknown` is exempt and traffic drops are NOT ledgered, so a bad crawl day cannot retire a domain.",
  "require_hotel_size_volume": true,
  "min_hotel_volume": "medium",
  "min_traffic_tier": "<measured>"
}
```

Replace `<N>` and `<measured>` with the Task 5 numbers. If the Task 5 cross-tabulation showed no relationship between tier and enrichment success, **omit `min_traffic_tier` entirely** and note that in the `_comment`.

- [ ] **Step 2: Verify the fixture is valid JSON and still fire-ready**

```bash
cd "/Users/davidsmac/Downloads/lead-outreach-system 2/project"
tools/venv/bin/python -c "import json;print(json.load(open('templates/eu-hotels/qualify.json')))"
bash tools/scripts/fire_campaign.sh eu-hotels --dry-run 2>&1 | head -20
```

Expected: the JSON parses, and the dry run gets past fixture validation. It will still abort at Stage 3 for exhausted ground — that is the correct, pre-existing behaviour and not a failure of this change.

- [ ] **Step 3: Update ARCHITECTURE.md**

In §3, add `min_traffic_tier` to the `qualify.json` description in the run-control-files paragraph. In the §6 safety-net table, add a row:

```markdown
| Traffic floor (per-campaign, opt-in) | `<run>/qualify.json` `min_traffic_tier`; `unknown` exempt; drops NOT ledgered | `qualify_leads.py` |
```

Also add a row recording that traffic ranking is unconditional:

```markdown
| Enrichment queue ordered by traffic tier | always, all campaigns, no config | `qualify_leads.py` |
```

- [ ] **Step 4: Run the full suite one last time**

Run: `tools/venv/bin/python -m pytest tests/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add project/templates/eu-hotels/qualify.json project/ARCHITECTURE.md
git commit -m "feat(traffic): set calibrated eu-hotels floor, document the gate"
```

---

## Self-Review

**Spec coverage.** Every section of `docs/superpowers/specs/2026-08-10-traffic-qualification-design.md` maps to a task: Component 1 → Tasks 1–4; signal groups A/B/C → Tasks 1/2/3; tiering and `unknown` → Task 4; Calibration → Task 5; Component 2 (extract) → Task 6; Component 3a (ranking) → Task 7; Component 3b (floor) + Rules 1 and 2 → Task 8; per-campaign configuration → Task 9. All 14 spec test cases appear: spec tests 1–7 in Tasks 1–4, spec tests 8–14 in Tasks 7–8. Spec error-handling items are covered — malformed JSON-LD (Task 1), clamping (Task 1), short/missing HTML (Task 4), malformed `qualify.json` and invalid floor value (Task 8), missing-field backwards compatibility (Tasks 7 and 8).

**Placeholder scan.** The only intentional fill-ins are the calibrated threshold values in Task 5 Step 6 and Task 9 Step 1. These are not plan failures: the spec explicitly requires them to be measured rather than guessed, Task 5 defines the exact procedure and the acceptance criterion (15–30% high / 30–50% medium), and Task 4 ships working provisional values so every task before 5 is fully executable. No step says "add appropriate error handling" or "write tests for the above".

**Type consistency.** `detect_review_count`, `detect_tracker_depth`, `detect_operational_load` all return `tuple[int, list[str]]`; `detect_traffic` returns `tuple[str, int, str, list[str]]` and is called with that arity in Tasks 4, 5 and 6. `TRAFFIC_RANK` and `_traffic_tier` are defined once in Task 7 and reused in Task 8. `MIN_HTML_FOR_JUDGMENT`, `REVIEW_CLAMP`, `TIER_HIGH_MIN`, `TIER_MEDIUM_MIN` are defined in Tasks 1/4 and imported by name in the tests that use them. The four lead fields are named identically in Tasks 6, 7 and 8.

**One risk flagged for the executor:** Task 5 Step 6 contains a genuine stop condition. If tier does not predict enrichment success, the floors in Task 9 must not ship — ranking alone is still safe and still lands, but the gate would be dropping leads on a signal that does not mean what we assumed.
