#!/usr/bin/env python3
"""Deterministic /fire orchestrator — NO AI brain.

`python3 tools/scripts/run_fire.py <slug> [--dry-run] [--max-workers N]`

Runs the full pipeline as plain Python: every control-flow decision is code, and
the two LLM fan-outs (sourcing, enrichment/writing) are dispatched via
`agent_dispatch.py` (headless `claude -p`, one process per query/lead). The
sub-agents are the SAME Haiku agents with the SAME definitions/tools/model as the
in-session Agent-tool path — validated quality-equivalent 2026-06-24 — so quality
is preserved while the orchestrator's own token cost drops to ~zero.

Mirrors `.claude/commands/fire.md` stage-for-stage. Kill-on-fallback = non-zero
exit. Run from `project/`. Email send is gated behind real run (omit --dry-run).

Coverage: template, combined-custom (email-only), custom+WhatsApp, WhatsApp.
The phrase->slug routing stays a thin Claude turn (see CLAUDE.md); this script
takes an explicit slug.
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_dispatch as ad

PROJECT = Path(__file__).resolve().parents[2]          # .../project
SENT_LOG = "vault/lead-outreach/sent-log.md"


PLAN = False   # --plan: trace the stage sequence without executing anything
STATUS_FILE: Path | None = None   # runs/<slug>/status.txt — live heartbeat for the session/user


def status(line: str):
    """Overwrite the run's status.txt with the current stage + timestamp.
    The Claude session monitoring a fire reads THIS file to narrate progress
    to the user — a run must never go dark for minutes."""
    if PLAN or STATUS_FILE is None:
        return
    from datetime import datetime
    try:
        STATUS_FILE.write_text(f"{datetime.now().strftime('%H:%M:%S')}  {line}\n")
    except Exception:
        pass


def die(stage: str, msg: str, code: int = 1):
    status(f"ABORTED: {stage} — {msg}")
    print(f"ABORT: {stage} — {msg}", file=sys.stderr)
    sys.exit(code)


def sh(args: list[str], stage: str):
    """Run a pipeline script; abort the whole run on non-zero (kill-on-fallback)."""
    print(f"\n=== {stage}: {' '.join(args)}")
    status(f"{stage} — running")
    if PLAN:
        return
    r = subprocess.run(args, cwd=str(PROJECT))
    if r.returncode != 0:
        die(stage, f"command exited {r.returncode}", r.returncode)
    status(f"{stage} — done")


def read_cfg(run: Path, name: str, default: str = "") -> str:
    p = run / name
    return p.read_text().strip() if p.exists() else default


def cleanup_run_artifacts(run: Path):
    """After a successful send+persist, drop the bulky intermediates that have
    no post-run consumer: raw_html (was 28 GB across 58 old runs) and the
    per-agent batch/out files (hundreds per run). The merged/final artifacts
    (candidates-all, leads-*, emails-*, send logs) are kept for auditability.
    Set KEEP_RUN_ARTIFACTS=1 to skip (e.g. when debugging a stage)."""
    if PLAN or os.environ.get("KEEP_RUN_ARTIFACTS") == "1":
        return
    import shutil
    removed = 0
    raw = run / "raw_html"
    if raw.is_dir():
        shutil.rmtree(raw, ignore_errors=True)
        removed += 1
    for pat in ("candidates-batch-*.txt", "queries-batch-*", "enrich-batch-*.txt",
                "enrich-out-*.json", "lead-batch-*.txt", "lead-out-*.json",
                "gap-batch-*.txt", "gap-out-*.json", "wa-batch-*.txt", "wa-out-*.json"):
        for f in run.glob(pat):
            f.unlink(missing_ok=True)
            removed += 1
    print(f"  cleanup: removed raw_html + {removed - 1 if removed else 0} intermediate files "
          f"(KEEP_RUN_ARTIFACTS=1 to keep).")


def count_lines(p: Path) -> int:
    return sum(1 for ln in p.read_text().splitlines() if ln.strip()) if p.exists() else 0


def fan_out(agent: str, prompts: list[str], stage: str, max_workers: int, timeout: int = 420):
    """Dispatch one sub-agent per prompt, in parallel, via headless claude -p."""
    print(f"\n=== {stage}: dispatching {len(prompts)} × {agent} (≤{max_workers} parallel, headless)")
    status(f"{stage} — 0/{len(prompts)} {agent} agents finished (dispatching)")
    if PLAN:
        print(f"  [plan] would run {len(prompts)} {agent} agents; sample prompt:\n"
              f"      {(prompts[0][:120] + '…') if prompts else '(none)'}")
        return []
    done = {"n": 0}

    def _cb(i, rc, out):
        done["n"] += 1
        tag = "ok" if rc == 0 else f"rc={rc}"
        last = (out.strip().splitlines() or [""])[-1][:80]
        print(f"  [{done['n']}/{len(prompts)}] {agent} {tag}: {last}")
        status(f"{stage} — {done['n']}/{len(prompts)} {agent} agents finished")

    res = ad.dispatch_pool(agent, prompts, max_workers=max_workers, timeout=timeout,
                           cwd=str(ad.REPO), on_done=_cb)
    fails = [i for i, (rc, _o, _e) in enumerate(res) if rc != 0]
    if fails:
        # A few sub-agent failures are tolerable (the merge stage drops them),
        # but a large partial fan-out is a DEGRADED run — kill-on-fallback.
        # (2026-06-25-eu-hotels shipped with 24/94 enrich agents completed;
        # that must halt, not send a fraction of the campaign.)
        frac = len(fails) / len(prompts)
        print(f"  WARNING: {len(fails)}/{len(prompts)} {agent} dispatches returned non-zero.")
        if frac > 0.3:
            die(stage, f"{len(fails)}/{len(prompts)} {agent} sub-agents failed "
                       f"(>{0.3:.0%} — degraded fan-out, kill-on-fallback).", 2)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="run folder name under runs/ (e.g. 2026-06-23-eu-hotels)")
    ap.add_argument("--dry-run", action="store_true", help="draft only; do NOT send or persist")
    ap.add_argument("--plan", action="store_true", help="trace the stage sequence + config; execute nothing")
    ap.add_argument("--max-workers", type=int, default=12, help="parallel sub-agent cap")
    a = ap.parse_args()
    global PLAN
    PLAN = a.plan

    run = (PROJECT / "runs" / a.slug)
    if not run.is_dir():
        die("pre-flight", f"run folder not found: {run}")
    abs_run = run.resolve()
    global STATUS_FILE
    STATUS_FILE = abs_run / "status.txt"
    status("starting")

    # --- Step 1: resolve config (deterministic) ---
    source_agent = read_cfg(run, "source_agent.txt", "source-agent")
    draft_mode = read_cfg(run, "draft_mode.txt", "template").lower()
    channels_raw = read_cfg(run, "channels.json", '["email"]')
    try:
        channels = set(json.loads(channels_raw))
    except Exception:
        channels = {"email"}
    email_on = "email" in channels
    wa_on = "whatsapp" in channels
    combined_custom = (draft_mode == "custom" and email_on and not wa_on)
    print(f"slug={a.slug} source={source_agent} draft_mode={draft_mode} "
          f"email={email_on} whatsapp={wa_on} combined_custom={combined_custom} dry_run={a.dry_run}")

    # pre-flight: agent definition + queries
    if not (ad.AGENTS_DIR / f"{source_agent}.md").exists():
        die("pre-flight", f"missing agent def {source_agent}.md")
    queries = [q.strip() for q in read_cfg(run, "queries.txt").splitlines()
               if q.strip() and not q.strip().startswith("#")]
    if not queries:
        die("pre-flight", "queries.txt empty")

    # --- Step 2: source fan-out (1 agent per query) ---
    for f in run.glob("candidates-batch-*.txt"):
        f.unlink()
    src_prompts = []
    for i, q in enumerate(queries, 1):
        out = abs_run / f"candidates-batch-{i:03d}.txt"
        src_prompts.append(f"Query: {q}\nOutputFile: {out}")
    fan_out(source_agent, src_prompts, "Stage 2 source", a.max_workers, timeout=240)

    # --- Step 3: merge + dedup ---
    sh(["python3", "tools/scripts/merge_candidates.py", "--run-dir", str(run),
        "--sent-log", SENT_LOG], "Stage 3 merge")
    merged = count_lines(run / "candidates-all.txt")
    if not PLAN:
        if merged == 0:
            die("Stage 3 merge", "0 candidates after dedup")
        if merged < 50:
            print(f"  NOTE: only {merged} merged candidates (<50).")

    # --- Step 4: fetch HTML ---
    sh(["bash", "tools/scripts/fetch_html.sh", str(run)], "Stage 4 fetch")

    # --- Step 5: extract + score ---
    sh(["python3", "tools/scripts/extract_leads.py", "--run-dir", str(run),
        "--sent-log", SENT_LOG], "Stage 5 extract")
    if not PLAN and count_lines(run / "leads-extracted.json") == 0:
        die("Stage 5 extract", "0 leads extracted")

    # --- Step 5.3: qualify + cap (per-run enrich_cap.txt overrides ENRICH_MAX_LEADS) ---
    env_cap = read_cfg(run, "enrich_cap.txt")
    if env_cap.isdigit():
        os.environ["ENRICH_MAX_LEADS"] = env_cap
    sh(["python3", "tools/scripts/qualify_leads.py", "--run-dir", str(run)], "Stage 5.3 qualify")
    qualified = count_lines(run / "leads-qualified.json")
    print(f"  qualified: {qualified}")

    # --- Step 6: per-lead path ---
    if combined_custom:
        # ONE lead-writer per lead = find + write in a single pass (email-only custom).
        sh(["python3", "tools/scripts/draft_lead_custom.py", "--phase", "prep",
            "--run-dir", str(run)], "Stage 6.5C prep")
        batches = sorted(run.glob("lead-batch-*.txt"))
        prompts = [b.read_text() for b in batches]   # lead-writer reads scraped pages itself; keep inline (unchanged behavior)
        fan_out("lead-writer", prompts, "Stage 6.5C lead-writer", a.max_workers)
        sh(["python3", "tools/scripts/draft_lead_custom.py", "--phase", "merge",
            "--run-dir", str(run)], "Stage 6.5C merge")
    else:
        # 5.5 enrich (name-finder) — phone only when WhatsApp on.
        prep = ["python3", "tools/scripts/enrich_contact_person.py", "--phase", "prep",
                "--run-dir", str(run)]
        if wa_on:
            prep.append("--enrich-phone")
        sh(prep, "Stage 5.5 enrich prep")
        ebatches = sorted(run.glob("enrich-batch-*.txt"))
        # name-finder reads its own file (validated path-based dispatch) -> orchestrator stays lean
        nf_prompts = [
            f"Your input file: {b.resolve()}\n"
            "Read it, then follow your name-finder instructions and write your JSON to the OutputFile named inside it."
            for b in ebatches
        ]
        fan_out("name-finder", nf_prompts, "Stage 5.5 name-finder", a.max_workers)
        sh(["python3", "tools/scripts/enrich_contact_person.py", "--phase", "merge",
            "--run-dir", str(run)], "Stage 5.5 enrich merge")
        if not PLAN and count_lines(run / "leads-with-contact.json") == 0:
            die("Stage 5.5", "0 leads with resolved contact + direct email")

        # Step 7 draft
        if draft_mode == "custom":   # custom + WhatsApp path (gap-writer)
            sh(["python3", "tools/scripts/draft_custom.py", "--phase", "prep",
                "--run-dir", str(run)], "Stage 6 gap prep")
            gbatches = sorted(run.glob("gap-batch-*.txt"))
            prompts = [b.read_text() for b in gbatches]   # gap-writer reads pages itself; inline unchanged
            fan_out("gap-writer", prompts, "Stage 6 gap-writer", a.max_workers)
            sh(["python3", "tools/scripts/draft_custom.py", "--phase", "merge",
                "--run-dir", str(run)], "Stage 6 gap merge")
        else:                        # template
            sh(["python3", "tools/scripts/draft_emails.py", "--run-dir", str(run)],
               "Stage 6 template draft")

    drafted = count_lines(run / "emails-drafted.json")
    print(f"  drafted: {drafted}")
    if not PLAN and email_on and drafted == 0:
        die("Stage 6 draft", "0 emails drafted")

    # --- Step 8: send + persist (email) ---
    if email_on and not a.dry_run:
        # Refresh the dead-letter suppression list from Brevo FIRST — sending
        # while the bounce-list is stale re-mails known-dead addresses and
        # burns sender reputation. Aborts the run if Brevo is unreachable.
        sh(["python3", "tools/scripts/sync_brevo_events.py"], "Stage 7 bounce-sync")
        cap = os.environ.get("MAX_EMAILS_PER_RUN", "1000")
        sh(["python3", "tools/scripts/send_batch_brevo.py", "--run-dir", str(run),
            "--send", "--cap", cap], "Stage 7 send")
        sh(["python3", "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
            "--sent-log", SENT_LOG], "Stage 8 persist")
    elif email_on:
        print("\n[DRY-RUN] drafts written; send + persist skipped.")

    # --- Step 8.5: WhatsApp (opt-in) ---
    if wa_on:
        sh(["python3", "tools/scripts/draft_whatsapp_custom.py", "--phase", "prep",
            "--run-dir", str(run)], "Stage 8.5 WA prep")
        wbatches = sorted(run.glob("wa-batch-*.txt"))
        if wbatches:
            prompts = [b.read_text() for b in wbatches]
            fan_out("wa-writer", prompts, "Stage 8.5 wa-writer", a.max_workers)
            sh(["python3", "tools/scripts/draft_whatsapp_custom.py", "--phase", "merge",
                "--run-dir", str(run)], "Stage 8.5 WA merge")
            if not a.dry_run:
                sh(["node", "bridge/send_campaign.js", "--run-dir", str(run)], "Stage 8.5 WA send")
                # WhatsApp sends must land in the sent-log too (idempotent —
                # previously a WhatsApp-ONLY run never persisted at all).
                sh(["python3", "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
                    "--sent-log", SENT_LOG], "Stage 8.5 WA persist")
            else:
                print("[DRY-RUN] WhatsApp drafts written; send skipped.")
        else:
            print("  no WhatsApp drafts (no CEO mobile); skipping WA send.")

    if not a.dry_run:
        cleanup_run_artifacts(run)

    status(f"DONE — qualified={qualified} drafted={drafted} "
           f"{'(dry-run, nothing sent)' if a.dry_run else 'sent+persisted'}")
    print(f"\nDONE: {a.slug} — qualified={qualified} drafted={drafted} "
          f"{'(dry-run, nothing sent)' if a.dry_run else 'sent+persisted'}")


if __name__ == "__main__":
    main()
