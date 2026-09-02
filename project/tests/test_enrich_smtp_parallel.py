"""The SMTP rescue in phase_merge runs its probes from a thread pool.

Measured on 2026-09-02-gcc-receptionist: 71 probes, serial, inside the
per-lead loop, 14.7 minutes. The restructure collects the probe candidates in a
first pass, runs them through a ThreadPoolExecutor (SMTP_WORKERS, default 16),
then applies the UNCHANGED per-lead gates. These tests pin the contract:

  * the set of leads probed, the survivors and every counter are identical
    whether the pool has 1 worker (the old serial order) or 8
  * the probes actually overlap when workers > 1
  * a verified hit still faces is_direct_email (a role mailbox stays dropped)
  * SMTP_PROBE=0 skips every probe, counters read 0, and the dropped rows carry
    smtp_probe_note "skipped: port 25 unreachable"

probe_person is monkeypatched — no network. enrich_contact_person parses argv
at import, hence the sys.argv dance (same as test_enrich_name_recovery.py).
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

sys.argv = ["enrich_contact_person.py", "--phase", "merge", "--run-dir", str(PROJECT)]
import enrich_contact_person as ecp  # noqa: E402

pytestmark = pytest.mark.skipif(ecp.smtp_probe is None, reason="smtp_email_probe unavailable (dnspython)")


def _lead(slug, **over):
    d = {"lead_id": f"web-{slug}", "name": slug.replace("-", " ").title(),
         "website": f"https://{slug}.test", "country_code": "AE", "vertical": "hotel",
         "to_email": f"info@{slug}.test", "email_class": "role", "score": 90}
    d.update(over)
    return d


LEADS = [
    _lead("alpha", score=99),   # structured name, probe -> verified direct address
    _lead("bravo", score=98),   # name only in prose, probe -> verified
    _lead("charlie", score=97), # probe -> catch_all, no site format -> stays dropped
    _lead("delta", score=96),   # probe -> not_found
    _lead("echo", score=95),    # found:false with NO name -> never probed
    _lead("foxtrot", score=94), # found:true with a direct email -> no probe needed
    _lead("golf", score=93),    # probe "verifies" gm@ -> role mailbox, gate must still drop it
]

AGENT_OUT = {
    "web-alpha": {"found": False, "first_name": "Anna", "last_name": "Muster",
                  "reason": "name found but no direct email"},
    "web-bravo": {"found": False, "reason": "name found (Uwe Schramm, Hotel Director) but no direct personal email"},
    "web-charlie": {"found": False, "first_name": "Carla", "last_name": "Rossi", "reason": "no direct email"},
    "web-delta": {"found": False, "first_name": "Dan", "last_name": "Field", "reason": "no direct email"},
    "web-echo": {"found": False, "reason": "no named decision-maker found in site pages"},
    "web-foxtrot": {"found": True, "first_name": "Fay", "last_name": "Trott", "title": "Mrs.",
                    "email": "fay.trott@foxtrot.test", "email_basis": "verbatim",
                    "email_source_url": "https://foxtrot.test/team", "source_url": "https://foxtrot.test/team",
                    "confidence": "high"},
    "web-golf": {"found": False, "first_name": "Gary", "last_name": "Moss", "reason": "no direct email"},
}

FAKE_PROBE = {
    "alpha.test": {"email": "anna.muster@alpha.test", "status": "verified", "confidence": "high", "method": "smtp", "note": "accepted"},
    "bravo.test": {"email": "uwe.schramm@bravo.test", "status": "verified", "confidence": "high", "method": "smtp", "note": "accepted"},
    "charlie.test": {"email": "carla.rossi@charlie.test", "status": "catch_all", "confidence": "medium", "method": "pattern", "note": "accepts all"},
    "delta.test": {"email": "", "status": "not_found", "confidence": "none", "method": "smtp", "note": "rejected every candidate"},
    "golf.test": {"email": "gm@golf.test", "status": "verified", "confidence": "high", "method": "smtp", "note": "accepted"},
}
EXPECTED_PROBED = {("Anna", "Muster", "alpha.test"), ("Uwe", "Schramm", "bravo.test"),
                   ("Carla", "Rossi", "charlie.test"), ("Dan", "Field", "delta.test"),
                   ("Gary", "Moss", "golf.test")}


class FakeProbe:
    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls: list[tuple[str, str, str]] = []
        self.threads: set[str] = set()
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, first, last, domain, mail_from, *a, **kw):
        with self._lock:
            self.calls.append((first, last, domain))
            self.threads.add(threading.current_thread().name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            return dict(FAKE_PROBE[domain])
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A merge-ready run dir wired into the imported module (no argv re-parse)."""
    run = tmp_path / "run"
    work = tmp_path / "work"
    run.mkdir()
    work.mkdir()
    (run / "leads-qualified.json").write_text("\n".join(json.dumps(l) for l in LEADS) + "\n")
    for i, (lid, out) in enumerate(AGENT_OUT.items(), 1):
        (work / f"enrich-batch-{i:03d}.txt").write_text(f"LeadId: {lid}\n")
        (work / f"enrich-out-{i:03d}.json").write_text(json.dumps({"lead_id": lid, **out}))
    monkeypatch.setattr(ecp, "ROOT", run)
    monkeypatch.setattr(ecp, "WORK", work)
    monkeypatch.setattr(ecp, "EXTRACTED", run / "leads-qualified.json")
    monkeypatch.setattr(ecp, "WITH_CONTACT", run / "leads-with-contact.json")
    monkeypatch.setattr(ecp, "DERIVED", run / "derived-contacts.json")
    monkeypatch.setattr(ecp, "domain_accepts_mail", lambda d: True)   # no DNS in tests
    monkeypatch.chdir(tmp_path)                                        # relative ledger path lands here
    ecp._site_email_format.cache_clear()
    ecp._site_has_person_format.cache_clear()
    return run


def _merge(run, monkeypatch, fake, workers):
    monkeypatch.setattr(ecp.smtp_probe, "probe_person", fake)
    monkeypatch.setenv("SMTP_WORKERS", str(workers))
    monkeypatch.delenv("SMTP_PROBE", raising=False)
    # Fresh ledger per merge: it is append-once, so a second merge in the same
    # tmp dir would report retired_unreachable=0 purely from dedup.
    ledger = Path("vault/lead-outreach/disqualified-log.txt")
    ledger.unlink(missing_ok=True)
    ecp.phase_merge()
    survivors = [json.loads(l) for l in (run / "leads-with-contact.json").read_text().splitlines() if l.strip()]
    dropped = [json.loads(l) for l in (run / "leads-dropped.json").read_text().splitlines() if l.strip()]
    summary = json.loads((run / "enrich-summary.json").read_text())
    return survivors, dropped, summary


def test_parallel_pool_matches_the_serial_result_exactly(run, monkeypatch):
    serial = FakeProbe()
    s_surv, s_drop, s_sum = _merge(run, monkeypatch, serial, workers=1)
    parallel = FakeProbe(delay=0.2)
    t0 = time.monotonic()
    p_surv, p_drop, p_sum = _merge(run, monkeypatch, parallel, workers=8)
    elapsed = time.monotonic() - t0

    # Same leads probed, regardless of scheduling.
    assert set(serial.calls) == EXPECTED_PROBED
    assert set(parallel.calls) == EXPECTED_PROBED
    # Same survivors, same drop ledger, same counters.
    assert [s["lead_id"] for s in p_surv] == [s["lead_id"] for s in s_surv]
    assert [(d["lead_id"], d["drop_stage"]) for d in p_drop] == [(d["lead_id"], d["drop_stage"]) for d in s_drop]
    assert p_sum == s_sum
    # And the expected values, spelled out: the same numbers the old serial loop produced.
    assert [s["lead_id"] for s in p_surv] == ["web-alpha", "web-bravo", "web-foxtrot"]
    assert p_sum["smtp_probed"] == 5
    assert p_sum["smtp_rescued"] == 3          # alpha, bravo, golf (golf dies later at the email gate)
    assert p_sum["catchall_built_from_site_format"] == 0
    assert p_sum["dropped_no_match"] == 3      # charlie, delta, echo
    assert p_sum["dropped_no_direct_email"] == 1   # golf: gm@ is a role mailbox
    assert p_sum["survived"] == 3
    by_id = {s["lead_id"]: s for s in p_surv}
    assert by_id["web-alpha"]["contact_email_basis"] == "smtp_verified"
    assert by_id["web-bravo"]["contact_first_name"] == "Uwe"      # prose-recovered name kept
    # The probes overlapped: 5 x 0.2 s serial would be >= 1.0 s.
    assert parallel.max_in_flight > 1, "probes never overlapped"
    assert elapsed < 0.9, f"parallel merge took {elapsed:.2f}s — probes ran serially"
    assert serial.max_in_flight == 1


def test_probe_notes_survive_into_the_drop_ledger(run, monkeypatch):
    _, dropped, _ = _merge(run, monkeypatch, FakeProbe(), workers=4)
    notes = {d["lead_id"]: d["smtp_probe_note"] for d in dropped}
    assert notes["web-charlie"].startswith("catch-all domain")
    assert "not_found" in notes["web-delta"]
    assert notes["web-echo"] == ""            # never probed: no name to probe with


def test_smtp_probe_0_skips_every_probe(run, monkeypatch):
    fake = FakeProbe()
    monkeypatch.setattr(ecp.smtp_probe, "probe_person", fake)
    monkeypatch.setenv("SMTP_PROBE", "0")
    ecp.phase_merge()
    survivors = [json.loads(l) for l in (run / "leads-with-contact.json").read_text().splitlines() if l.strip()]
    dropped = [json.loads(l) for l in (run / "leads-dropped.json").read_text().splitlines() if l.strip()]
    summary = json.loads((run / "enrich-summary.json").read_text())

    assert fake.calls == []
    assert summary["smtp_probed"] == 0
    assert summary["smtp_rescued"] == 0
    assert summary["catchall_built_from_site_format"] == 0
    assert [s["lead_id"] for s in survivors] == ["web-foxtrot"]
    notes = {d["lead_id"]: d["smtp_probe_note"] for d in dropped}
    for lid in ("web-alpha", "web-bravo", "web-charlie", "web-delta", "web-golf"):
        assert notes[lid] == "skipped: port 25 unreachable", (lid, notes[lid])
    assert notes["web-echo"] == ""            # would not have been probed anyway
