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
