import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "scripts" / "draft_whatsapp_custom.py"


def _run(run_dir, phase):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--phase", phase, "--run-dir", str(run_dir)],
        capture_output=True, text=True,
    )


def _lead(**over):
    base = {
        "lead_id": "web-aishti-com", "lead_slug": "aishti-com", "name": "Aishti",
        "website": "examplebrand.com", "vertical": "retail", "country_code": "LB",
        "score": 90, "contact_first_name": "Tony", "contact_last_name": "Salame",
        "contact_title": "Mr.", "contact_phone": "+96170123456",
        "contact_email": "founder@examplebrand.com",
    }
    base.update(over)
    return base


def _good_body():
    return (
        "Hello Mr. Salame,\n\n"
        "Aishti routes a lot of stock and order questions through your team by hand.\n\n"
        "I'm David Geha, a third-year engineering student at AUB. I work with a team "
        "building custom AI systems for clients across Lebanon, the GCC, and India, and "
        "we'd take that repetitive work off your team. You own the system, no platform "
        "lock-in.\n\n"
        "Worth a short call this week?\n\n"
        "David Geha"
    )


def _setup(tmp_path, leads, outs):
    run = tmp_path / "runs" / "2026-06-22-lb-enterprise"
    run.mkdir(parents=True)
    (run.parent.parent / "vault" / "lead-outreach").mkdir(parents=True)
    (run.parent.parent / "vault" / "lead-outreach" / "sent-log.md").write_text("")
    (run / "leads-with-contact.json").write_text(
        "\n".join(json.dumps(l) for l in leads) + "\n")
    for i, o in enumerate(outs, 1):
        (run / f"wa-out-{i:03d}.json").write_text(json.dumps(o))
    return run


def test_merge_keeps_valid_draft(tmp_path):
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": _good_body()}])
    r = _run(run, "merge")
    assert r.returncode == 0, r.stderr + r.stdout
    rows = [json.loads(x) for x in (run / "whatsapp-drafted.json").read_text().splitlines() if x.strip()]
    assert len(rows) == 1
    d = rows[0]
    assert d["to_jid"] == "96170123456@c.us"
    assert d["salutation"] == "Mr. Salame"
    assert "automatelb" not in d["body_text"].lower()


def test_merge_drops_automate_mention(tmp_path):
    body = _good_body().replace("David Geha", "David Geha\nAutomate, automatelb.com")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5  # zero survive -> abort


def test_merge_drops_money_talk(tmp_path):
    body = _good_body().replace("no platform lock-in.", "no platform lock-in. It is free.")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5


def test_merge_drops_bad_salutation(tmp_path):
    body = _good_body().replace("Hello Mr. Salame,", "Hi there,")
    run = _setup(tmp_path, [_lead()],
                 [{"lead_id": "web-aishti-com", "lead_slug": "aishti-com",
                   "workflow_gaps": "g", "how_we_help": "h", "body_text": body}])
    r = _run(run, "merge")
    assert r.returncode == 5


def test_prep_skips_lead_without_mobile(tmp_path):
    run = _setup(tmp_path, [_lead(contact_phone="")], [])
    (run / "raw_html").mkdir()
    r = _run(run, "prep")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not list(run.glob("wa-batch-*.txt"))  # no mobile -> not prepped
