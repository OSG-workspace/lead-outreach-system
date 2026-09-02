#!/usr/bin/env python3
"""The li-search -> LinkedIn outreach handoff, end to end, in one command.

    ./linkedin-run handoff <brief> [--take N] [--plan] [--no-writer]

WHAT IT RUNS, IN ORDER
  1. import_lisearch.py   li-search's delivered pool -> a run folder
  2. draft_linkedin.py --phase batch     one li-batch file per person
  3. li-writer (headless claude -p, parallel)   one custom DM per person
  4. draft_linkedin.py --phase merge     DMs back onto the person records
  5. linkedin_queue.py    rank, suppress, and MERGE INTO linkedin/state/backlog.json
  6. queue/generate.js    today's ramp-cap of invites, so the sender has work

After step 6 nothing else happens here. The channel's daily autopilot
(launchd `com.osg.linkedin-daily`, 09:10 weekdays) drains the backlog at
8->18 invites a day, sweeps acceptances, and sends each person's DM only after
they accept. A handoff QUEUES, exactly like a LinkedIn fire; "sent 0" is the
correct output.

WHY A SEPARATE DRIVER AND NOT A run_fire.py FLAG
run_fire.py's first stages are sourcing (OSM / Maps / Overture) and they
permanently consume city-ledger ground. A handoff has already been sourced —
by li-search — so it must enter the chain AFTER sourcing, at the point where a
LinkedIn fire has its people-qualified.json. Reusing run_fire would either
re-source or need a bypass flag threaded through twelve stages. The stages
this driver calls are the SAME scripts a fire calls (Stage 8.6c/d); nothing
here is a second implementation of anything.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_dispatch as ad  # noqa: E402

PROJECT = HERE.parents[1]
PY = sys.executable
PLAN = False


def sh(args: list[str], stage: str, tolerate: tuple[int, ...] = ()) -> int:
    print(f"\n=== {stage}: {' '.join(args)}")
    if PLAN:
        return 0
    r = subprocess.run(args, cwd=str(PROJECT))
    if r.returncode != 0 and r.returncode not in tolerate:
        sys.exit(f"ABORT at {stage}: exit {r.returncode}")
    return r.returncode


def write_dms(run: Path, max_workers: int, timeout: int) -> None:
    print(f"\n=== Stage 8.6 li-writer: one headless claude -p per person, ≤{max_workers} parallel")
    if PLAN:
        print("  [plan] would dispatch one li-writer per li-batch file")
        return
    batches = sorted(run.glob("li-batch-*.txt"))
    if not batches:
        print("  no li-batch files — nothing to write")
        return
    prompts = [f"Your input file: {b.resolve()}\n"
               "Read it, then follow your li-writer instructions and write your JSON "
               "to the OutputFile named inside it." for b in batches]
    print(f"  {len(prompts)} people")
    ok, why = ad.preflight()
    if not ok:
        sys.exit(f"ABORT: headless claude cannot reach the model — {why}")
    done = {"n": 0}

    def _cb(i, rc, out):
        done["n"] += 1
        last = (out.strip().splitlines() or [""])[-1][:80]
        print(f"  [{done['n']}/{len(prompts)}] li-writer {'ok' if rc == 0 else f'rc={rc}'}: {last}")

    ad.dispatch_pool("li-writer", prompts, max_workers=max_workers, timeout=timeout,
                     cwd=str(ad.REPO), on_done=_cb)
    written = len(list((run / "li-out").glob("*.json")))
    print(f"  li-writer wrote {written}/{len(prompts)} messages")


def main() -> None:
    global PLAN
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--brief")
    src.add_argument("--audience")
    ap.add_argument("--take", type=int, default=60)
    ap.add_argument("--pitch")
    ap.add_argument("--li-search-root")
    ap.add_argument("--run-dir")
    ap.add_argument("--config", default="{}")
    ap.add_argument("--max-workers", type=int, default=6)
    ap.add_argument("--writer-timeout", type=int, default=420)
    ap.add_argument("--plan", action="store_true", help="print every stage, run nothing")
    ap.add_argument("--no-writer", action="store_true",
                    help="skip li-writer (for tests, or when li-out/ is already filled by hand)")
    ap.add_argument("--no-queue", action="store_true",
                    help="stop after the backlog merge; do not write today's queue.json")
    a = ap.parse_args()
    PLAN = a.plan

    imp = [PY, "tools/scripts/import_lisearch.py", "--take", str(a.take), "--config", a.config]
    imp += ["--brief", a.brief] if a.brief else ["--audience", a.audience]
    if a.pitch:
        imp += ["--pitch", a.pitch]
    if a.li_search_root:
        imp += ["--li-search-root", a.li_search_root]
    run = Path(a.run_dir) if a.run_dir else None
    if run is None:
        # Let the importer pick the folder, then read it back from its own print.
        from datetime import date
        base = PROJECT / "runs" / f"{date.today().isoformat()}-li-{a.brief or a.audience}"
        run, k = base, 1
        while run.exists():
            run = base.with_name(f"{base.name}-{k}"); k += 1
    imp += ["--run-dir", str(run)]

    rc = sh(imp, "Stage 0-LS import from li-search", tolerate=(7,))
    if rc == 7:
        print("\nHANDOFF: nothing new to hand over — the channel already holds everyone delivered.")
        return
    sh([PY, "tools/scripts/draft_linkedin.py", "--phase", "batch", "--run-dir", str(run)],
       "Stage 8.6 LI batch")
    if not a.no_writer:
        write_dms(run, a.max_workers, a.writer_timeout)
    rc = sh([PY, "tools/scripts/draft_linkedin.py", "--phase", "merge", "--run-dir", str(run)],
            "Stage 8.6 LI merge", tolerate=(7,))
    if rc == 7:
        sys.exit("ABORT: li-writer composed no messages — nothing was queued. "
                 "Check li-out/ and the pitch, then re-run.")
    li_cfg = (run / "linkedin.json").read_text() if (run / "linkedin.json").exists() else "{}"
    sh([PY, "tools/scripts/linkedin_queue.py", "--run-dir", str(run), "--config", li_cfg],
       "Stage 8.6 LI rank -> backlog")
    if not a.no_queue:
        sh(["node", "linkedin/queue/generate.js", "--leads-file", str(run / "linkedin-leads.json")],
           "Stage 8.6 LI invite queue (today's cap)")

    if PLAN:
        print("\n[plan] done — nothing ran.")
        return
    state_dir = Path(os.environ.get("LINKEDIN_STATE_DIR") or PROJECT / "linkedin" / "state")
    waiting = 0
    try:
        waiting = len(json.loads((state_dir / "backlog.json").read_text()))
    except Exception:
        pass
    print(f"\nHANDOFF DONE: run {run.name} — {waiting} people now waiting in the backlog.")
    print("  This QUEUES. The daily autopilot sends 8->18 invites/day and DMs on acceptance.")
    print("  Check: ./linkedin-run status")


if __name__ == "__main__":
    main()
