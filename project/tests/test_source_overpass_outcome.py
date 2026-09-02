"""Exit-code semantics for a finished Overpass sweep.

Regression context (2026-08-11): the old code was
    if not total: sys.exit("ABORT: 0 usable candidates sourced ...")
and `sys.exit(<str>)` exits **1**. run_fire.py tolerates only **8** for a
Stage 2 source, so exit 1 killed the whole fire.

Two things made that wrong rather than merely strict:

  * `total` counts ONLY website-tagged rows, ignoring the name-only
    `unresolved` rows the sweep had just written for Stage 2.5 to resolve. A
    target returning 300 correctly-named businesses with no `website` tag
    aborted the run and discarded its own output. Measured on
    2026-08-09-au-trades: 50 website-tagged rows vs 1,217 unresolved.
  * "every city already swept" is the ordinary no-fresh-ground end state that
    exit 8 exists to express, and it too was reported as a hard failure.

source_places.py already had the correct logic; source_overpass.py never did.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from source_overpass import sweep_outcome


def test_website_tagged_rows_are_yield():
    code, msg = sweep_outcome(total=42, total_unresolved=0, cities_completed=3, skipped_seen=0)
    assert code == 0
    assert msg == ""


def test_name_only_rows_alone_are_YIELD_not_failure():
    """THE regression. Zero website-tagged rows but 1,217 name-only rows is a
    successful sweep — Stage 2.5 turns those into candidates."""
    code, msg = sweep_outcome(total=0, total_unresolved=1217, cities_completed=16, skipped_seen=0)
    assert code == 0, f"name-only harvest must not abort the run: {msg}"


def test_mixed_yield_is_success():
    code, _ = sweep_outcome(total=50, total_unresolved=1217, cities_completed=16, skipped_seen=0)
    assert code == 0


def test_no_cities_swept_is_no_fresh_ground_not_failure():
    """Exit 8 so a run's OTHER sources still carry it."""
    code, msg = sweep_outcome(total=0, total_unresolved=0, cities_completed=0, skipped_seen=0)
    assert code == 8
    assert "NO FRESH GROUND" in msg


def test_everything_already_seen_is_no_fresh_ground():
    code, msg = sweep_outcome(total=0, total_unresolved=0, cities_completed=5, skipped_seen=120)
    assert code == 8
    assert "already sourced or contacted" in msg


def test_genuinely_empty_sweep_is_dry_not_fatal():
    """Cities were searched and nothing came back. Since 2026-08-11 that is
    exit 8 (no fresh ground), NOT exit 1: the caller cannot tell a malformed
    selector from a sparse vertical, and on 2026-08-11-au-trades an exit 1
    here killed the run before the one source that had ground ever ran. A run
    where EVERY source is dry still halts at run_fire's 0-candidates gate."""
    code, msg = sweep_outcome(total=0, total_unresolved=0, cities_completed=5, skipped_seen=0)
    assert code == 8
    assert "NO FRESH GROUND" in msg


def test_exit_code_is_never_the_untolerated_one_when_there_is_ground():
    """run_fire.py tolerates only 8. Any no-fresh-ground shape must return 8,
    never 1, or one dry source kills a multi-source run."""
    for completed, skipped in ((0, 0), (0, 5), (3, 9), (10, 1)):
        code, _ = sweep_outcome(0, 0, completed, skipped)
        if completed == 0 or skipped:
            assert code == 8, f"completed={completed} skipped={skipped} gave {code}"
