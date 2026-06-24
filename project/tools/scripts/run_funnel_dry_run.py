#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from funnel_lib import (
    apply_source_fit,
    build_funnel_config,
    gate_drafts,
    load_jsonl,
    merge_signals,
    normalize_maps_record,
    normalize_web_record,
    qualify_ranked_signals,
    resolve_email_pool,
    score_lead,
    stage_report,
    summarize_status,
    write_jsonl,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the reusable funnel in dry-run mode against existing artifacts.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--target", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    cfg = build_funnel_config(run_dir)
    target = args.target or cfg["target_sends"]
    (run_dir / "funnel-config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

    raw_rows = [normalize_maps_record(row) for row in load_jsonl(run_dir / "maps-raw.json")]
    raw_rows.extend(normalize_web_record(row) for row in load_jsonl(run_dir / "web-raw.json"))
    source_fit, source_drops = apply_source_fit(raw_rows, cfg)
    write_jsonl(run_dir / "leads-source-fit.json", source_fit)

    signal_rows = load_jsonl(run_dir / "lead-signals.json")
    source_fit_with_signals = merge_signals(source_fit, signal_rows)
    email_resolved_pool, email_drops = resolve_email_pool(source_fit_with_signals)
    write_jsonl(run_dir / "leads-email-resolved.json", email_resolved_pool)
    score_candidates, score_source_drops = apply_source_fit(email_resolved_pool, cfg)
    scored = [score_lead(row, cfg) for row in score_candidates]
    scored.sort(key=lambda row: -(row.get("score") or 0))
    write_jsonl(run_dir / "leads-scored-balanced.json", scored)

    with_signals = merge_signals(scored, signal_rows)
    signal_result = qualify_ranked_signals(with_signals, target=target, batch_size=args.batch_size)
    write_jsonl(run_dir / "leads-signal-qualified.json", signal_result["qualified"])

    drafts = load_jsonl(run_dir / "emails-drafted.json")
    eligible, send_report = gate_drafts(drafts, role_threshold=cfg["role_inbox_threshold"])
    write_jsonl(run_dir / "emails-send-eligible.json", eligible)

    report = stage_report(
        run_dir,
        sourced_raw=len(raw_rows),
        source_fit=len(source_fit),
        source_fit_drops=dict(source_drops),
        email_resolved=len(email_resolved_pool),
        email_resolution_drops=dict(email_drops),
        score_source_fit=len(score_candidates),
        score_source_fit_drops=dict(score_source_drops),
        scored=len(scored),
        scoring=summarize_status(scored),
        signal_tested=signal_result["tested"],
        signal_qualified=len(signal_result["qualified"]),
        signal_drops=signal_result["drops"],
        signal_exhausted=signal_result["exhausted"],
        drafted=len(drafts),
        send_eligible=send_report["eligible"],
        send_drops=send_report["dropped"],
        dry_run=True,
        target=target,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
