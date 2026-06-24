#!/usr/bin/env python3
"""Recovery: re-run signal extraction on accumulated resolved leads, then re-qualify and draft.

Used when the orchestrator's in-pipeline signal-extract step returned
fetched=false for every URL (Chromium-pool contamination). Standalone
extract_signals.py works, so we reap chromium and rerun it directly.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("<home>/Desktop/lead-outreach-system/project")
SCRIPTS = ROOT / "tools" / "scripts"
VENV_PY = ROOT / "tools" / "venv" / "bin" / "python"
sys.path.insert(0, str(SCRIPTS))

import funnel_lib as fl  # noqa: E402
from run_campaign import classify_signal  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def reap_chromium() -> int:
    killed = 0
    for pat in ("chrome-headless-shell", "chromium", "playwright"):
        try:
            out = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True, timeout=10)
            pids = [p for p in out.stdout.split() if p.isdigit()]
            if pids:
                subprocess.run(["kill", "-9", *pids], capture_output=True, timeout=10)
                killed += len(pids)
        except Exception:
            pass
    return killed


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: recover_signals.py <run-slug>")
        sys.exit(2)
    run_slug = sys.argv[1]
    run_dir = ROOT / "runs" / run_slug
    cfg = fl.build_funnel_config(run_dir)
    target = 100

    log(f"recovery start: {run_slug}")

    # Step 1: reap orphan Chromium
    killed = reap_chromium()
    log(f"reaped {killed} chromium-related processes")
    time.sleep(2)

    # Step 2: collect ALL resolved leads from every pass (dedup by id)
    resolved_files = sorted(run_dir.glob("leads-email-resolved-pass*.json"))
    log(f"merging {len(resolved_files)} resolved files: {[f.name for f in resolved_files]}")
    all_leads: dict[str, dict] = {}
    for f in resolved_files:
        for lead in fl.load_jsonl(f):
            key = (lead.get("id") or fl.slugify(lead.get("name") or "")
                   or lead.get("email") or "")
            if key and key not in all_leads:
                all_leads[key] = lead
    leads = list(all_leads.values())
    log(f"merged unique resolved leads: {len(leads)}")
    if not leads:
        log("no resolved leads — nothing to recover")
        return

    # Step 3: re-score (ensures funnel_status set)
    scored = [fl.score_lead(l, cfg) for l in leads]
    scored.sort(key=lambda r: -(r.get("score") or 0))
    fl.write_jsonl(run_dir / "leads-scored-recovery.json", scored)
    log(f"rescored: top score={scored[0].get('score') if scored else 0}, "
        f"send_ready={sum(1 for l in scored if l.get('funnel_status')=='send_ready')}, "
        f"needs_signal={sum(1 for l in scored if l.get('funnel_status') in {'needs_signal','signal_rescue'})}")

    # Step 4: signal-extract on top needs_signal leads (bigger budget than the orchestrator's)
    needs_signal = [l for l in scored if l.get("funnel_status") in {"needs_signal", "signal_rescue"}]
    sig_budget = max(target * 3, 50)
    needs_signal_top = needs_signal[:sig_budget]
    if needs_signal_top:
        sig_dir = run_dir / "signals-recovery"
        sig_dir.mkdir(exist_ok=True)
        urls_jsonl = run_dir / "signal-urls-recovery.jsonl"
        with urls_jsonl.open("w") as f:
            for lead in needs_signal_top:
                url = lead.get("url") or lead.get("website")
                lid = lead.get("id") or fl.slugify(lead.get("name"))
                if url and lid:
                    f.write(json.dumps({"url": url, "id": lid}) + "\n")
        n_urls = sum(1 for _ in urls_jsonl.open())
        log(f"signal-extract recovery: {n_urls} URLs at concurrency=20")
        # concurrency lower than orchestrator default (40) — gentler on Chromium
        proc = subprocess.run([
            str(VENV_PY), str(SCRIPTS / "extract_signals.py"),
            "--batch", str(urls_jsonl),
            "--out-dir", str(sig_dir),
            "--concurrency", "20",
        ], capture_output=True, text=True, timeout=3600)
        log(f"extract_signals rc={proc.returncode}")
        if proc.returncode != 0:
            log(f"extract_signals stderr tail: {proc.stderr[-400:]}")
        # Merge signals back
        merged = []
        for lead in scored:
            lid = lead.get("id") or fl.slugify(lead.get("name"))
            sig_file = sig_dir / f"{lid}.json"
            if sig_file.exists():
                try:
                    raw_sig = json.loads(sig_file.read_text())
                    if raw_sig.get("fetched"):
                        lead["signals"] = classify_signal(raw_sig)
                        lead["primary_gap"] = fl.signal_used(lead) or lead.get("primary_gap")
                except json.JSONDecodeError:
                    pass
            merged.append(lead)
        fetched_ok = sum(1 for l in merged if l.get("signals") and any(
            k in l.get("signals", {}) for k in ("mode", "ai_mentioned", "saas", "hiring_manual", "pdf_menu")))
        log(f"signals merged: leads with concrete signal = {fetched_ok}")
    else:
        merged = scored

    # Step 5: qualify
    send_ready = [l for l in merged if l.get("funnel_status") == "send_ready"]
    pool = needs_signal_top + send_ready
    qualify = fl.qualify_ranked_signals(pool, target=target * 2)
    qualified = qualify["qualified"]
    log(f"qualify_ranked_signals: tested={qualify['tested']} qualified={len(qualified)} "
        f"drops={qualify['drops']}")

    # Step 6: dedup + write final
    by_key: dict[str, dict] = {}
    for q in qualified:
        k = (q.get("email") or "") + "|" + (q.get("id") or q.get("name") or "")
        if k and k not in by_key:
            by_key[k] = q
    final = sorted(by_key.values(), key=lambda r: -(r.get("score") or 0))[:target]
    fl.write_jsonl(run_dir / "qualified-final.json", final)
    log(f"qualified-final.json: {len(final)} leads")

    # Step 7: drafts
    drafts = [fl.build_outreach_draft(l, run_slug) for l in final]
    fl.write_jsonl(run_dir / "emails-drafted.json", drafts)
    eligible, gate_report = fl.gate_drafts(drafts, role_threshold=cfg.get("role_inbox_threshold", 85))
    fl.write_jsonl(run_dir / "emails-eligible.json", eligible)
    log(f"drafts={len(drafts)} eligible={len(eligible)} dropped={gate_report['dropped']}")

    # Step 8: append to campaign-summary
    summary_path = run_dir / "campaign-summary.txt"
    body = (
        "\n\n============================================================\n"
        f"RECOVERY (signal-extract retry)\n"
        f"============================================================\n"
        f"merged resolved leads     : {len(leads)}\n"
        f"with concrete signal      : {fetched_ok if needs_signal_top else 'n/a'}\n"
        f"qualified after recovery  : {len(final)}\n"
        f"drafts                    : {len(drafts)}\n"
        f"eligible (send gate pass) : {len(eligible)}\n"
    )
    with summary_path.open("a") as f:
        f.write(body)
    log("done")


if __name__ == "__main__":
    main()
