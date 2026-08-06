"""Regression tests for the LinkedIn arm's Python stages.

Each test here pins a bug that silently LOST a lead rather than failing loudly,
which is the failure mode this channel is prone to: the invite drip is so slow
that a person quietly vanishing between two stages looks identical to a slow
week.
"""
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "tools" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from li_url import canonical_profile_url  # noqa: E402


def _run(script, *args):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          capture_output=True, text=True)


# --- the canonical URL, shared by both person-routes -------------------------

def test_canonical_url_collapses_every_spelling_of_one_profile():
    forms = [
        "https://www.linkedin.com/in/sara-khoury-2/",
        "https://www.linkedin.com/in/sara-khoury-2",
        "https://ae.linkedin.com/in/sara-khoury-2",
        "http://linkedin.com/in/Sara-Khoury-2/",
        "https://www.linkedin.com/in/sara-khoury-2?trk=public_profile",
        "linkedin.com/in/sara-khoury-2",
    ]
    got = {canonical_profile_url(f) for f in forms}
    assert got == {"https://www.linkedin.com/in/sara-khoury-2/"}, got


def test_canonical_url_rejects_non_profiles():
    # These are exactly what an agent returns when it could not find a person.
    for bad in ["https://www.linkedin.com/company/alpha-realty/", "",
                "https://example.com/in/someone", "https://www.linkedin.com/feed/",
                None, "not a url"]:
        assert canonical_profile_url(bad) is None, bad


# --- draft_linkedin.py merge -------------------------------------------------

def _seed(tmp_path, people):
    (tmp_path / "people-qualified.json").write_text(json.dumps(people))
    (tmp_path / "li-out").mkdir(exist_ok=True)
    return tmp_path


def test_merge_matches_despite_trailing_slash_drift(tmp_path):
    """li-writer is a model echoing a URL back; the slash drifts.

    Before this, the merge did an exact string compare, so one differing
    character dropped a message that had already been written — and the lead was
    then rejected downstream as "no follow-up DM composed", reporting a gate
    where there was really a lost artifact.
    """
    run = _seed(tmp_path, [
        {"full_name": "Sara", "profile_url": "https://www.linkedin.com/in/sara-2/"},
        {"full_name": "Faisal", "profile_url": "https://www.linkedin.com/in/faisal-9"},
    ])
    (run / "li-out" / "001.json").write_text(json.dumps(
        {"profile_url": "https://www.linkedin.com/in/sara-2", "message": "hello sara"}))
    (run / "li-out" / "002.json").write_text(json.dumps(
        {"profile_url": "https://ae.linkedin.com/in/faisal-9/", "message": "hello faisal"}))

    r = _run("draft_linkedin.py", "--phase", "merge", "--run-dir", str(run))
    assert r.returncode == 0, r.stderr
    out = json.loads((run / "people-with-notes.json").read_text())
    assert [p["message"] for p in out] == ["hello sara", "hello faisal"]


def test_merge_falls_back_to_batch_index_when_url_is_unusable(tmp_path):
    """NNN.json maps 1:1 onto people[NNN-1] by construction in batch()."""
    run = _seed(tmp_path, [{"full_name": "Sara",
                            "profile_url": "https://www.linkedin.com/in/sara-2/"}])
    (run / "li-out" / "001.json").write_text(
        json.dumps({"profile_url": "(not found)", "message": "hello sara"}))
    r = _run("draft_linkedin.py", "--phase", "merge", "--run-dir", str(run))
    assert r.returncode == 0, r.stderr
    out = json.loads((run / "people-with-notes.json").read_text())
    assert len(out) == 1 and out[0]["message"] == "hello sara"


def test_merge_reports_who_got_no_message(tmp_path):
    run = _seed(tmp_path, [{"full_name": "Ghost",
                            "profile_url": "https://www.linkedin.com/in/ghost-1/"}])
    r = _run("draft_linkedin.py", "--phase", "merge", "--run-dir", str(run))
    assert r.returncode == 7          # nothing composed; run_fire decides
    assert "Ghost" in r.stdout        # named, not just counted


# --- resolve_li_profiles.py --------------------------------------------------

def test_profile_find_merge_still_rejects_evidence_free_and_fake_profiles(tmp_path):
    (tmp_path / "li-companies.json").write_text(json.dumps(
        [{"name": "Gamma Insurance", "domain": "g.sa", "country": "SA", "city": "Riyadh"}]))
    out = tmp_path / "lif-out"
    out.mkdir()
    # no evidence_url
    (out / "001.json").write_text(json.dumps(
        {"found": True, "company": "Gamma Insurance", "full_name": "A",
         "profile_url": "https://www.linkedin.com/in/a-1"}))
    # a company page, not a person
    (out / "002.json").write_text(json.dumps(
        {"found": True, "company": "Gamma Insurance", "full_name": "B",
         "profile_url": "https://www.linkedin.com/company/gamma/",
         "evidence_url": "https://g.sa/about"}))
    r = _run("resolve_li_profiles.py", "--phase", "merge", "--run-dir", str(tmp_path))
    assert r.returncode == 7
    assert json.loads((tmp_path / "people-found.json").read_text()) == []


# --- linkedin_queue.py: the cross-run guards --------------------------------

def _state(dirpath, profile, company, when, domain=None):
    (dirpath / "state.json").write_text(json.dumps({"leads": {profile: {
        "profileUrl": profile, "company": company, "companyDomain": domain,
        "status": "sent", "sentAt": f"{when}T09:30:00.000Z"}}}))


def _rank(run, state_dir, cfg):
    import os
    env = {**os.environ, "LINKEDIN_STATE_DIR": str(state_dir)}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "linkedin_queue.py"),
         "--run-dir", str(run), "--config", json.dumps(cfg)],
        capture_output=True, text=True, env=env)


def _person(**over):
    base = {"full_name": "A", "title": "Owner", "tier_rank": 3,
            "profile_url": "https://www.linkedin.com/in/a-1/",
            "company": "Alpha Realty", "company_domain": "alpharealty.ae",
            "company_headcount": 4, "company_country": "AE",
            "reach_mode": "invite", "last_activity_days": 5, "message": "a real message"}
    return {**base, **over}


def test_small_firm_window_holds_a_second_person_across_runs(tmp_path):
    """The window used to be dead: its ledger was read on every run and written
    on none, so two fires a week apart both invited into the same tiny firm."""
    run = tmp_path / "run"; run.mkdir()
    sd = tmp_path / "state"; sd.mkdir()
    (run / "people-with-notes.json").write_text(json.dumps([
        _person(full_name="Layla", profile_url="https://www.linkedin.com/in/layla-4/")]))
    # A previous fire already spent an invite at this company — recorded, as the
    # sender records it, under the NAME only.
    _state(sd, "https://www.linkedin.com/in/ahmed-1/", "Alpha Realty",
           str(__import__("datetime").date.today()))

    r = _rank(run, sd, {"small_firm_headcount": 6, "coordination_window_days": 14})
    assert r.returncode == 0, r.stderr
    assert json.loads((run / "linkedin-leads.json").read_text()) == []
    carry = json.loads((run / "invite-carry.json").read_text())
    assert len(carry) == 1 and "small firm" in carry[0]["carry_reason"]


def test_a_person_already_invited_is_suppressed_and_pruned_from_the_backlog(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    sd = tmp_path / "state"; sd.mkdir()
    url = "https://www.linkedin.com/in/a-1/"
    (run / "people-with-notes.json").write_text(json.dumps([_person()]))
    (sd / "backlog.json").write_text(json.dumps([{"linkedinUrl": url, "score": 9}]))
    _state(sd, url, "Alpha Realty", "2026-01-01")

    r = _rank(run, sd, {})
    assert r.returncode == 0, r.stderr
    assert json.loads((run / "linkedin-leads.json").read_text()) == []
    assert json.loads((sd / "backlog.json").read_text()) == []   # pruned, not left to inflate the ETA


def test_a_large_firm_is_not_subject_to_the_one_person_window(tmp_path):
    run = tmp_path / "run"; run.mkdir()
    sd = tmp_path / "state"; sd.mkdir()
    (run / "people-with-notes.json").write_text(json.dumps([
        _person(full_name="Layla", profile_url="https://www.linkedin.com/in/layla-4/",
                company_headcount=400)]))
    _state(sd, "https://www.linkedin.com/in/ahmed-1/", "Alpha Realty",
           str(__import__("datetime").date.today()))
    r = _rank(run, sd, {"small_firm_headcount": 6})
    assert r.returncode == 0, r.stderr
    assert len(json.loads((run / "linkedin-leads.json").read_text())) == 1
