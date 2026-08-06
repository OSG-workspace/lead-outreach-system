# Full-Haiku Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Opus with Haiku for the entire lead outreach pipeline by building parallel bounded sourcing agents, three new utility scripts, and the memory/contract files that wire it all together.

**Architecture:** The pipeline has 8 stages. Only Stages 1–2 use Haiku (orchestrate + parallel DDG sourcing). Stages 3–8 are pure Python/bash driven by `--run-dir` instead of hardcoded paths. A set of markdown memory files (`PIPELINE.md`, `agents/source-agent.md`, `agents/run-kickoff.md`) give Haiku all the context it needs at each step without relying on conversation history.

**Tech Stack:** Python 3.10+, bash, Brevo REST API, Claude Code WebSearch tool (DDG), `dispatching-parallel-agents` skill

---

## Task 1: Test suite bootstrap + merge_candidates.py tests

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_merge_candidates.py`

- [ ] **Step 1: Create the tests directory**

```bash
mkdir -p <home>/lead-outreach-system/project/tests
touch <home>/lead-outreach-system/project/tests/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_merge_candidates.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
pytest tests/test_merge_candidates.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'merge_candidates'`

- [ ] **Step 4: Commit the tests**

```bash
git -C <home>/lead-outreach-system/project add tests/
git -C <home>/lead-outreach-system/project commit -m "test: add merge_candidates tests"
```

---

## Task 2: Write merge_candidates.py

**Files:**
- Create: `tools/scripts/merge_candidates.py`

- [ ] **Step 1: Write the implementation**

Create `tools/scripts/merge_candidates.py`:

```python
#!/usr/bin/env python3
"""Stage 3: Merge candidates-batch-*.txt → candidates-all.txt.

Deduplicates by domain, drops already-sent domains, enforces GCC hard filter.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

GCC = {"AE", "SA", "QA", "BH", "KW"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_sent_domains(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    domains: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if "@" in e and "smtp-relay" not in e and "mailin.fr" not in e:
                domains.add(e.split("@")[1])
    return domains


def merge(run_dir: Path, sent_log: Path) -> list[str]:
    sent_domains = load_sent_domains(sent_log)
    seen: set[str] = set()
    rows: list[str] = []
    for batch_file in sorted(run_dir.glob("candidates-batch-*.txt")):
        for raw in batch_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue
            domain = parts[0].lower().strip()
            country = parts[2].upper().strip()
            if domain in seen:
                continue
            if country not in GCC:
                continue
            if domain in sent_domains:
                continue
            seen.add(domain)
            rows.append(line)
    return rows


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--sent-log", required=True)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    rows = merge(run_dir, Path(args.sent_log))

    out = run_dir / "candidates-all.txt"
    out.write_text("\n".join(rows) + ("\n" if rows else ""))

    batch_count = len(list(run_dir.glob("candidates-batch-*.txt")))
    print(f"Merged {len(rows)} candidates from {batch_count} batch files → {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests — expect all to pass**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
pytest tests/test_merge_candidates.py -v
```

Expected: `7 passed`

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/merge_candidates.py
git -C <home>/lead-outreach-system/project commit -m "feat: add merge_candidates.py (Stage 3)"
```

---

## Task 3: Tests for persist_sent_log.py

**Files:**
- Create: `tests/test_persist_sent_log.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_persist_sent_log.py`:

```python
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
```

- [ ] **Step 2: Run to verify they fail**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
pytest tests/test_persist_sent_log.py -v 2>&1 | head -10
```

Expected: `ModuleNotFoundError: No module named 'persist_sent_log'`

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tests/test_persist_sent_log.py
git -C <home>/lead-outreach-system/project commit -m "test: add persist_sent_log tests"
```

---

## Task 4: Write persist_sent_log.py

**Files:**
- Create: `tools/scripts/persist_sent_log.py`

- [ ] **Step 1: Write the implementation**

Create `tools/scripts/persist_sent_log.py`:

```python
#!/usr/bin/env python3
"""Stage 8: Append sent emails to vault/lead-outreach/sent-log.md.

Idempotent: skips message_ids already present in the log.
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

MSG_ID_RE = re.compile(r"<[^>]+@[^>]+>")


def load_existing_message_ids(sent_log: Path) -> set[str]:
    if not sent_log.exists():
        return set()
    ids: set[str] = set()
    for line in sent_log.read_text().splitlines():
        for m in MSG_ID_RE.finditer(line):
            ids.add(m.group(0))
    return ids


def format_row(row: dict, run_slug: str) -> str:
    date = (row.get("sent_at") or "")[:10]
    return (
        f"{date} | {row['to_email']} | [[{row['lead_slug']}]] "
        f"| step 1 | {run_slug} | {row['message_id']}"
    )


def append_rows(sent_jsonl: Path, sent_log: Path, run_slug: str) -> int:
    existing_ids = load_existing_message_ids(sent_log)
    new_rows: list[str] = []
    for line in sent_jsonl.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("result") != "sent":
            continue
        msg_id = row.get("message_id", "")
        if not msg_id or msg_id in existing_ids:
            continue
        new_rows.append(format_row(row, run_slug))
        existing_ids.add(msg_id)

    if not new_rows:
        return 0

    sent_log.parent.mkdir(parents=True, exist_ok=True)
    if not sent_log.exists():
        sent_log.write_text(
            "---\nname: sent-log\ndescription: Permanent dedup log of all outreach emails sent\n"
            "metadata:\n  type: project\n---\n\n# Sent Log\n\n"
            "Master dedup record. Format: `date | email | lead_slug | step | run_slug | brevo_message_id`\n\n"
        )
    with sent_log.open("a") as f:
        for row in new_rows:
            f.write(row + "\n")
    return len(new_rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--sent-log", required=True)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    sent_jsonl = run_dir / "emails-sent.jsonl"
    run_slug = run_dir.name

    added = append_rows(sent_jsonl, Path(args.sent_log), run_slug)
    print(f"Persisted {added} new entries to {args.sent_log}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests — expect all to pass**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
pytest tests/test_persist_sent_log.py -v
```

Expected: `6 passed`

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/persist_sent_log.py
git -C <home>/lead-outreach-system/project commit -m "feat: add persist_sent_log.py (Stage 8)"
```

---

## Task 5: Write fetch_html.sh

**Files:**
- Create: `tools/scripts/fetch_html.sh`

- [ ] **Step 1: Write the script**

Create `tools/scripts/fetch_html.sh`:

```bash
#!/usr/bin/env bash
# Stage 4: Fetch homepage HTML for every domain in candidates-all.txt.
# Skips already-fetched files. Runs 4 parallel curls.
set -euo pipefail

RUN_DIR="${1:?Usage: fetch_html.sh <run-dir>}"
CANDIDATES="$RUN_DIR/candidates-all.txt"
OUT_DIR="$RUN_DIR/raw_html"
mkdir -p "$OUT_DIR"

UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Write a worker script — avoids bash export-f portability issues
WORKER=$(mktemp /tmp/fetch_worker_XXXXXX.sh)
trap 'rm -f "$WORKER"' EXIT
cat > "$WORKER" << WORKER_EOF
#!/usr/bin/env bash
domain="\$1"; out_dir="\$2"
safe="\${domain//\//_}"
outfile="\$out_dir/\${safe}.html"
[ -f "\$outfile" ] && exit 0
curl -sL --max-time 10 --connect-timeout 5 \
    -A "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36" \
    --compressed \
    "https://\${domain}" -o "\${outfile}" 2>/dev/null \
    || rm -f "\${outfile}"
WORKER_EOF
chmod +x "$WORKER"

cut -d'|' -f1 "$CANDIDATES" | \
    grep -v '^[[:space:]]*$' | \
    grep -v '^#' | \
    xargs -P 4 -I{} bash "$WORKER" {} "$OUT_DIR"

fetched=$(find "$OUT_DIR" -name "*.html" 2>/dev/null | wc -l | tr -d ' ')
total=$(grep -c '[^[:space:]]' "$CANDIDATES" 2>/dev/null || echo 0)
echo "Fetched ${fetched} / ${total} → ${OUT_DIR}"
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x <home>/lead-outreach-system/project/tools/scripts/fetch_html.sh
```

- [ ] **Step 3: Smoke test with 3 known domains**

```bash
cd /tmp && mkdir -p test_fetch/raw_html
printf 'automatelb.com|Automate|AE|clinic|5\ngoogle.com|Google|AE|clinic|1\n' > test_fetch/candidates-all.txt
bash <home>/lead-outreach-system/project/tools/scripts/fetch_html.sh test_fetch
ls test_fetch/raw_html/
```

Expected: `automatelb.com.html  google.com.html` and `Fetched 2 / 2`

- [ ] **Step 4: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/fetch_html.sh
git -C <home>/lead-outreach-system/project commit -m "feat: add fetch_html.sh (Stage 4)"
```

---

## Task 6: Refactor extract_leads.py → tools/scripts/

**Files:**
- Create: `tools/scripts/extract_leads.py`

- [ ] **Step 1: Write the refactored script**

The logic is identical to the run-specific version. Only the path setup changes — replace the hardcoded `ROOT =` block at the top with argparse.

Create `tools/scripts/extract_leads.py`:

```python
#!/usr/bin/env python3
"""Stage 5: Extract emails + signals from bulk-fetched HTML.

Usage: python extract_leads.py --run-dir <path>
Outputs: leads-extracted.json (JSONL)
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# ── CLI ──────────────────────────────────────────────────────────────────────
p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
p.add_argument("--sent-log", default="")
args = p.parse_args()

ROOT = Path(args.run_dir)
RAW = ROOT / "raw_html"
CANDIDATES = ROOT / "candidates-all.txt"
OUT = ROOT / "leads-extracted.json"

# ── Regexes ───────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ROLE_RE = re.compile(
    r"^(info|contact|hello|hi|sales|admin|support|noreply|marketing|hr|jobs|"
    r"careers|booking|appointment|reception|office|team|enquiry|enquiries|"
    r"inquiry|inquiries|mail|email|service|customercare|help|feedback|"
    r"webmaster|finance|accounts|billing|news|abuse|postmaster|general|"
    r"operations|ops|frontdesk|press|media|pr|cs)@",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|html?)$"
    r"|@(example\.com|sentry\.io|wpforms\.com|wixpress\.com|wix\.com|"
    r"godaddy\.com|sentry-next|gmail\.com\.|domain\.com)"
    r"|^(\d+x|[a-f0-9]{16,})@",
    re.IGNORECASE,
)
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
            "icloud.com", "protonmail.com", "live.com", "aol.com"}

# ── Sent-log dedup ────────────────────────────────────────────────────────────
SENT: set[str] = set()
sent_log_path = Path(args.sent_log) if args.sent_log else ROOT.parent.parent / "vault" / "lead-outreach" / "sent-log.md"
if sent_log_path.exists():
    for line in sent_log_path.read_text().splitlines():
        for m in EMAIL_RE.finditer(line):
            e = m.group(0).lower()
            if "smtp-relay" not in e and "mailin.fr" not in e:
                SENT.add(e)


def root_domain(s: str) -> str:
    s = s.lower().strip()
    if "@" in s:
        s = s.split("@", 1)[1]
    s = s.split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    parts = [p for p in s.split(".") if p]
    if len(parts) < 2:
        return s
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "co", "gov", "med"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def detect_signal(html_lower: str) -> tuple[str, str]:
    has_whatsapp = bool(re.search(r"whatsapp|wa\.me/", html_lower))
    has_phone = bool(re.search(r"\+?\d{1,3}[\s\-]?\d{2,3}[\s\-]?\d{3,4}[\s\-]?\d{3,4}|tel:", html_lower))
    has_form = bool(re.search(r"<form|contact[\-_ ]form|name=\"email\"|input[^>]*email", html_lower))
    has_book = bool(re.search(r"book[\-_ ]now|book[\-_ ]online|book[\-_ ]appointment|book[\-_ ]your", html_lower))
    has_pdf_menu = bool(re.search(r"\.pdf.*menu|menu.*\.pdf", html_lower))
    has_real_booking = bool(re.search(
        r"calendly|setmore|fresha|simplybook|nookal|zenoti|10to8|appointy|squareup|acuityscheduling",
        html_lower,
    ))
    if has_real_booking:
        return "modern_booking", "uses a third-party online booking platform"
    if has_pdf_menu:
        return "pdf_menu", "PDF menu/brochure on the site"
    if has_whatsapp and not has_book:
        return "whatsapp", "primary customer contact is WhatsApp"
    if has_form and not has_book:
        return "contact_form", "site sends customers through a contact form"
    if has_book and not has_real_booking:
        return "booking_form_manual", "manual booking form, no integrated calendar"
    return "phone_led", "phone-led customer intake"


def classify_email(e: str, lead_domain: str) -> str:
    e = e.lower().strip()
    if "@" not in e or e.count("@") != 1:
        return "junk"
    local, dom = e.split("@", 1)
    if len(local) < 3:
        return "junk"
    if JUNK_RE.search(e):
        return "junk"
    if ROLE_RE.match(e):
        return "role"
    if dom in FREEMAIL:
        return "personal"
    return "person"


def lead_root_variants(domain: str) -> set[str]:
    d = root_domain(domain)
    base = d.split(".")[0]
    return {
        d, f"{base}.com", f"{base}.ae", f"{base}.sa", f"{base}.com.sa",
        f"{base}.qa", f"{base}.com.qa", f"{base}.bh", f"{base}.com.bh",
        f"{base}.kw", f"{base}.com.kw", f"{base}.me", f"{base}.org", f"{base}.net",
    }


def pick_best(emails: list[str], lead_domain: str) -> tuple[str, str]:
    variants = lead_root_variants(lead_domain)
    scored = []
    for e in emails:
        c = classify_email(e, lead_domain)
        if c == "junk":
            continue
        e_root = root_domain(e)
        if c in {"person", "role"} and lead_domain:
            if e_root not in variants:
                continue
        rank = {"person": 0, "role": 1, "personal": 2}[c]
        scored.append((rank, e, c))
    if not scored:
        return "", "missing"
    scored.sort(key=lambda x: (x[0], len(x[1])))
    return scored[0][1], scored[0][2]


# ── Main extraction loop ──────────────────────────────────────────────────────
leads = []
candidates_rows = [l.split("|") for l in CANDIDATES.read_text().splitlines() if l.strip()]

for row in candidates_rows:
    if len(row) < 5:
        continue
    domain, name, country, vertical, branches_s = row[0], row[1], row[2], row[3], row[4]
    html_file = RAW / f"{domain.replace('/', '_')}.html"
    if not html_file.exists():
        continue
    html = html_file.read_text(errors="ignore")
    html_lower = html.lower()
    clean_html = re.sub(r"%[0-9A-Fa-f]{2}", " ", html).replace(" ", " ")
    emails_found = list({m.group(0).lower() for m in EMAIL_RE.finditer(clean_html)})
    emails_found = [e.lstrip("%20").lstrip(".") for e in emails_found]
    best, klass = pick_best(emails_found, domain)
    if not best or best in SENT:
        continue
    signal, evidence = detect_signal(html_lower)
    if signal == "modern_booking":
        continue

    base = {"person": 92, "role": 80, "personal": 78}[klass]
    try:
        b = int(branches_s)
        if 5 <= b <= 15:
            base += 3
    except ValueError:
        pass

    leads.append({
        "lead_id": f"web-{domain.replace('.', '-')}",
        "lead_slug": domain.replace(".", "-"),
        "name": name,
        "website": f"https://{domain}",
        "country_code": country,
        "vertical": vertical,
        "branches_estimate": int(branches_s) if branches_s.isdigit() else 0,
        "to_email": best,
        "to_name": name,
        "email_class": klass,
        "signal": signal,
        "signal_evidence": evidence,
        "score": base,
    })

leads.sort(key=lambda x: -x["score"])
OUT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in leads) + "\n")
print(f"Extracted {len(leads)} leads")
print(f"Person-class: {sum(1 for l in leads if l['email_class'] == 'person')}")
print(f"Role-class:   {sum(1 for l in leads if l['email_class'] == 'role')}")
for l in leads[:10]:
    print(f"  {l['score']} {l['country_code']} {l['vertical']:12} {l['email_class']:8} {l['to_email']:40} {l['name']}")
```

- [ ] **Step 2: Smoke test against last run**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
python tools/scripts/extract_leads.py \
    --run-dir runs/2026-05-23-gcc-consumer-chains-fast \
    --sent-log vault/lead-outreach/sent-log.md
```

Expected: `Extracted 70 leads` (same count as the original run)

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/extract_leads.py
git -C <home>/lead-outreach-system/project commit -m "feat: refactor extract_leads.py to tools/scripts/ with --run-dir"
```

---

## Task 7: Refactor draft_emails.py → tools/scripts/

**Files:**
- Create: `tools/scripts/draft_emails.py`

- [ ] **Step 1: Write the refactored script**

Create `tools/scripts/draft_emails.py`:

```python
#!/usr/bin/env python3
"""Stage 6: Generate per-lead email drafts from leads-extracted.json.

Usage: python draft_emails.py --run-dir <path>
Outputs: emails-drafted.json (JSONL)
"""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
args = p.parse_args()

ROOT = Path(args.run_dir)
IN = ROOT / "leads-extracted.json"
OUT = ROOT / "emails-drafted.json"
RUN_SLUG = ROOT.name

COUNTRY_NAMES = {"AE": "the UAE", "SA": "Saudi Arabia", "QA": "Qatar", "BH": "Bahrain", "KW": "Kuwait"}
VERTICAL_NAMES = {
    "clinic": "clinic groups", "vet": "veterinary clinics",
    "optical": "optical chains", "fitness": "fitness brands",
    "salon": "salon and spa brands", "spa": "spa brands",
    "restaurant": "restaurant groups", "cafe": "cafe groups",
    "cloud_kitchen": "cloud-kitchen groups", "bakery": "bakery groups",
    "pharmacy": "pharmacy chains", "retail": "specialty retailers",
}
SIGNAL_OPENERS = {
    "phone_led": lambda name, country: (
        f"{name}'s visible customer path appears to be phone-led. For a chain in {country}, "
        f"that is exactly the kind of repeated intake work a custom AI system can take off the front desk."
    ),
    "contact_form": lambda name, country: (
        f"{name}'s site sends customers through a contact form rather than a real-time workflow. "
        f"At chain scale in {country}, that creates manual back-and-forth after every inquiry."
    ),
    "whatsapp": lambda name, country: (
        f"{name} runs most customer contact through WhatsApp. At chain scale in {country}, "
        f"every reply is still a manual person — and a clean place to slot in a custom AI layer."
    ),
    "booking_form_manual": lambda name, country: (
        f"{name}'s booking flow sends customers through a form rather than an integrated calendar. "
        f"For a chain in {country}, that means every booking still routes through a person."
    ),
    "pdf_menu": lambda name, country: (
        f"{name}'s online presence still leans on PDF menus and brochure pages. "
        f"For a chain in {country}, that's a lot of manual customer ops the team is absorbing."
    ),
    "no_online_booking": lambda name, country: (
        f"{name}'s site doesn't surface a real online booking flow. "
        f"For a chain in {country}, that means front-desk staff are still the booking system."
    ),
}


def name_short(n: str) -> str:
    n = re.split(r"\s+[\-|–|—]\s+", n)[0]
    n = re.sub(r"\s+(LLC|L\.L\.C\.|FZ-LLC|FZE|Co\.?|Group)$", "", n, flags=re.IGNORECASE)
    if len(n) > 40:
        n = n[:40].rsplit(" ", 1)[0]
    return n.strip()


def draft(lead: dict) -> dict:
    name = name_short(lead["name"])
    country = COUNTRY_NAMES.get(lead["country_code"], lead["country_code"])
    vertical = VERTICAL_NAMES.get(lead["vertical"], "chains")
    signal = lead.get("signal", "phone_led")
    opener = SIGNAL_OPENERS.get(signal, SIGNAL_OPENERS["phone_led"])(name, country)
    subject = f"{name}: AI for the intake workflow"
    body_text = (
        f"Hello,\n\n{opener}\n\n"
        f"Automate is an AI consulting firm that designs custom AI systems for mid-market companies in the GCC. "
        f"For {vertical}, we build the intake, reporting, and handoff workflows around the tools already in place. "
        f"You own the system, with no monthly platform lock-in. Not a chatbot, but a custom AI tool that quietly "
        f"takes work off your team.\n\n"
        f"A fraction of what global consulting firms charge, and we walk if there is no clear ROI in the first 20 minutes.\n\n"
        f"Worth a brief call this week?\n\nRegards,\nDavid\nAutomate, automatelb.com"
    )
    paragraphs = body_text.strip().split("\n\n")
    body_html = "\n".join(f"<p>{para.replace(chr(10), '<br>')}</p>" for para in paragraphs)
    return {
        "lead_id": lead["lead_id"],
        "lead_slug": lead["lead_slug"],
        "to_email": lead["to_email"],
        "to_name": name,
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
        "tags": ["cold-outreach", RUN_SLUG, lead["vertical"], lead["country_code"]],
        "score": lead["score"],
        "qualification_status": "send_ready",
        "funnel_status": "needs_signal",
        "signal_used": signal,
        "primary_gap": signal,
        "email_class": lead["email_class"],
        "send_gate": "pass" if lead["email_class"] in {"person", "role"} else "review",
        "country_code": lead["country_code"],
        "vertical": lead["vertical"],
    }


leads = [json.loads(l) for l in IN.read_text().splitlines() if l.strip()]
drafts = [draft(l) for l in leads if l.get("email_class") != "personal"]
drafts.sort(key=lambda d: -d["score"])
OUT.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in drafts) + "\n")
print(f"Drafted {len(drafts)} emails")
for d in drafts[:3]:
    print(f"\n--- {d['to_email']} | {d['to_name']} ---")
    print(f"Subject: {d['subject']}")
    print(d["body_text"][:200] + "...")
```

- [ ] **Step 2: Smoke test against last run**

```bash
source tools/venv/bin/activate
python tools/scripts/draft_emails.py --run-dir runs/2026-05-23-gcc-consumer-chains-fast
```

Expected: `Drafted 70 emails` (matches original)

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/draft_emails.py
git -C <home>/lead-outreach-system/project commit -m "feat: refactor draft_emails.py to tools/scripts/ with --run-dir"
```

---

## Task 8: Refactor send.py → tools/scripts/

**Files:**
- Create: `tools/scripts/send.py`

- [ ] **Step 1: Write the refactored script**

Create `tools/scripts/send.py` — identical logic to the run-specific version, `--run-dir` replaces hardcoded `RUN_DIR`:

```python
#!/usr/bin/env python3
"""Stage 7: Send drafted emails via Brevo REST API with 30s pacing.

Usage:
  python send.py --run-dir <path>            # dry-run (default)
  python send.py --run-dir <path> --send     # actually send
  python send.py --run-dir <path> --send --cap 70 --pace 30
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def decode_brevo_key(token: str) -> str:
    if token.startswith("xkeysib-") and not token.endswith("=="):
        return token
    try:
        body = token[len("xkeysib-"):] if token.startswith("xkeysib-") else token
        return json.loads(base64.b64decode(body + "==").decode())["api_key"]
    except Exception:
        return token


def send_one(api_key: str, sender: dict, draft: dict, dry_run: bool) -> tuple[bool, str, dict]:
    if dry_run:
        return True, "", {"messageId": f"DRY-{int(time.time() * 1000)}"}
    from urllib import error, request as urllib_request
    payload = {
        "sender": sender,
        "to": [{"email": draft["to_email"].strip().lower(), "name": draft["to_name"]}],
        "replyTo": sender,
        "subject": draft["subject"][:200],
        "textContent": draft["body_text"],
        "htmlContent": draft["body_html"],
        "tags": draft.get("tags", []),
        "headers": {
            "X-Mailin-Custom": f"lead_id={draft['lead_id']}",
            "List-Unsubscribe": f"<mailto:{sender['email']}?subject=remove>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    }
    req = urllib_request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return True, "", json.loads(body) if body else {}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        return False, f"HTTP {exc.code}: {body[:300]}", {}
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-dir", required=True)
    p.add_argument("--send", action="store_true")
    p.add_argument("--pace", type=int, default=30)
    p.add_argument("--cap", type=int, default=100)
    args = p.parse_args()

    run_dir = Path(args.run_dir)
    project_root = run_dir.parent.parent
    env = {**load_env(project_root / ".env"), **os.environ}
    api_key = decode_brevo_key(env.get("BREVO_MCP_TOKEN", ""))
    sender_email = env.get("BREVO_SENDER_EMAIL", "")
    sender_name = env.get("BREVO_SENDER_NAME", "Automate")

    if not args.send:
        print("DRY-RUN mode (pass --send to actually ship)")
    elif not api_key or not sender_email:
        print("ABORT: missing BREVO_MCP_TOKEN or BREVO_SENDER_EMAIL", file=sys.stderr)
        sys.exit(1)
    sender = {"email": sender_email, "name": sender_name}

    drafts_path = run_dir / "emails-drafted.json"
    drafts = [json.loads(l) for l in drafts_path.read_text().splitlines() if l.strip()]
    seen: set[str] = set()
    unique = []
    for d in drafts:
        e = d["to_email"].lower()
        if e in seen:
            continue
        seen.add(e)
        unique.append(d)
    drafts = unique[: args.cap]
    print(f"Sending {len(drafts)} emails, pace={args.pace}s, dry_run={not args.send}")

    out_path = run_dir / "emails-sent.jsonl"
    log_path = run_dir / "send-log.txt"
    results = []
    with out_path.open("w") as out_f, log_path.open("w") as log_f:
        for i, draft in enumerate(drafts):
            ok, err, resp = send_one(api_key, sender, draft, dry_run=not args.send)
            sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ok else ""
            row = {
                "lead_id": draft["lead_id"],
                "lead_slug": draft["lead_slug"],
                "to_email": draft["to_email"],
                "to_name": draft["to_name"],
                "subject": draft["subject"],
                "result": "sent" if ok else "failed",
                "message_id": resp.get("messageId", ""),
                "sent_at": sent_at,
                "dry_run": not args.send,
                "vertical": draft.get("vertical"),
                "country_code": draft.get("country_code"),
            }
            if err:
                row["error"] = err
            out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            out_f.flush()
            line = (
                f"[{sent_at or 'NOW'}] {row['result']:6} {i+1:3}/{len(drafts)} "
                f"{draft['to_email']:45} {err or resp.get('messageId', '')}"
            )
            print(line)
            log_f.write(line + "\n")
            log_f.flush()
            results.append(row)
            if i + 1 < len(drafts) and args.send:
                time.sleep(args.pace)

    sent = sum(1 for r in results if r["result"] == "sent")
    failed = sum(1 for r in results if r["result"] == "failed")
    print(f"\nDONE: attempted={len(results)} sent={sent} failed={failed} dry_run={not args.send}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test dry-run against last run**

```bash
source tools/venv/bin/activate
python tools/scripts/send.py --run-dir runs/2026-05-23-gcc-consumer-chains-fast --cap 3
```

Expected: `DRY-RUN mode` then `Sending 3 emails, pace=30s, dry_run=True` then 3 DRY- lines

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add tools/scripts/send.py
git -C <home>/lead-outreach-system/project commit -m "feat: refactor send.py to tools/scripts/ with --run-dir"
```

---

## Task 9: Write agents/source-agent.md

**Files:**
- Create: `agents/source-agent.md`

- [ ] **Step 1: Create the agents directory and write the file**

```bash
mkdir -p <home>/lead-outreach-system/project/agents
```

Create `agents/source-agent.md`:

````markdown
# Source Agent

You are a lead sourcing agent for the Automate outreach pipeline. Your sole task: run DDG searches for a batch of queries, extract GCC consumer chain companies from the results, and write them in the exact pipe-delimited format below. Nothing else.

## Your output format

One line per company, no headers, no markdown, no commentary:

```
domain|CompanyName|COUNTRY|vertical|branches
```

Example:
```
thewarehousegym.com|The Warehouse Gym|AE|fitness|18
noyaclinic.sa|Noya Clinic|SA|clinic|6
zawyacoffee.qa|Zawya Coffee|QA|cafe|12
dentakay.com|Dentakay|AE|clinic|0
```

## Per-query workflow

For each query in your assigned batch:
1. Run `WebSearch` for the query
2. Scan the top 8–10 results
3. For each result matching the rules below: write one line to your output
4. Move to the next query

Target: 8–15 candidates per query. If a query returns nothing relevant, skip it and move on.

## What to include

A company is valid if ALL of these are true:
- Has a real website (not Instagram/Facebook/TikTok/LinkedIn/WhatsApp/Linktree)
- Operates in UAE, Saudi Arabia, Qatar, Bahrain, or Kuwait
- Is a consumer-facing chain (multi-location, not a single-location independent)
- Fits one of the verticals in the list below
- Is NOT a directory, aggregator, press article, or job listing

## What to skip

- Single-location independents (one clinic, one restaurant)
- Enterprise giants (>30 locations: Apparel Group, Americana, Chalhoub, McDonalds)
- Pure franchise outlets where each location is independently owned
- Companies whose website is a social profile or link-in-bio
- Results that are news articles, job listings, or review aggregators
- Any domain already in your current batch output (deduplicate)

## Country detection rules (in order)

1. TLD: `.ae` → AE, `.sa` → SA, `.qa` → QA, `.bh` → BH, `.kw` → KW
2. City in snippet/address: Dubai/Abu Dhabi/Sharjah/Ajman → AE | Riyadh/Jeddah/Dammam/Khobar → SA | Doha/Lusail → QA | Manama → BH | Kuwait City → KW
3. Cannot determine → skip the company entirely

## Branch estimate rules (in order)

1. Number stated in name or snippet: "8 Locations", "20+ branches", "clinics across 6 cities" → use that number
2. "branches"/"outlets"/"clinics"/"locations" count visible anywhere → use it
3. Chain name + GCC city with no count → write `0` (the extract script handles it)
4. Ambiguous single listing with no chain signals → skip

Write `0` when unsure. Do not guess. Never fabricate a number.

## Vertical mapping

Use exactly one of these values — no variations:

| Vertical | Matches |
|---|---|
| `clinic` | dental, medical, dermatology, aesthetics, IVF, laser, hair transplant, optical (if medical), diagnostics, physiotherapy, weight loss, polyclinic |
| `vet` | veterinary, pet clinic, animal hospital |
| `optical` | optician, eyewear, contact lens store |
| `fitness` | gym, fitness club, yoga studio, pilates, boutique fitness, ladies gym |
| `salon` | hair salon, beauty salon, barber, nails, blow dry bar |
| `spa` | spa, massage, wellness centre |
| `restaurant` | restaurant, fast casual, casual dining, cloud kitchen |
| `cafe` | cafe, coffee shop, juice bar, smoothie bar |
| `bakery` | bakery, patisserie, dessert chain |
| `pharmacy` | pharmacy, drugstore |
| `retail` | apparel, beauty retail, home goods, specialty consumer retail |

If a company doesn't fit any of these: skip it.

## Domain format

- Use the root domain only: `thewarehousegym.com` not `www.thewarehousegym.com/about`
- Lowercase always
- No trailing slash

## Output file

Write all lines to the file path you were given as `output_path`. Append as you go — do not wait until the end. When all queries in your batch are done, report: `Done: N candidates written to <output_path>`
````

- [ ] **Step 2: Verify the file was created**

```bash
wc -l <home>/lead-outreach-system/project/agents/source-agent.md
```

Expected: `~80 lines`

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add agents/source-agent.md
git -C <home>/lead-outreach-system/project commit -m "feat: add agents/source-agent.md (Haiku sourcing memory)"
```

---

## Task 10: Write agents/run-kickoff.md

**Files:**
- Create: `agents/run-kickoff.md`

- [ ] **Step 1: Write the file**

Create `agents/run-kickoff.md`:

````markdown
# Run Kickoff

You are the pipeline orchestrator for the Automate lead outreach system. When the user asks to run a campaign, follow these steps exactly. Read this file fully before taking any action.

## Step 1 — Orient

Read `project/PIPELINE.md`. This is the master contract.  
Read `runs/<slug>/run-config.md` (the user will give you the slug, or you create one: `YYYY-MM-DD-<brief-slug>`).  
Read `icp.yaml` and `queries.txt` in the run folder (copy from last run if the brief is the same ICP).

## Step 2 — Create the run folder

```bash
RUN=runs/YYYY-MM-DD-<slug>
mkdir -p $RUN/raw_html
```

If `icp.yaml` and `queries.txt` already exist in the run folder: use them.  
If not: copy from the last run folder (`runs/$(ls runs/ | sort | tail -1)/`).

## Step 3 — Split queries into batches

```bash
split -l 10 $RUN/queries.txt $RUN/queries-batch-
# renames to queries-batch-aa, queries-batch-ab, ...
```

Count the batch files: `ls $RUN/queries-batch-* | wc -l`

## Step 4 — Dispatch parallel sourcing agents

Use the `dispatching-parallel-agents` skill.

For each batch file, dispatch one sourcing agent with this prompt (fill in the variables):

```
You are a sourcing agent. Read agents/source-agent.md fully — it is your complete instruction set.

Your query batch (search each query with WebSearch):
<contents of queries-batch-XX>

Output path: runs/<slug>/candidates-batch-<N>.txt

Write your output lines directly to that file as you go.
```

Dispatch ALL agents simultaneously (parallel). Wait for all to complete.

## Step 5 — Merge candidates

```bash
source tools/venv/bin/activate
python tools/scripts/merge_candidates.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Check: `wc -l $RUN/candidates-all.txt` — expect 300–600 lines.  
If fewer than 100: report to user and stop. The sourcing agents may have been too restrictive.

## Step 6 — Fetch HTML

```bash
bash tools/scripts/fetch_html.sh $RUN
```

Watch for: `Fetched N / M → raw_html/`. N should be within 20% of M (some domains will be unreachable).

## Step 7 — Extract leads

```bash
python tools/scripts/extract_leads.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Check: `wc -l $RUN/leads-extracted.json` — expect 50–150 qualified leads.

## Step 8 — Draft emails

```bash
python tools/scripts/draft_emails.py --run-dir $RUN
```

Check: `wc -l $RUN/emails-drafted.json`

## Step 9 — Preview + user approval

Show the user 3 sample emails:
```bash
python -c "
import json
lines = open('$RUN/emails-drafted.json').read().splitlines()[:3]
for l in lines:
    d = json.loads(l)
    print(f'TO: {d[\"to_email\"]}')
    print(f'SUBJECT: {d[\"subject\"]}')
    print(d['body_text'][:300])
    print('---')
"
```

Then ask: **"N emails drafted. Send all N? (yes/no)"**

Do NOT send until the user says yes.

## Step 10 — Send

```bash
python tools/scripts/send.py \
    --run-dir $RUN \
    --send \
    --cap <cap from run-config.md> \
    --pace 30
```

Run in background. Monitor `$RUN/send-log.txt`.

## Step 11 — Persist

```bash
python tools/scripts/persist_sent_log.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Report to user: "Campaign complete. Sent N/M. Sent-log updated."
````

- [ ] **Step 2: Commit**

```bash
git -C <home>/lead-outreach-system/project add agents/run-kickoff.md
git -C <home>/lead-outreach-system/project commit -m "feat: add agents/run-kickoff.md (orchestrator memory)"
```

---

## Task 11: Write PIPELINE.md

**Files:**
- Create: `PIPELINE.md`

- [ ] **Step 1: Write the file**

Create `project/PIPELINE.md`:

````markdown
# Pipeline Contract

The Automate outreach pipeline. 8 stages. Read this file at the start of every session.

## Quick reference

| Stage | Script | Input | Output |
|---|---|---|---|
| 1 | (Haiku orchestrator) | run-config.md, queries.txt | dispatches Stage 2 |
| 2 | (Haiku sourcing agents, parallel) | source-agent.md, 10 queries each | candidates-batch-N.txt |
| 3 | `tools/scripts/merge_candidates.py` | candidates-batch-*.txt | candidates-all.txt |
| 4 | `tools/scripts/fetch_html.sh` | candidates-all.txt | raw_html/ |
| 5 | `tools/scripts/extract_leads.py` | raw_html/, candidates-all.txt | leads-extracted.json |
| 6 | `tools/scripts/draft_emails.py` | leads-extracted.json | emails-drafted.json |
| 7 | `tools/scripts/send.py` | emails-drafted.json | emails-sent.jsonl |
| 8 | `tools/scripts/persist_sent_log.py` | emails-sent.jsonl | vault/lead-outreach/sent-log.md |

## To start a new run

Read `agents/run-kickoff.md`. Follow it exactly.

## Run folder layout

```
runs/YYYY-MM-DD-<slug>/
  run-config.md         ← brief, cap, ICP pointer
  icp.yaml              ← scoring rubric
  queries.txt           ← search queries (one per line)
  queries-batch-aa      ← 10-query batches (created by run-kickoff)
  queries-batch-ab
  ...
  candidates-batch-1.txt  ← sourcing agent outputs
  candidates-batch-2.txt
  ...
  candidates-all.txt    ← merged + deduped (Stage 3 output)
  raw_html/             ← fetched HTML (Stage 4 output)
  leads-extracted.json  ← scored leads (Stage 5 output)
  emails-drafted.json   ← ready-to-send drafts (Stage 6 output)
  emails-sent.jsonl     ← send results (Stage 7 output)
  send-log.txt          ← human-readable send log
```

## Key constraints

- **NEVER re-contact** an email already in `vault/lead-outreach/sent-log.md`
- **Dedup is non-negotiable** — merge_candidates.py enforces it at Stage 3; extract_leads.py enforces it again at Stage 5
- **Send cap** is set in `run-config.md` — never exceed it
- **User must approve** before Stage 7 runs (`--send` flag requires explicit yes)
- **modern_booking signal** → skip the lead (gap already solved)

## Scripts

All scripts use the project venv:
```bash
source tools/venv/bin/activate
```

All scripts accept `--run-dir <path>` as their only required argument (except fetch_html.sh which takes it as $1).

The `.env` file at `project/.env` holds `BREVO_MCP_TOKEN`, `BREVO_SENDER_EMAIL`, `BREVO_SENDER_NAME`.

## Sourcing agents

Each sourcing agent is a fresh Haiku instance. It reads `agents/source-agent.md` and its query batch. It writes `candidates-batch-N.txt` directly. All agents run in parallel via `dispatching-parallel-agents` skill.

132 queries ÷ 10 per batch = ~14 agents.
````

- [ ] **Step 2: Commit**

```bash
git -C <home>/lead-outreach-system/project add PIPELINE.md
git -C <home>/lead-outreach-system/project commit -m "feat: add PIPELINE.md (master pipeline contract)"
```

---

## Task 12: Update CLAUDE.md + run-config.md template

**Files:**
- Modify: `CLAUDE.md`
- Create: `runs/run-config-template.md`

- [ ] **Step 1: Update CLAUDE.md to point to PIPELINE.md**

In `CLAUDE.md`, replace the "Core principle" section with:

```markdown
## Core principle: PIPELINE.md is the contract

At the start of every session, read `project/PIPELINE.md` first. It contains the 8-stage pipeline, the run folder layout, all script invocations, and key constraints (dedup, send cap, user approval gate).

For starting a new run, read `agents/run-kickoff.md` — it is the step-by-step orchestrator playbook.

For sourcing agent context, read `agents/source-agent.md` — it is the complete DDG search + extraction instruction set for parallel Haiku sourcing agents.

Long-term memory (sent history, voice, ICP) lives in the Obsidian vault at `vault/lead-outreach/`.
```

- [ ] **Step 2: Create run-config.md template**

Create `runs/run-config-template.md`:

```markdown
# Run Config

run_slug: YYYY-MM-DD-<slug>
created: YYYY-MM-DD
brief: "<one sentence describing the campaign target and pitch angle>"
icp: same-as-last-run   # or: new (describe changes)
send_cap: 70
pace_seconds: 30
model: haiku
approval_mode: full_auto
notes: ""
```

- [ ] **Step 3: Commit**

```bash
git -C <home>/lead-outreach-system/project add CLAUDE.md runs/run-config-template.md
git -C <home>/lead-outreach-system/project commit -m "feat: update CLAUDE.md + add run-config template"
```

---

## Task 13: End-to-end smoke test

**Files:** None (verification only)

- [ ] **Step 1: Run the full test suite**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate
pytest tests/ -v
```

Expected: all tests pass

- [ ] **Step 2: Simulate a new run with a test folder**

```bash
cd <home>/lead-outreach-system/project
source tools/venv/bin/activate

# Create a test run
mkdir -p /tmp/test-run/raw_html
cp runs/2026-05-23-gcc-consumer-chains-fast/candidates-batch-*.txt /tmp/test-run/ 2>/dev/null || \
  cp runs/2026-05-23-gcc-consumer-chains-fast/candidates-all.txt /tmp/test-run/candidates-batch-1.txt
cp -r runs/2026-05-23-gcc-consumer-chains-fast/raw_html /tmp/test-run/

# Stage 3
python tools/scripts/merge_candidates.py \
    --run-dir /tmp/test-run \
    --sent-log vault/lead-outreach/sent-log.md

# Stage 5
python tools/scripts/extract_leads.py --run-dir /tmp/test-run

# Stage 6
python tools/scripts/draft_emails.py --run-dir /tmp/test-run

# Stage 7 dry-run
python tools/scripts/send.py --run-dir /tmp/test-run --cap 3

# Stage 8
python tools/scripts/persist_sent_log.py \
    --run-dir /tmp/test-run \
    --sent-log /tmp/test-sent-log.md
cat /tmp/test-sent-log.md
```

Expected: each stage prints success, final persist shows 3 DRY entries appended

- [ ] **Step 3: Commit test results note**

```bash
git -C <home>/lead-outreach-system/project add -A
git -C <home>/lead-outreach-system/project commit -m "chore: full-haiku pipeline complete — all stages portable, agents wired"
```
