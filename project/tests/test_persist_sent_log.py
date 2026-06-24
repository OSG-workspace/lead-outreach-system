"""Tests for persist_sent_log.py"""
import json
import sys
from pathlib import Path
import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from persist_sent_log import load_existing_message_ids, append_rows, format_row


def make_sent_jsonl(tmp_path, rows):
    p = tmp_path / "emails-sent.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_load_existing_empty(tmp_path):
    log = tmp_path / "sent-log.md"
    assert load_existing_message_ids(log) == set()


def test_load_existing_reads_message_ids(tmp_path):
    log = tmp_path / "sent-log.md"
    log.write_text("| 2026-05-23 | a@b.com | slug | step1 | run | <abc123@smtp> |\n")
    ids = load_existing_message_ids(log)
    assert "<abc123@smtp>" in ids


def test_format_row():
    row = {
        "sent_at": "2026-05-23T15:30:00Z",
        "to_email": "info@clinic.ae",
        "lead_slug": "clinic-ae",
        "run": "2026-05-23-test",
        "message_id": "<msg123@smtp>",
    }
    line = format_row(row, run_slug="2026-05-23-test")
    assert "info@clinic.ae" in line
    assert "<msg123@smtp>" in line
    assert line.startswith("2026-05-23")


def test_append_rows_creates_log_if_missing(tmp_path):
    log = tmp_path / "sent-log.md"
    sent = make_sent_jsonl(tmp_path, [
        {"sent_at": "2026-05-23T15:30:00Z", "to_email": "a@b.ae",
         "lead_slug": "b-ae", "message_id": "<id1@smtp>", "result": "sent"}
    ])
    append_rows(sent, log, run_slug="2026-05-23-test")
    assert log.exists()
    assert "a@b.ae" in log.read_text()


def test_append_rows_idempotent(tmp_path):
    log = tmp_path / "sent-log.md"
    sent = make_sent_jsonl(tmp_path, [
        {"sent_at": "2026-05-23T15:30:00Z", "to_email": "a@b.ae",
         "lead_slug": "b-ae", "message_id": "<id1@smtp>", "result": "sent"}
    ])
    append_rows(sent, log, run_slug="2026-05-23-test")
    append_rows(sent, log, run_slug="2026-05-23-test")
    lines = [l for l in log.read_text().splitlines() if "a@b.ae" in l]
    assert len(lines) == 1


def test_append_rows_skips_failed(tmp_path):
    log = tmp_path / "sent-log.md"
    sent = make_sent_jsonl(tmp_path, [
        {"sent_at": "", "to_email": "a@b.ae",
         "lead_slug": "b-ae", "message_id": "", "result": "failed"}
    ])
    append_rows(sent, log, run_slug="2026-05-23-test")
    assert not log.exists() or "a@b.ae" not in log.read_text()


def test_append_rows_skips_dry_run(tmp_path):
    log = tmp_path / "sent-log.md"
    sent = make_sent_jsonl(tmp_path, [
        {"sent_at": "2026-05-23T15:30:00Z", "to_email": "a@b.ae",
         "lead_slug": "b-ae", "message_id": "<id1@smtp>", "result": "sent",
         "dry_run": True}
    ])
    append_rows(sent, log, run_slug="2026-05-23-test")
    assert not log.exists() or "a@b.ae" not in log.read_text()
