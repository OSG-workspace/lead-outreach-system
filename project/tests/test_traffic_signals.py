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
    assert signals == sorted(signals)


def test_no_trackers():
    depth, signals = detect_tracker_depth("<html><body>plain site</body></html>")
    assert depth == 0
    assert signals == []
