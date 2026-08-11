"""A live send with no BREVO_SENDER_ADDRESS must warn, loudly and every time.

The network is never touched here: `post_batch` is replaced with a stub, so
these tests exercise the warning and its gating without sending an email.
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import send_batch_brevo as sbb

DRAFT = {
    "lead_id": "L1",
    "lead_slug": "acme",
    "to_email": "owner@acme-test-example.com",
    "to_name": "Mr. Smith",
    "subject": "Quick question",
    "body_text": "Hello Mr. Smith,\n\nA note.\n\nDavid",
    "body_html": "<p>Hello Mr. Smith,</p>",
}


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A run dir under a fake project root, so load_suppression finds no vault
    and cannot suppress the draft."""
    project = tmp_path / "project"
    rd = project / "runs" / "2026-08-11-test"
    rd.mkdir(parents=True)
    (rd / "emails-drafted.json").write_text(json.dumps(DRAFT) + "\n")
    # main() reads .env relative to run_dir.parent.parent
    (project / ".env").write_text("")
    monkeypatch.setattr(sbb, "post_batch",
                        lambda *a, **k: (True, "", {"messageIds": ["stub-1"]}))
    return rd


def _run(monkeypatch, run_dir, env: dict, send: bool):
    for k in ("BREVO_MCP_TOKEN", "BREVO_SENDER_EMAIL", "BREVO_SENDER_ADDRESS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    argv = ["send_batch_brevo.py", "--run-dir", str(run_dir)]
    if send:
        argv.append("--send")
    monkeypatch.setattr(sys, "argv", argv)
    sbb.main()


LIVE_ENV = {"BREVO_MCP_TOKEN": "xkeysib-stub", "BREVO_SENDER_EMAIL": "d@example.com"}


def test_live_send_without_address_warns(monkeypatch, run_dir, capsys):
    _run(monkeypatch, run_dir, LIVE_ENV, send=True)
    err = capsys.readouterr().err
    assert "BREVO_SENDER_ADDRESS" in err
    assert "CAN-SPAM" in err


def test_live_send_with_address_is_silent(monkeypatch, run_dir, capsys):
    _run(monkeypatch, run_dir, {**LIVE_ENV, "BREVO_SENDER_ADDRESS": "12 Rue Verdun, Beirut"},
         send=True)
    assert "BREVO_SENDER_ADDRESS" not in capsys.readouterr().err


def test_dry_run_does_not_warn(monkeypatch, run_dir, capsys):
    """A dry run ships nothing, so there is nothing to be non-compliant about —
    warning there would train the operator to ignore the message."""
    _run(monkeypatch, run_dir, {}, send=False)
    assert "BREVO_SENDER_ADDRESS" not in capsys.readouterr().err


def test_the_address_is_threaded_into_the_rendered_email(monkeypatch, run_dir):
    """The warning is only worth having if the configured value really reaches
    the wire. Capture what post_batch would have been handed."""
    captured = {}

    def spy(api_key, sender, reply_to, versions, tags, dry_run):
        captured["versions"] = versions
        return True, "", {"messageIds": ["stub-1"]}

    monkeypatch.setattr(sbb, "post_batch", spy)
    _run(monkeypatch, run_dir, {**LIVE_ENV, "BREVO_SENDER_ADDRESS": "12 Rue Verdun, Beirut"},
         send=True)
    assert "12 Rue Verdun, Beirut" in captured["versions"][0]["htmlContent"]
