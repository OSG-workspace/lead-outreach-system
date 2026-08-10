"""Traffic signal detection — free demand proxies from already-fetched HTML.

The caller (extract_leads.py:296) passes html.lower(), so every pattern here
must match LOWERCASE keys: reviewcount, not reviewCount.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from traffic_signals import REVIEW_CLAMP, detect_review_count, detect_tracker_depth


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


def test_microdata_text_content_bare_digits():
    """Schema.org form where the count is the element's TEXT, not a content= attr."""
    html = '<span itemprop="reviewcount">1247</span>'
    count, signals = detect_review_count(html)
    assert count == 1247
    assert signals == ["reviews_microdata"]


def test_microdata_text_content_thousands_separator():
    html = '<span itemprop="ratingcount">1,247</span> reviews'
    count, signals = detect_review_count(html)
    assert count == 1247
    assert signals == ["reviews_microdata"]


def test_microdata_text_content_feeds_existing_signal_not_a_new_one():
    """The text-content form must contribute reviews_microdata, never a new signal name."""
    html = '<span itemprop="reviewcount">1247</span>'
    _, signals = detect_review_count(html)
    assert "reviews_microdata" in signals
    assert "reviews_text" not in signals
    assert len(signals) == 1


def test_void_element_does_not_leak_trailing_prose_into_a_count():
    """<meta> has no closing tag, so trailing prose after its '>' must not be
    mistaken for the element's text content. The real microdata value here is
    the content="0" attribute; "1,204 people follow this page" is unrelated."""
    html = '<meta itemprop="reviewcount" content="0">1,204 people follow this page'
    count, signals = detect_review_count(html)
    assert count == 0
    assert "reviews_microdata" not in signals


def test_void_element_attribute_form_still_works():
    """Proves the void-element fix didn't break the working content= attribute path."""
    html = '<meta itemprop="reviewcount" content="431">'
    count, signals = detect_review_count(html)
    assert count == 431
    assert "reviews_microdata" in signals


def test_span_text_content_unaffected_by_void_element_fix():
    html = '<span itemprop="reviewcount">1247</span>'
    count, signals = detect_review_count(html)
    assert count == 1247
    assert "reviews_microdata" in signals


def test_hyphenated_custom_element_starting_with_void_name_is_not_excluded():
    """Custom Elements must contain a hyphen, so <input-group> is the normal
    shape for such a tag. \\w+ would truncate the capture at "input" and
    wrongly match it against _VOID_ELEMENTS; the tag name must be captured
    whole so "input-group" (not in the void set) is correctly non-void."""
    html = '<input-group itemprop="reviewcount">1247</input-group>'
    count, signals = detect_review_count(html)
    assert count == 1247
    assert "reviews_microdata" in signals


def test_void_element_fix_still_holds_after_hyphen_widening():
    html = '<meta itemprop="reviewcount" content="0">1,204 people follow this page'
    count, signals = detect_review_count(html)
    assert count == 0
    assert "reviews_microdata" not in signals


def test_non_void_name_merely_starting_with_a_void_name_is_unaffected():
    html = '<linkbox itemprop="reviewcount">99</linkbox>'
    count, signals = detect_review_count(html)
    assert count == 99
    assert "reviews_microdata" in signals


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


@pytest.mark.parametrize("html", [
    "4.8 rating",          # digits directly followed by a matching review word: reaches the guard
    "rating 4.8 stars",    # kept for coverage, though "stars" never matches _REVIEW_WORDS
])
def test_decimal_rating_is_not_a_count(html):
    """4.8 is a rating, not 48 reviews."""
    count, _ = detect_review_count(html)
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
    """Pin WHICH signals fire (and in what order), not merely that the list is sorted —
    `detect_review_count` returns `sorted(...)` on every path, so `signals == sorted(signals)`
    holds trivially for any input and proves nothing on its own."""
    html = '{"reviewcount":50} <span itemprop="ratingcount" content="60"></span> based on 70 reviews'
    _, signals = detect_review_count(html)
    assert signals == ["reviews_jsonld", "reviews_microdata", "reviews_text"]


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
    assert signals == ["tracker_ga4", "tracker_google_ads", "tracker_gtm", "tracker_meta_pixel"]


def test_no_trackers():
    depth, signals = detect_tracker_depth("<html><body>plain site</body></html>")
    assert depth == 0
    assert signals == []


# Tests proving narrowed patterns work both ways:
# - Real embed snippets still fire
# - Generic lookalikes do NOT fire


def test_hotjar_real_embed_still_fires():
    """Prove static.hotjar.com still detects hotjar."""
    html = '<script src="https://static.hotjar.com/c/hotjar-12345.js"></script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_hotjar"]


def test_hotjar_hjid_context_still_fires():
    """Prove hjid: pattern still detects hotjar."""
    html = '<script>var hjid = "654321";</script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_hotjar"]


def test_hotjar_bare_function_call_does_not_fire():
    """Prove removed hj( pattern: bare two-char function doesn't fire."""
    html = '<script>function hj(x) { return x; } hj(foo);</script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 0
    assert signals == []


def test_meta_pixel_fbq_init_real_embed_still_fires():
    """Prove fbq('init',...) still detects meta_pixel."""
    html = '<script>fbq("init","123456");</script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_meta_pixel"]


def test_meta_pixel_fbq_with_single_quotes_still_fires():
    """Prove fbq('init',...) with single quotes still detects meta_pixel."""
    html = "<script>fbq('init','789012');</script>"
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_meta_pixel"]


def test_meta_pixel_fbq_bare_function_call_does_not_fire():
    """Prove narrowed fbq pattern: fbq(someVar) without 'init' does not fire."""
    html = '<script>function fbq(action) {} fbq(userData);</script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 0
    assert signals == []


def test_google_ads_aw_id_in_string_context_still_fires():
    """Prove "aw-<id>" or 'aw-<id>' in string context still detects google_ads."""
    html = '<script>var conversionId = "aw-987654321";</script>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_google_ads"]


def test_google_ads_aw_id_with_single_quote_still_fires():
    """Prove 'aw-<id>' variant still detects google_ads."""
    html = "<script>var id = 'aw-123456789';</script>"
    depth, signals = detect_tracker_depth(html)
    assert depth == 1
    assert signals == ["tracker_google_ads"]


def test_google_ads_bare_aw_id_without_quotes_does_not_fire():
    """Prove narrowed pattern: bare aw-123456 in unrelated markup like id-aw-102938 does not fire."""
    html = '<div class="item-id-aw-102938">Product asset</div>'
    depth, signals = detect_tracker_depth(html)
    assert depth == 0
    assert signals == []


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


def test_operational_all_signals_sum_to_max():
    """Verify that all four signals together sum to exactly _OP_MAX.
    The min() guard in detect_operational_load is defensive against future weight changes."""
    html = ('<script src="https://widget.intercom.io/widget/abc"></script>'
            '<link rel="alternate" hreflang="en"><link rel="alternate" hreflang="de">')
    points, signals = detect_operational_load(html, [f"p{i}" for i in range(6)], 5)
    assert len(signals) == 4
    assert points == 30


def test_hreflang_script_subtags_zh_hans_with_en():
    """zh-hans (4-char script subtag) should match and contribute to multilang when paired with en."""
    html = ('<link rel="alternate" hreflang="zh-hans" href="/zh">'
            '<link rel="alternate" hreflang="en" href="/en">')
    _, signals = detect_operational_load(html, [], 0)
    assert "multilang" in signals


def test_hreflang_script_subtags_zh_hans_and_zh_hant_same_language():
    """zh-hans and zh-hant both collapse to zh — one language, not multilang."""
    html = ('<link rel="alternate" hreflang="zh-hans" href="/zh-hans">'
            '<link rel="alternate" hreflang="zh-hant" href="/zh-hant">')
    _, signals = detect_operational_load(html, [], 0)
    assert "multilang" not in signals


def test_x_default_does_not_contribute_to_multilang():
    """x-default is private-use and x- does not match [a-z]{2}, so it doesn't register as a language."""
    html = ('<link rel="alternate" hreflang="x-default" href="/">'
            '<link rel="alternate" hreflang="en" href="/en">')
    _, signals = detect_operational_load(html, [], 0)
    assert "multilang" not in signals


def test_operational_empty_input():
    points, signals = detect_operational_load("", [], 0)
    assert points == 0
    assert signals == []


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
