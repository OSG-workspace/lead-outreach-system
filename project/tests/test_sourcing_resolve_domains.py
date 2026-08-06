"""Tests for run_fire.py's auto-enable of Stage 2.5 domain resolution.

Regression context (2026-08-03): source_places.py's default tier flipped from
"enterprise" (requests + persists websiteUri/phone, restricted under the Places
API ToS) to "pro" (id + name only). A Pro-tier result is name-only until Stage
2.5 (resolve_domains.py) independently proves a domain against the business's
own site. Without resolve_domains enabled, a fixture that merely declares a
places source (the common case — nothing in au-trades/sourcing.json sets
"tier") would source names that never become usable candidates: a silent
zero-yield failure that would look identical to "the campaign has no domains,"
not "a config flag is missing." resolve_sourcing() now auto-enables
resolve_domains whenever any places source isn't explicitly pinned to
"enterprise", and leaves the fixture's own resolve_domains value alone
whenever it's stated explicitly.
"""
import json
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import run_fire


def write_sourcing(cfg: dict) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "sourcing.json").write_text(json.dumps(cfg))
    return d


def test_bare_places_source_auto_enables_resolve_domains():
    run = write_sourcing({
        "sources": [{"type": "places",
                     "targets": [{"included_type": "plumber", "vertical": "trades"}]}]
    })
    cfg = run_fire.resolve_sourcing(run)
    assert cfg["resolve_domains"] is True


def test_explicit_enterprise_tier_does_not_auto_enable():
    """A fixture that deliberately opts into Enterprise (gets websiteUri directly)
    has no name-only results to resolve, so nothing should be silently turned on
    behind the fixture author's back."""
    run = write_sourcing({
        "sources": [{"type": "places", "tier": "enterprise",
                     "targets": [{"included_type": "plumber", "vertical": "trades"}]}]
    })
    cfg = run_fire.resolve_sourcing(run)
    assert cfg.get("resolve_domains") is not True


def test_explicit_resolve_domains_false_is_respected():
    """An explicit opt-out is a real decision and must survive the auto-enable."""
    run = write_sourcing({
        "sources": [{"type": "places",
                     "targets": [{"included_type": "plumber", "vertical": "trades"}]}],
        "resolve_domains": False,
    })
    cfg = run_fire.resolve_sourcing(run)
    assert cfg["resolve_domains"] is False


def test_explicit_resolve_domains_true_is_preserved():
    run = write_sourcing({
        "sources": [{"type": "places",
                     "targets": [{"included_type": "plumber", "vertical": "trades"}]}],
        "resolve_domains": True,
    })
    cfg = run_fire.resolve_sourcing(run)
    assert cfg["resolve_domains"] is True


def test_map_only_fixture_is_unaffected():
    run = write_sourcing({"selector": '"craft"="plumber"', "vertical": "trades"})
    cfg = run_fire.resolve_sourcing(run)
    assert cfg.get("resolve_domains") is not True


def test_mixed_map_and_places_sources_still_auto_enables():
    """au-trades' actual shape: a map source plus a places source. The places
    source alone should be enough to trigger the auto-enable."""
    run = write_sourcing({
        "sources": [
            {"type": "map", "targets": [{"selector": '"craft"="plumber"', "vertical": "trades"}]},
            {"type": "places", "targets": [{"included_type": "plumber", "vertical": "trades"}]},
        ]
    })
    cfg = run_fire.resolve_sourcing(run)
    assert cfg["resolve_domains"] is True
