"""The enrichment cap default: 250 -> 400 (2026-09-02).

Measured across 18 runs, the 250 cap discarded 349 leads scoring >= 82 — 46% of
everything the qualify stage removed, more than the score floor did (335).
ENRICH_MAX_LEADS and --cap (which run_fire derives from enrich_cap.txt or a
--target-leads need) must keep overriding it. Subprocess-driven, like
test_qualify_traffic.py, because the script parses argv at import.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
SCRIPT = PROJECT / "tools" / "scripts" / "qualify_leads.py"


def _lead(i):
    return {"lead_id": f"web-l{i}", "lead_slug": f"l{i}", "name": f"L {i}",
            "website": f"https://l{i}.test", "country_code": "ES", "vertical": "hotel",
            "to_email": f"owner@l{i}.test", "email_class": "person", "signal": "phone_led",
            "hotel_volume": "na", "traffic_tier": "medium", "traffic_score": 40,
            "score": 90, "branches_estimate": 0}


def _run(tmp_path, n, env=None, extra=()):
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    (run / "leads-extracted.json").write_text("\n".join(json.dumps(_lead(i)) for i in range(n)) + "\n")
    (tmp_path / "vault" / "lead-outreach").mkdir(parents=True, exist_ok=True)
    e = {**os.environ}
    e.pop("ENRICH_MAX_LEADS", None)
    e.update(env or {})
    r = subprocess.run([sys.executable, str(SCRIPT), "--run-dir", str(run), *extra],
                       capture_output=True, text=True, cwd=str(tmp_path), env=e)
    assert r.returncode == 0, r.stdout + r.stderr
    kept = [l for l in (run / "leads-qualified.json").read_text().splitlines() if l.strip()]
    return r.stdout, len(kept)


def test_default_cap_is_400(tmp_path):
    out, kept = _run(tmp_path, 405)
    assert kept == 400
    assert "CAPPED OFF (top-400 kept): 5" in out


def test_399_qualified_leads_are_not_capped(tmp_path):
    out, kept = _run(tmp_path, 399)
    assert kept == 399
    assert "CAPPED OFF" not in out


def test_env_override_still_wins(tmp_path):
    _, kept = _run(tmp_path, 50, env={"ENRICH_MAX_LEADS": "7"})
    assert kept == 7


def test_explicit_cap_flag_still_wins(tmp_path):
    _, kept = _run(tmp_path, 50, env={"ENRICH_MAX_LEADS": "7"}, extra=("--cap", "3"))
    assert kept == 3


def test_help_text_names_the_new_default():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert "default 400" in r.stdout
