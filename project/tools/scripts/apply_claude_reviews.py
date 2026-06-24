#!/usr/bin/env python3
"""
Apply email-review results from the claude-tasks queue back into the lead pool.

Read `runs/<slug>/claude-tasks/email-review-results/*.json` and patch the
matching leads in `leads-email-resolved.json` (or a specified file) with the
recovered `email` and `dm_name` fields. Re-runs deterministically; safe to
invoke after every Claude review pass.

No network calls. No model invocations. Pure file-merging.

Usage:
    ./tools/run.sh tools/scripts/apply_claude_reviews.py \\
        --run-slug 2026-05-20-test
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import funnel_lib as fl  # noqa: E402


def _classify(email: str) -> str:
    return fl.email_class(email)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-slug", required=True)
    p.add_argument("--target-file", default=None,
                   help="Lead file to patch. Default: latest leads-email-resolved-passN.json "
                        "or leads-email-resolved.json.")
    p.add_argument("--results-dir", default=None,
                   help="Defaults to runs/<slug>/claude-tasks/email-review-results/")
    args = p.parse_args()

    run_dir = ROOT / "runs" / args.run_slug
    if not run_dir.exists():
        raise SystemExit(f"runs/{args.run_slug} does not exist")

    results_dir = Path(args.results_dir) if args.results_dir else (
        run_dir / "claude-tasks" / "email-review-results"
    )
    if not results_dir.exists():
        print(f"no results dir at {results_dir} — nothing to apply")
        return

    if args.target_file:
        target = Path(args.target_file)
    else:
        candidates = sorted(run_dir.glob("leads-email-resolved-pass*.json"))
        target = candidates[-1] if candidates else (run_dir / "leads-email-resolved.json")
    if not target.exists():
        raise SystemExit(f"target file {target} does not exist")

    leads = fl.load_jsonl(target)
    by_id = {(l.get("id") or l.get("lead_id") or l.get("name")): l for l in leads}
    by_name = {(l.get("name") or ""): l for l in leads if l.get("name")}

    applied = 0
    skipped_no_email = 0
    skipped_wrong_domain = 0
    skipped_bad_class = 0
    for result_file in sorted(results_dir.glob("*.json")):
        try:
            r = json.loads(result_file.read_text())
        except json.JSONDecodeError:
            continue
        lead_id = r.get("lead_id")
        email = (r.get("email") or "").strip().lower()
        name = (r.get("name") or "").strip()
        lead = by_id.get(lead_id) or by_name.get(r.get("lead_name") or "")
        if not lead:
            continue
        if not email or "@" not in email:
            skipped_no_email += 1
            continue
        lead_domain = fl.root_domain(fl.lead_url(lead))
        if lead_domain and fl.root_domain(email) != lead_domain:
            skipped_wrong_domain += 1
            continue
        klass = _classify(email)
        if klass not in {"person", "personal"}:
            skipped_bad_class += 1
            continue
        lead["email"] = email
        lead["email_class"] = klass
        lead["email_source"] = "claude_review"
        if name and not lead.get("dm_name"):
            lead["dm_name"] = name
        if not lead.get("dm_email"):
            lead["dm_email"] = email
        applied += 1

    fl.write_jsonl(target, list(by_id.values()) if by_id else leads)

    print(json.dumps({
        "results_dir": str(results_dir),
        "target": str(target),
        "applied": applied,
        "skipped_no_email": skipped_no_email,
        "skipped_wrong_domain": skipped_wrong_domain,
        "skipped_bad_class": skipped_bad_class,
        "total_leads": len(leads),
    }, indent=2))


if __name__ == "__main__":
    main()
