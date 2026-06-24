"""Tests for merge_candidates.py"""
import sys
from pathlib import Path
import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from merge_candidates import load_sent_domains, merge


def test_load_sent_domains_empty(tmp_path):
    log = tmp_path / "sent-log.md"
    assert load_sent_domains(log) == set()


def test_load_sent_domains_reads_email_domains(tmp_path):
    log = tmp_path / "sent-log.md"
    log.write_text("2026-05-23 | info@example.com | lead | step1 | run | <id>\n")
    assert "example.com" in load_sent_domains(log)


def test_merge_deduplicates_by_domain(tmp_path):
    b1 = tmp_path / "candidates-batch-1.txt"
    b1.write_text("clinic.ae|Test Clinic|AE|clinic|5\n")
    b2 = tmp_path / "candidates-batch-2.txt"
    b2.write_text("clinic.ae|Test Clinic Duplicate|AE|clinic|5\n")
    sent_log = tmp_path / "sent-log.md"
    rows = merge(tmp_path, sent_log)
    assert len(rows) == 1
    assert rows[0].startswith("clinic.ae|")


def test_merge_drops_non_gcc(tmp_path):
    b = tmp_path / "candidates-batch-1.txt"
    b.write_text("clinic.eg|Egyptian Clinic|EG|clinic|5\n")
    rows = merge(tmp_path, tmp_path / "sent-log.md")
    assert rows == []


def test_merge_drops_sent_domains(tmp_path):
    b = tmp_path / "candidates-batch-1.txt"
    b.write_text("clinic.ae|Test Clinic|AE|clinic|5\n")
    log = tmp_path / "sent-log.md"
    log.write_text("2026-05-23 | info@clinic.ae | lead | step1 | run | <id>\n")
    rows = merge(tmp_path, log)
    assert rows == []


def test_merge_keeps_missing_branches_field(tmp_path):
    b = tmp_path / "candidates-batch-1.txt"
    b.write_text("clinic.ae|Test|AE|clinic|0\n")
    rows = merge(tmp_path, tmp_path / "sent-log.md")
    assert len(rows) == 1


def test_merge_skips_blank_and_comment_lines(tmp_path):
    b = tmp_path / "candidates-batch-1.txt"
    b.write_text("# comment\n\nclinic.ae|Test|AE|clinic|5\n")
    rows = merge(tmp_path, tmp_path / "sent-log.md")
    assert len(rows) == 1
