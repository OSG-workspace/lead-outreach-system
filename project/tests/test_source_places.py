"""Tests for source_places.py city-ledger keying.

Regression context (2026-08-03): the Places city ledger was keyed `places:<vertical>`.
Every au-trades places target shares vertical "trades", so the first target
(plumber) ledgered Sydney and the remaining targets (electrician, locksmith,
roofing_contractor) then saw Sydney as already swept — a four-target source would
really have swept only its first target. The key now includes the included_type.

It must also stay namespaced away from the OSM sweep: `trades` is fully burned in
the shared ledger for the map source, and Places must not inherit that.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from source_places import places_ledger_key, resolve_tier, sweep_outcome


def test_targets_sharing_a_vertical_get_distinct_keys():
    keys = {places_ledger_key("trades", t)
            for t in ("plumber", "electrician", "locksmith", "roofing_contractor")}
    assert len(keys) == 4


def test_key_is_namespaced_away_from_the_osm_sweep():
    """The map source ledgers plain `trades`; Places must never collide with it."""
    assert places_ledger_key("trades", "plumber") != "trades"
    assert places_ledger_key("trades", "plumber").startswith("places:")


def test_same_target_is_stable_across_calls():
    assert places_ledger_key("trades", "plumber") == places_ledger_key("trades", "plumber")


def test_vertical_still_separates_campaigns():
    assert places_ledger_key("trades", "plumber") != places_ledger_key("clinics", "plumber")


def test_key_survives_the_ledger_line_format():
    """Rows are `key|city|date|run`, so the key must not contain a pipe."""
    assert "|" not in places_ledger_key("trades", "roofing_contractor")


class TestResolveTier:
    """Regression context (2026-08-03): source_places.py defaulted to "enterprise",
    which requests websiteUri + nationalPhoneNumber and persists them into
    candidates-batch files -> leads-qualified.json -> the vault ledgers forever.
    Independent research into the Places API (New) ToS found only the place ID
    is storage-eligible indefinitely; website/phone are not. The default flipped
    to "pro" (id + displayName only), routing name-only results through Stage 2.5
    (resolve_domains.py) to independently PROVE a domain instead of trusting
    Google's returned field. "enterprise" survives as an explicit per-source
    opt-in, never a silent default."""

    def test_default_is_pro_not_enterprise(self):
        assert resolve_tier("", None) == "pro"
        assert resolve_tier("", "") == "pro"

    def test_source_level_enterprise_is_honored_when_explicit(self):
        assert resolve_tier("", "enterprise") == "enterprise"

    def test_cli_flag_overrides_the_source_config(self):
        assert resolve_tier("pro", "enterprise") == "pro"
        assert resolve_tier("enterprise", "pro") == "enterprise"


class TestSweepOutcome:
    """Regression context (2026-08-03, caught in review before shipping): when the
    default tier flipped to Pro, the end-of-sweep check still judged yield by
    `total` alone. `total` counts only rows that already carry a domain, and Pro
    tier never requests websiteUri — so `total` is ALWAYS 0 on the new default.
    A fully successful Pro sweep producing hundreds of name-only rows would have
    hit the anomalous-halt branch and killed the run with "Google Places returned
    0 businesses". Yield must count name-only rows too, since Stage 2.5 is what
    turns them into candidates."""

    def test_pro_tier_success_is_not_an_abort(self):
        """The bug this class exists for: 0 domains + many name-only = success."""
        code, _ = sweep_outcome(total=0, total_unresolved=250, cities_swept=16, skipped=0)
        assert code == 0

    def test_enterprise_tier_success(self):
        code, _ = sweep_outcome(total=120, total_unresolved=30, cities_swept=16, skipped=0)
        assert code == 0

    def test_no_ground_left_is_exit_8_not_a_hard_abort(self):
        """Every city already ledgered -> other sources and the backlog carry on."""
        code, msg = sweep_outcome(total=0, total_unresolved=0, cities_swept=0, skipped=0)
        assert code == 8
        assert "NO FRESH GROUND" in msg

    def test_all_results_already_seen_is_exit_8(self):
        code, msg = sweep_outcome(total=0, total_unresolved=0, cities_swept=16, skipped=42)
        assert code == 8
        assert "already sourced or contacted" in msg

    def test_swept_real_ground_but_api_gave_nothing_halts_loudly(self):
        """Must NOT be laundered as 'no fresh ground' — it means a broken key,
        an invalid included_type, or a failing API."""
        code, msg = sweep_outcome(total=0, total_unresolved=0, cities_swept=16, skipped=0)
        assert code == 1
        assert "ABORT" in msg

    def test_a_single_name_only_row_is_still_yield(self):
        assert sweep_outcome(0, 1, 16, 0)[0] == 0
