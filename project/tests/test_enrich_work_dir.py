"""--work-dir on enrich_contact_person.py (prep + merge).

WHY: a batch file under project/runs/<slug>/ makes Claude Code attach
project/CLAUDE.md (~7.5k tokens) to every name-finder agent — 664/665 agent
transcripts on 2026-09-02 carried it. run_fire now hands the stage a work dir
outside project/ for the per-agent intermediates. The contract under test:

  * absent            -> everything in the run dir, byte-for-byte the old layout
  * --work-dir <dir>  -> enrich-batch-NNN.txt / enrich-out-NNN.json live there,
                         the OutputFile: line inside each batch points there,
                         the dir is created if missing, and every RUN-level
                         artifact (derived-contacts.json, leads-with-contact.json,
                         leads-dropped.json, enrich-summary.json) stays in the run
                         dir. raw_html is read from the run dir in both cases.

The script parses argv at import, so each case runs it as a subprocess. PATH is
emptied so `dig` is not found and the DNS gate passes open (no network in
tests); no lead needs an SMTP probe because every agent output is found:true.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
SCRIPT = PROJECT / "tools" / "scripts" / "enrich_contact_person.py"

LEADS = [
    {"lead_id": "web-acme-test", "name": "Acme Hotel", "website": "https://acme.test",
     "country_code": "AE", "vertical": "hotel", "to_email": "info@acme.test",
     "email_class": "role", "score": 90},
    {"lead_id": "web-beta-test", "name": "Beta Clinic", "website": "https://beta.test",
     "country_code": "SA", "vertical": "clinic", "to_email": "info@beta.test",
     "email_class": "role", "score": 88},
]


def _page(raw_dir: Path, domain: str) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / f"{domain}__contact.html").write_text(
        f"<html><title>{domain}</title><body>Contact info@{domain} {'.' * 300}</body></html>")


def _make_run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "leads-extracted.json").write_text("\n".join(json.dumps(l) for l in LEADS) + "\n")
    for l in LEADS:
        _page(run / "raw_html", l["website"].removeprefix("https://"))
    (tmp_path / "vault" / "lead-outreach").mkdir(parents=True)
    return run


def _env(tmp_path: Path) -> dict:
    nopath = tmp_path / "nopath"
    nopath.mkdir(exist_ok=True)
    return {**os.environ, "PATH": str(nopath),          # no `dig` -> DNS gate passes open
            "DERIVE_NAMES_FROM_EMAIL": "0",             # every lead gets a batch
            "SMTP_PROBE": "0"}                          # belt and braces: never probe in tests


def _run(phase: str, run: Path, tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    r = subprocess.run([sys.executable, str(SCRIPT), "--phase", phase, "--run-dir", str(run), *extra],
                       capture_output=True, text=True, cwd=str(tmp_path), env=_env(tmp_path))
    assert r.returncode == 0, f"{phase} failed rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


def _agent_writes_outputs(batch_dir: Path) -> None:
    """Stand in for the name-finder agents: honour the OutputFile: line."""
    for b in sorted(batch_dir.glob("enrich-batch-*.txt")):
        fields = dict(line.split(": ", 1) for line in b.read_text().splitlines()
                      if ": " in line and not line.startswith("SitePages"))
        out = Path(fields["OutputFile"].strip())
        dom = fields["Website"].strip().removeprefix("https://")
        out.write_text(json.dumps({
            "lead_id": fields["LeadId"].strip(), "found": True,
            "first_name": "Sarah", "last_name": "Jones", "title": "Mrs.",
            "role": "Owner", "email": f"sarah.jones@{dom}", "email_basis": "verbatim",
            "email_source_url": fields["Website"].strip(),
            "source_url": fields["Website"].strip(), "confidence": "high", "reason": "test"}))


def test_default_layout_is_unchanged_without_work_dir(tmp_path):
    run = _make_run(tmp_path)
    _run("prep", run, tmp_path)
    batches = sorted(run.glob("enrich-batch-*.txt"))
    assert [b.name for b in batches] == ["enrich-batch-001.txt", "enrich-batch-002.txt"]
    for b in batches:
        out_line = next(l for l in b.read_text().splitlines() if l.startswith("OutputFile: "))
        assert Path(out_line.removeprefix("OutputFile: ")).parent == run
    _agent_writes_outputs(run)
    _run("merge", run, tmp_path)
    assert (run / "leads-with-contact.json").exists()
    assert (run / "enrich-summary.json").exists()
    assert json.loads((run / "enrich-summary.json").read_text())["batches"] == 2


def test_work_dir_holds_only_the_per_agent_intermediates(tmp_path):
    run = _make_run(tmp_path)
    work = tmp_path / "fire-work" / "slug"            # does not exist yet: prep must create it
    _run("prep", run, tmp_path, "--work-dir", str(work))

    assert work.is_dir()
    batches = sorted(work.glob("enrich-batch-*.txt"))
    assert [b.name for b in batches] == ["enrich-batch-001.txt", "enrich-batch-002.txt"]
    assert not list(run.glob("enrich-batch-*.txt")), "batches must not also land in the run dir"
    for b in batches:
        out_line = next(l for l in b.read_text().splitlines() if l.startswith("OutputFile: "))
        out_path = Path(out_line.removeprefix("OutputFile: "))
        assert out_path.parent == work.resolve(), out_line
        assert out_path.name.startswith("enrich-out-")
        # raw_html came from the RUN dir: the batch carries the scraped page text.
        assert "SitePages" in b.read_text()

    _agent_writes_outputs(work)
    assert len(list(work.glob("enrich-out-*.json"))) == 2
    r = _run("merge", run, tmp_path, "--work-dir", str(work))

    # Run-level artifacts stay in the run dir …
    survivors = [json.loads(l) for l in (run / "leads-with-contact.json").read_text().splitlines() if l.strip()]
    assert {s["lead_id"] for s in survivors} == {"web-acme-test", "web-beta-test"}
    assert (run / "enrich-summary.json").exists()
    assert (run / "leads-dropped.json").exists()
    summary = json.loads((run / "enrich-summary.json").read_text())
    assert summary["batches"] == 2 and summary["agent_outputs"] == 2, summary
    # … and nothing run-level leaks into the work dir.
    assert sorted(p.name for p in work.iterdir()) == [
        "enrich-batch-001.txt", "enrich-batch-002.txt",
        "enrich-out-001.json", "enrich-out-002.json"]
    assert "survived (name + Mr./Mrs. + direct email): 2" in r.stdout


def test_merge_with_work_dir_ignores_stale_files_in_the_run_dir(tmp_path):
    """The fan-out completion guard must count the work dir, not leftovers in
    the run dir from an older fire that predates --work-dir."""
    run = _make_run(tmp_path)
    work = tmp_path / "work"
    _run("prep", run, tmp_path, "--work-dir", str(work))
    _agent_writes_outputs(work)
    # Stale batches in the run dir with NO outputs: counted there, they would
    # read as a 2/4 = 50% fan-out and trip the 60% abort.
    (run / "enrich-batch-001.txt").write_text("stale\n")
    (run / "enrich-batch-002.txt").write_text("stale\n")
    _run("merge", run, tmp_path, "--work-dir", str(work))
    assert json.loads((run / "enrich-summary.json").read_text())["batches"] == 2


def test_prep_still_refuses_without_raw_html_in_the_run_dir(tmp_path):
    """The raw_html guard is a RUN-dir check; a work dir must not satisfy or
    relocate it."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "leads-extracted.json").write_text("\n".join(json.dumps(l) for l in LEADS) + "\n")
    work = tmp_path / "work"
    (work / "raw_html").mkdir(parents=True)
    (work / "raw_html" / "acme.test__index.html").write_text("<html>" + "." * 300)
    r = subprocess.run([sys.executable, str(SCRIPT), "--phase", "prep", "--run-dir", str(run),
                        "--work-dir", str(work)],
                       capture_output=True, text=True, cwd=str(tmp_path), env=_env(tmp_path))
    assert r.returncode != 0
    assert "ABORT: Stage 5.5 prep" in (r.stdout + r.stderr)
    assert str(run / "raw_html") in (r.stdout + r.stderr)
