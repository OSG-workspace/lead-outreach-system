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
    # Filenames must match collect_html_for_domain's glob, which preserves
    # dots in the domain (only "/" is replaced) — see fetch_html.py.
    (raw / "busy.com__index.html").write_text(BUSY)
    (raw / "quiet.com__index.html").write_text(QUIET)
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
    (raw / "busy.com__index.html").write_text(BUSY)
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
