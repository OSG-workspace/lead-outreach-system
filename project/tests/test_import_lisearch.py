"""The li-search -> LinkedIn outreach handoff.

What these pin: a handoff must (1) take only people the channel does not
already hold, so re-running it advances through the pool instead of
re-drafting; (2) honour li-search's own erasure list and the operator's
`withdrawn` mark; (3) apply the same title gate as the walk route; (4) carry
the search snippet through to the li-batch file as a hook; and (5) produce
records that linkedin_queue.py accepts into the backlog with the message on.
"""
import csv
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "tools" / "scripts"


def _run(script, *args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                          capture_output=True, text=True, env=e)


FIELDS = ["n", "batch", "delivered_at", "audience", "linkedin_account", "linkedin_url",
          "full_name", "title", "company", "location", "match", "score", "sources", "status"]


def _li_search(tmp_path: Path, rows, leads, slug="lb-test-owners", brief="test-brief",
               suppressed=(), countries=("LB",)):
    root = tmp_path / "li-search"
    (root / "results" / brief).mkdir(parents=True)
    with (root / "results" / brief / "owners.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for i, r in enumerate(rows, 1):
            w.writerow({**{k: "" for k in FIELDS}, "n": i, "batch": 1, "audience": slug, **r})
    run = root / "runs" / f"2026-09-02-{slug}"
    run.mkdir(parents=True)
    (run / "leads.json").write_text(json.dumps({"leads": leads}))
    (root / "audiences").mkdir()
    (root / "audiences" / f"{slug}.json").write_text(json.dumps({"slug": slug, "countries": list(countries)}))
    if suppressed:
        (root / "suppression.txt").write_text("# never\n" + "\n".join(suppressed) + "\n")
    return root


def _state(tmp_path: Path, state_leads=None, backlog=None):
    d = tmp_path / "li-state"
    d.mkdir()
    if state_leads is not None:
        (d / "state.json").write_text(json.dumps({"leads": state_leads}))
    if backlog is not None:
        (d / "backlog.json").write_text(json.dumps(backlog))
    return d


def _row(acct, name, title, company="Acme", match="", status=""):
    return {"linkedin_account": acct, "linkedin_url": f"https://www.linkedin.com/in/{acct}",
            "full_name": name, "title": title, "company": company, "match": match,
            "score": 5, "sources": "ddgs", "status": status}


def _lead(acct, summary="", country="LB", qualified=True):
    return {"linkedin_account": acct, "linkedin_url": f"https://lb.linkedin.com/in/{acct}",
            "summary": summary, "country": country, "qualified": qualified,
            "sources": ["ddgs"], "eu_flag": False}


def test_handoff_skips_people_the_channel_already_holds_and_honours_erasure(tmp_path):
    rows = [
        _row("sara-k", "Sara K", "Founder"),                       # taken
        _row("omar-h", "Omar H", "Owner"),                         # already invited -> skipped
        _row("lina-z", "Lina Z", "CEO"),                           # waiting in backlog -> skipped
        _row("nour-a", "Nour A", "Managing Director", status="withdrawn"),
        _row("rami-b", "Rami B", "General Manager"),               # suppressed by li-search
        _row("joe-c", "Joe C", "Account Executive"),               # wrong tier
        _row("maya-d", "Maya D", "", match="person=Maya D;via=company;title=Founder"),  # tier via match
    ]
    leads = [_lead(r["linkedin_account"], summary=f"About {r['full_name']}") for r in rows]
    root = _li_search(tmp_path, rows, leads, suppressed=["rami-b"])
    state = _state(tmp_path,
                   state_leads={"https://www.linkedin.com/in/omar-h/": {"status": "sent",
                                                                        "profileUrl": "https://www.linkedin.com/in/omar-h/"}},
                   backlog=[{"linkedinUrl": "https://www.linkedin.com/in/lina-z/", "message": "x"}])
    run = tmp_path / "run"
    r = _run("import_lisearch.py", "--brief", "test-brief", "--li-search-root", str(root),
             "--run-dir", str(run), env={"LINKEDIN_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stdout + r.stderr

    taken = json.loads((run / "people-qualified.json").read_text())
    assert [p["lead_id"] for p in taken] == ["sara-k", "maya-d"], taken
    assert taken[0]["profile_url"] == "https://www.linkedin.com/in/sara-k/"
    assert taken[0]["snippet"] == "About Sara K"
    assert taken[0]["company_country"] == "LB"
    assert taken[0]["reach_mode"] == "invite"
    assert taken[1]["tier_rank"] == 3            # Founder via the match string

    dropped = {d["linkedin_account"]: d["drop_reason"] for d in
               json.loads((run / "people-dropped.json").read_text())}
    assert dropped["omar-h"].startswith("already sent")
    assert dropped["lina-z"] == "already waiting in the backlog"
    assert dropped["nour-a"].startswith("withdrawn")
    assert dropped["rami-b"].startswith("on li-search's suppression")
    assert dropped["joe-c"].startswith("title tier none")
    assert json.loads((run / "channels.json").read_text()) == ["linkedin"]


def test_take_bounds_a_handoff_and_the_next_one_continues(tmp_path):
    rows = [_row(f"p{i}", f"P {i}", "Owner") for i in range(5)]
    root = _li_search(tmp_path, rows, [_lead(r["linkedin_account"]) for r in rows])
    state = _state(tmp_path, backlog=[])
    r = _run("import_lisearch.py", "--brief", "test-brief", "--li-search-root", str(root),
             "--run-dir", str(tmp_path / "r1"), "--take", "2", env={"LINKEDIN_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stdout + r.stderr
    first = [p["lead_id"] for p in json.loads((tmp_path / "r1" / "people-qualified.json").read_text())]
    assert first == ["p0", "p1"]
    # Pretend the channel queued them (what linkedin_queue.py does), then hand off again.
    (state / "backlog.json").write_text(json.dumps(
        [{"linkedinUrl": f"https://www.linkedin.com/in/{a}/"} for a in first]))
    r = _run("import_lisearch.py", "--brief", "test-brief", "--li-search-root", str(root),
             "--run-dir", str(tmp_path / "r2"), "--take", "2", env={"LINKEDIN_STATE_DIR": str(state)})
    assert r.returncode == 0, r.stdout + r.stderr
    second = [p["lead_id"] for p in json.loads((tmp_path / "r2" / "people-qualified.json").read_text())]
    assert second == ["p2", "p3"]


def test_nothing_new_exits_7_not_0(tmp_path):
    rows = [_row("only-one", "Only One", "Owner")]
    root = _li_search(tmp_path, rows, [_lead("only-one")])
    state = _state(tmp_path, backlog=[{"linkedinUrl": "https://www.linkedin.com/in/only-one/"}])
    r = _run("import_lisearch.py", "--brief", "test-brief", "--li-search-root", str(root),
             "--run-dir", str(tmp_path / "r"), env={"LINKEDIN_STATE_DIR": str(state)})
    assert r.returncode == 7, r.stdout + r.stderr


def test_snippet_reaches_the_writer_and_the_message_reaches_the_backlog(tmp_path):
    """The whole rest of the arm, unchanged, must accept what the import wrote."""
    rows = [_row("hala-j", "Hala J", "Founder", company="Special Events")]
    root = _li_search(tmp_path, rows, [_lead("hala-j", summary="Producing weddings across Beirut since 2011")])
    state = _state(tmp_path)
    run = tmp_path / "run"
    env = {"LINKEDIN_STATE_DIR": str(state)}
    assert _run("import_lisearch.py", "--brief", "test-brief", "--li-search-root", str(root),
                "--run-dir", str(run), env=env).returncode == 0

    r = _run("draft_linkedin.py", "--phase", "batch", "--run-dir", str(run))
    assert r.returncode == 0, r.stdout + r.stderr
    batch = (run / "li-batch-001.txt").read_text()
    assert "Producing weddings across Beirut since 2011" in batch
    assert "Person: Hala J" in batch and "Country: LB" in batch

    # Stand in for li-writer.
    (run / "li-out" / "001.json").write_text(json.dumps({
        "profile_url": "https://www.linkedin.com/in/hala-j",
        "message": "Hala, the 2011 note on your page stood out. Would a scoped pilot for your next event be worth ten minutes?",
        "hook_used": "snippet"}))
    r = _run("draft_linkedin.py", "--phase", "merge", "--run-dir", str(run))
    assert r.returncode == 0, r.stdout + r.stderr

    r = _run("linkedin_queue.py", "--run-dir", str(run), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    backlog = json.loads((state / "backlog.json").read_text())
    assert len(backlog) == 1
    assert backlog[0]["linkedinUrl"] == "https://www.linkedin.com/in/hala-j/"
    assert backlog[0]["message"].startswith("Hala, the 2011 note")
    assert backlog[0]["directMessageable"] is False
