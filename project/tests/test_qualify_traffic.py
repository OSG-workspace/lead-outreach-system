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
    """Within one traffic tier, the email-quality score still orders leads."""
    leads = [
        _lead("weak-com", traffic_tier="high", traffic_score=60, score=82),
        _lead("strong-com", traffic_tier="high", traffic_score=60, score=96),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["strong-com", "weak-com"]


def test_email_quality_outranks_traffic(tmp_path):
    """THE core contract: traffic is a TIE-BREAK, never an override.

    A quiet business with a findable decision-maker email must still beat a busy
    business with a weak one. Calibration could not show that busy businesses
    enrich BETTER, and the plausible mechanism runs the other way (big orgs
    publish only info@/reception@, and email-availability failures are already
    77% of all enrichment drops). So traffic must never push a hard-to-reach
    lead ahead of a reachable one.
    """
    leads = [
        _lead("busy-weak-com", traffic_tier="high", traffic_score=95, score=82),
        _lead("quiet-strong-com", traffic_tier="low", traffic_score=0, score=96),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["quiet-strong-com", "busy-weak-com"]


def test_email_class_outranks_traffic(tmp_path):
    """Same contract at the email-CLASS key: person beats role regardless of traffic."""
    leads = [
        _lead("busy-role-com", traffic_tier="high", traffic_score=95,
              score=90, email_class="role"),
        _lead("quiet-person-com", traffic_tier="low", traffic_score=0,
              score=90, email_class="person"),
    ]
    _, kept, _ = _run(tmp_path, leads)
    assert [l["lead_slug"] for l in kept] == ["quiet-person-com", "busy-role-com"]


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
