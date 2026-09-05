"""Tests for the tier-1 naming pass in perplexity_research.py (added 2026-09-05).

WHY THIS SUITE EXISTS
Stage 5.5's bill is a per-REQUEST search fee, not tokens: OpenRouter's own
cost_details on a live au-trades call put $0.005117 of a $0.00616 request on the
completions line for 109 output tokens. So the only way to spend less is to make
fewer sonar requests, and tier-1 does that by letting a cheap no-search model
answer the leads whose own website already names the owner.

That is only safe because of the accept gate, and the gate is the whole reason
this file exists. Measured on 69 real batch files from 2026-09-05-au-trades,
scored against that run's own final names:

    tier 1 returned a name   23/69   13 exact, 8 first-name-only,
                                     2 the BUSINESS NAME as a person
    gate accepted             8/69   divergences: ZERO

Ungated the pass is 31% cheaper and changes answers; gated it is ~9% cheaper and
provably changes none. "Same output" was the requirement, so every case below is
one of the real strings the gate had to reject or keep on that run. A regression
here does not fail loudly at run time — it silently sends "Hello Mr. Eagle," to a
business called Green Eagle Construction.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import perplexity_research as pxr  # noqa: E402


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize("first,last,business", [
    # Both tokens are the brand. Real tier-1 answers from the au-trades batches.
    ("Green", "Eagle", "Green Eagle Construction"),
    ("Guttering", "Adelaide", "Guttering Adelaide"),
    ("Luke", "Electrical", "Luke Electrical"),
])
def test_rejects_the_business_name_wearing_a_persons_shape(first, last, business):
    assert pxr.tier1_accept(first, last, business) == "name is the business name"


@pytest.mark.parametrize("first,last,business", [
    # An eponymous owner is the NORM in trades, so the gate must keep these:
    # only one token is the brand, the given name is real evidence.
    ("Ben", "Feltus", "Feltus Electrical"),
    ("Tim", "Clayton", "Clayton Electrical"),
    ("Gavin", "Best", "Best Roofing"),
    # No overlap at all — the ordinary case.
    ("Clint", "Buick", "Adelaide Precision Glass"),
    ("Lance", "White", "Green Eagle Construction"),
    ("Tom", "James-Martin", "Bluewater Plumbers"),
])
def test_keeps_a_real_person(first, last, business):
    assert pxr.tier1_accept(first, last, business) == ""


@pytest.mark.parametrize("first,last", [("Jamie", ""), ("", "Small"), ("Dan", "  ")])
def test_first_name_only_escalates(first, last):
    """8 of 23 tier-1 answers were a bare first name where the run had first +
    last. The surname is what the Mr./Mrs. salutation and the SMTP rescue's
    candidate list are built from, so a partial name must go to sonar, not
    downstream."""
    assert pxr.tier1_accept(first, last, "Pro Image Electrical") == "first-name-only"


def test_gate_is_case_and_accent_insensitive():
    assert pxr.tier1_accept("GREEN", "EAGLE", "green eagle construction")
    assert pxr.tier1_accept("Zurbrügg", "Zurbrügg", "Zurbrugg Zurbrugg")


# --- the all-caps repair ----------------------------------------------------

def test_shouting_is_repaired_but_real_capitalisation_is_not():
    """The live run returned "GAVIN BEST", which becomes "Hello Mr. BEST,".
    All-caps is never a chosen spelling; mixed case always is."""
    assert pxr._fix_shouting("GAVIN BEST") == "Gavin Best"
    assert pxr._fix_shouting("McDonald") == "McDonald"
    assert pxr._fix_shouting("O'Connor") == "O'Connor"
    assert pxr._fix_shouting("van der Berg") == "van der Berg"
    assert pxr._fix_shouting("") == ""


# --- the tier-1 prompt is the shipping prompt, minus the web ----------------

def test_tier1_prompt_is_derived_and_keeps_every_shared_rule():
    """Built by substitution from SYSTEM_PROMPT so the two cannot drift. If the
    anchor stops matching, the constant is None and annotate_batches skips the
    pass rather than shipping a half-edited prompt."""
    p = pxr.TIER1_SYSTEM_PROMPT
    assert p is not None, "anchor text no longer matches SYSTEM_PROMPT"
    assert "NO web access" in p
    assert "Green Eagle Construction" in p          # the explicit brand warning
    assert "Search the web only for" not in p       # the web instruction is gone
    for shared in ('"lead_id"', 'Mr.', "NEVER return info@", "DO NOT CONSTRUCT"):
        assert shared in p, f"tier-1 lost the shared rule: {shared}"


# --- the request payload ----------------------------------------------------

def test_request_caps_output_and_carries_the_requested_system_prompt(tmp_path, monkeypatch):
    batch = tmp_path / "enrich-batch-001.txt"
    batch.write_text("LeadId: web-example-com\nBusiness: Example\n"
                     "Website: https://example.com\n\nSitePages\nsome text\n")
    seen = {}

    def fake_post(payload, key, timeout):
        seen.update(payload)
        return {"choices": [{"message": {"content":
                '{"lead_id":"x","found":false,"first_name":"Ann","last_name":"Lee"}'}}],
                "usage": {"cost": 0.0}}

    monkeypatch.setattr(pxr, "_post", fake_post)
    pxr.research_one(batch, "k", system=pxr.TIER1_SYSTEM_PROMPT, model=pxr.TIER1_MODEL)
    assert seen["max_tokens"] == pxr.MAX_OUTPUT_TOKENS   # unbounded replies cost money
    assert seen["model"] == pxr.TIER1_MODEL
    assert seen["messages"][0]["content"] == pxr.TIER1_SYSTEM_PROMPT
    # Default call must still be the search-grounded prompt, unchanged.
    pxr.research_one(batch, "k")
    assert seen["messages"][0]["content"] == pxr.SYSTEM_PROMPT
    assert seen["model"] == pxr.DEFAULT_MODEL


def test_sitepages_cap_is_a_backstop_not_the_real_limit():
    """extract_page_text already caps SitePages at total_chars=3600; real
    au-trades batches averaged 1,840 chars. Anyone lowering MAX_SITEPAGES_CHARS
    to save money is tuning a knob that does not bind — the docstring says so
    and this asserts the ordering stays that way."""
    assert pxr.MAX_SITEPAGES_CHARS > 3600
