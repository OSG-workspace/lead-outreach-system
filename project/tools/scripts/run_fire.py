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
import concurrent.futures
import json
import math
import os
import re as _re
import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_dispatch as ad
from li_url import canonical_profile_url
from email_utils import COUNTRY_NAMES_ISO

PROJECT = Path(__file__).resolve().parents[2]          # .../project
SENT_LOG = "vault/lead-outreach/sent-log.md"

# Per-agent batch/out files live HERE — at the repo root, deliberately OUTSIDE
# project/. Claude Code loads every CLAUDE.md on the path from cwd down to a
# file an agent Reads, and project/CLAUDE.md is 30 KB (~7.5k tokens). Measured
# on 2026-09-02-gcc-receptionist: it rode along as `nested_memory` in 664/665
# name-finder transcripts, purely because their batch files sat under
# project/runs/<slug>/. Nothing under <repo>/.fire-work/ has a CLAUDE.md above
# it short of the repo root, so a Read of a batch file here loads nothing extra.
# Run-level artifacts (candidates, leads-*, emails-*, summaries) stay in the run
# folder; only the per-agent intermediates move. agent_dispatch enforces this.
WORK_ROOT = ad.REPO / ".fire-work"


def work_dir_for(slug: str) -> Path:
    return WORK_ROOT / slug


def _interpreter() -> str:
    """The interpreter EVERY stage subprocess must run under: the project venv.

    Stages are dispatched as `[PY, "tools/scripts/<stage>.py", ...]`. That slot
    used to be the literal string "python3", which resolves off $PATH — and on a
    normal shell that is /usr/bin/python3, NOT `tools/venv`. The venv is where
    the pipeline's third-party deps actually live (ddgs, crawl4ai, duckdb), so
    any stage needing one silently got a different, wrong interpreter.

    This is not hypothetical. On 2026-08-11-gcc-receptionist, Stage 2.5 ran on
    /usr/bin/python3, which has no `ddgs` but does have its frozen predecessor
    `duckduckgo_search` (renamed July 2025, no longer receiving anti-bot fixes).
    resolve_domains.py's import fallback dutifully used it and every single
    search returned nothing: 0/300 verified, breakdown {'no-results': 300}. The
    same queries return 5/5 results under the venv. It read as "GCC businesses
    are unfindable" — a data-supply conclusion — when it was purely this.

    Only `fetch_html.sh` was immune, because it sources the venv itself; that
    is why Stage 4 rendered 303 pages while Stage 2.5 scored zero in the same
    run. Resolving here fixes every stage of every campaign at once, and keeps
    working no matter how the fire was started (fire_campaign.sh, ./linkedin-run,
    a bare `python3 run_fire.py`, launchd).
    """
    venv = PROJECT / "tools" / "venv" / "bin" / "python3"
    if venv.exists():
        return str(venv)
    # No venv (fresh checkout before tools/install.sh): fall back to the
    # interpreter running THIS file rather than $PATH's, so at least the
    # orchestrator and its children stay on one interpreter.
    print(f"WARN: {venv} missing — stages fall back to {sys.executable}. "
          f"Run `bash tools/install.sh` to build the venv.", file=sys.stderr)
    return sys.executable


PY = _interpreter()


PLAN = False   # --plan: trace the stage sequence without executing anything
STATUS_FILE: Path | None = None   # runs/<slug>/status.txt — live heartbeat for the session/user
# status.txt and stdout are now written from several threads at once (the
# Stage 2 source pool, the fan-out callbacks): one lock each keeps a line whole.
_STATUS_LOCK = threading.Lock()
_PRINT_LOCK = threading.Lock()


def status(line: str):
    """Overwrite the run's status.txt with the current stage + timestamp.
    The Claude session monitoring a fire reads THIS file to narrate progress
    to the user — a run must never go dark for minutes."""
    if PLAN or STATUS_FILE is None:
        return
    from datetime import datetime
    try:
        with _STATUS_LOCK:
            STATUS_FILE.write_text(f"{datetime.now().strftime('%H:%M:%S')}  {line}\n")
    except Exception:
        pass


def die(stage: str, msg: str, code: int = 1):
    status(f"ABORTED: {stage} — {msg}")
    print(f"ABORT: {stage} — {msg}", file=sys.stderr)
    # An aborted run keeps no bulk either (user policy 2026-08-25): raw_html has
    # no consumer after death — a re-fire always starts a fresh folder, so the
    # only thing an aborted run's raw_html ever did was accumulate (2.8 GB by
    # the time this was added). KEEP_RUN_ARTIFACTS=1 still preserves everything.
    # …except raw_html on a run that already got past extract: the enrichment
    # prep reads it (SitePages + harvested addresses per lead), so a run
    # resumed in-session after an enrichment/draft/send abort needs it. On
    # 2026-09-01-gcc-receptionist the abort's cleanup deleted it, the re-run
    # prep produced 0/250 leads with site text, and 180 name-finders started
    # blind. cleanup_stale_runs() still sweeps it after 6h.
    if STATUS_FILE is not None:
        try:
            cleanup_run_artifacts(STATUS_FILE.parent,
                                  keep_raw=(STATUS_FILE.parent / "leads-extracted.json").exists())
        except Exception:
            pass
    sys.exit(code)


def sh(args: list[str], stage: str, tolerate: tuple[int, ...] = (),
       defer_fail: bool = False):
    """Run a pipeline script; abort the whole run on non-zero (kill-on-fallback).
    `tolerate` lists exit codes that are an expected mid-run state, not a
    fallback (e.g. qualify's rc=7 "0 qualified yet" between sourcing waves of a
    --target-leads run). `defer_fail` returns the code instead of aborting, so
    the caller can die with a message only it can build (Stage 2 names the
    sources the crash skipped). Returns the exit code."""
    print(f"\n=== {stage}: {' '.join(args)}")
    status(f"{stage} — running")
    if PLAN:
        return 0
    r = subprocess.run(args, cwd=str(PROJECT))
    if r.returncode != 0 and r.returncode not in tolerate:
        if not defer_fail:
            die(stage, f"command exited {r.returncode}", r.returncode)
        return r.returncode
    status(f"{stage} — done")
    return r.returncode


def read_cfg(run: Path, name: str, default: str = "") -> str:
    p = run / name
    return p.read_text().strip() if p.exists() else default


def resolve_sourcing(run: Path) -> dict:
    """THE single sourcing contract (ONE chain, ONE Stage-2 method, every campaign).

    Stage 2 is ALWAYS deterministic OSM enumeration (source_overpass.py) — it
    lives on the main chain, not in a per-campaign branch. A fixture does not
    choose a *method*; the only things that vary per run are the two the user
    directive names (2026-07-27):

      * WHERE it looks    -> places.txt   (`City|ISO2|lat|lon|half_width_deg`)
      * WHO it looks for  -> `selector` + `vertical` in sourcing.json

    So a fixture's sourcing.json is just the target audience:

      {"selector": "\"office\"=\"lawyer\"", "vertical": "law", "max_per_run": 350}

    Adding a NEW campaign never touches this code: drop a new templates/<base>/
    folder with a places.txt + sourcing.json and the chain runs it unchanged.

    Legacy tolerance: a `method` key in an older fixture/run folder is IGNORED
    (not an error) so pre-2026-07-27 run folders stay re-runnable; `agent` is
    likewise ignored. The old per-file overpass_*.txt config still resolves."""
    cfg: dict = {}
    raw = read_cfg(run, "sourcing.json")
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception as e:
            die("pre-flight", f"sourcing.json is not valid JSON: {e}")
        if not isinstance(cfg, dict):
            die("pre-flight", "sourcing.json must be a JSON object")
    # legacy per-file config (pre-sourcing.json run folders)
    cfg.setdefault("selector", read_cfg(run, "overpass_selector.txt") or None)
    cfg.setdefault("vertical", read_cfg(run, "overpass_vertical.txt") or None)
    has_targets = isinstance(cfg.get("targets"), list) and bool(cfg["targets"])
    has_sources = isinstance(cfg.get("sources"), list) and bool(cfg["sources"])
    if not has_targets and not has_sources:
        if not cfg.get("selector"):
            die("pre-flight",
                'sourcing.json needs a "selector" — the OSM tag filter naming this '
                'run\'s target audience, e.g. {"selector": "\\"office\\"=\\"lawyer\\"", '
                '"vertical": "law"} — or a "targets" list for a multi-vertical campaign')
        if not cfg.get("vertical"):
            die("pre-flight", 'sourcing.json needs a "vertical" string for the candidate lines')
    cfg.setdefault("max_per_run", int(read_cfg(run, "overpass_max.txt", "350")))
    cfg.setdefault("region", read_cfg(run, "overpass_region.txt") or None)
    # A campaign may target SEVERAL audiences that live under different OSM keys
    # (clinics are `amenity`, salons are `shop`, brokerages are `office`), which
    # one selector cannot express. `targets` normalises both shapes to a list of
    # {selector, vertical} swept in ranked order:
    #   {"selector": "...", "vertical": "..."}                      -> 1 target
    #   {"targets": [{"selector": "...", "vertical": "..."}, ...]}  -> N targets
    if isinstance(cfg.get("targets"), list) and cfg["targets"]:
        targets = []
        for i, t in enumerate(cfg["targets"], 1):
            if not (isinstance(t, dict) and t.get("selector") and t.get("vertical")):
                die("pre-flight", f'sourcing.json targets[{i}] needs "selector" and "vertical"')
            targets.append({"selector": str(t["selector"]), "vertical": str(t["vertical"])})
        cfg["targets"] = targets
    elif cfg.get("selector"):
        cfg["targets"] = [{"selector": str(cfg["selector"]), "vertical": str(cfg["vertical"])}]
    else:
        cfg["targets"] = []

    # Stage 2 is a SET of deterministic SOURCE ADAPTERS, not one hard-wired source.
    # OSM only knows physical premises; whole ICPs (online stores, association
    # members, licensed-operator registers, portal rosters) have no map presence,
    # so a branch declares whichever sources reach ITS audience:
    #   {"sources": [{"type": "map", "targets": [...]},
    #                {"type": "directory", "url": "...", "name": "..."}]}
    # A fixture with only `targets`/`selector` is normalised to a single map
    # source, so every existing campaign is unchanged.
    if has_sources:
        srcs = []
        for i, s in enumerate(cfg["sources"], 1):
            if not isinstance(s, dict) or not s.get("type"):
                die("pre-flight", f'sourcing.json sources[{i}] needs a "type"')
            if s["type"] not in ("map", "directory", "places", "overture", "gmaps"):
                die("pre-flight", f'sourcing.json sources[{i}] unknown type {s["type"]!r} '
                                  '(known: "map", "places", "overture", "gmaps", "directory")')
            srcs.append(s)
        cfg["sources"] = srcs
    else:
        cfg["sources"] = [{"type": "map", "targets": cfg["targets"]}]

    # A `places` source defaults to Pro tier (see resolve_tier() in
    # source_places.py) precisely so it never requests/stores the website/phone
    # Content the Places API ToS restricts. Pro-tier results are therefore
    # name-only until Stage 2.5 (resolve_domains.py) independently proves a
    # domain against the business's own site. So a fixture that declares a
    # places source gets resolve_domains auto-enabled — without it, Pro-tier
    # results would source names that never become usable candidates, which is
    # a silent zero-yield failure, not a real choice. A fixture that explicitly
    # sets `"tier": "enterprise"` on every places source (a deliberate, opt-in
    # ToS tradeoff) or explicitly sets `"resolve_domains": false` is left alone.
    #
    # An `overture` source needs it only when the fixture opts INTO name-only
    # rows (`"require_website": false`). By default that source emits solely
    # rows whose domain the dataset already carries, so there is nothing for
    # Stage 2.5 to resolve and enabling it would just cost time.
    if "resolve_domains" not in cfg:
        needs_resolve = any(
            (s["type"] == "places" and (s.get("tier") or "pro") != "enterprise")
            or (s["type"] == "overture" and s.get("require_website") is False)
            for s in cfg["sources"])
        if needs_resolve:
            cfg["resolve_domains"] = True

    cfg["method"] = "map"   # retained for status/log wording only; never branches
    return cfg


def _send_outcome(run: Path, email_on: bool) -> str:
    """What the run ACTUALLY sent, read from emails-sent.jsonl.

    Never assert "sent+persisted" from the mere fact that the send stage ran.
    Two states used to be indistinguishable from that line: a real send, and a
    run whose every draft was suppressed at send time (already in the sent-log
    or bounce-list), which writes no send log at all and exits 0.
    """
    if not email_on:
        return "(no email channel)"
    log = run / "emails-sent.jsonl"
    if not log.exists():
        return "sent 0 (nothing left after suppression) — nothing persisted"
    sent = failed = 0
    for line in log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("result") == "sent":
            sent += 1
        elif r.get("result") == "failed":
            failed += 1
    if failed:
        # Defensive, and unreachable on a normal run: Stage 7 returns 6 on any
        # failure and die() exits before this line. It exists so the DONE line
        # stays truthful if that tolerance is ever widened, and so this function
        # is correct read on its own.
        return f"sent {sent}, FAILED {failed} — only the {sent} sent were persisted"
    return f"sent {sent}, persisted"


def slug_base_of(slug: str) -> str:
    """`2026-07-27-lb-insurance-tpa-1` -> `lb-insurance-tpa` (the fixture name)."""
    base = _re.sub(r"^\d{4}-\d{2}-\d{2}-", "", slug)
    return _re.sub(r"-\d+$", "", base)


# Words that carry no company identity, so they must never match a domain.
def cleanup_run_artifacts(run: Path, keep_raw: bool = False):
    """At the END of every run — success OR abort — drop the bulky intermediates
    that have no post-run consumer: raw_html (was 28 GB across 58 old runs, and
    2.8 GB again by 2026-08-25 because aborted/killed runs skipped this) and the
    per-agent batch/out files (hundreds per run). The merged/final artifacts
    (candidates-all, leads-*, emails-*, send logs) are kept for auditability.
    Set KEEP_RUN_ARTIFACTS=1 to skip (e.g. when debugging a stage).
    `keep_raw` (die() on a post-extract abort) spares raw_html only."""
    if PLAN or os.environ.get("KEEP_RUN_ARTIFACTS") == "1":
        return
    import shutil
    removed = 0
    raw = run / "raw_html"
    if raw.is_dir() and not keep_raw:
        shutil.rmtree(raw, ignore_errors=True)
        removed += 1
    elif raw.is_dir():
        print(f"  cleanup [{run.name}]: raw_html KEPT (run aborted after extract; "
              f"an in-session resume needs it — swept automatically after 6h).")
    # The per-agent work dir (<repo>/.fire-work/<slug>) holds every batch/out
    # file of this run and nothing else — the merges have already folded its
    # contents into the run-level artifacts, so it goes whole. Same three exit
    # paths as raw_html: DONE, die(), and the stale sweep at the next fire.
    work = work_dir_for(run.name)
    if work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
        removed += 1
    # Pre-2026-09-02 runs kept these intermediates IN the run folder; the stale
    # sweep still meets such folders, so the old patterns stay.
    for pat in ("candidates-batch-*.txt", "queries-batch-*", "enrich-batch-*.txt",
                "enrich-out-*.json", "lead-batch-*.txt", "lead-out-*.json",
                "gap-batch-*.txt", "gap-out-*.json", "wa-batch-*.txt", "wa-out-*.json",
                # LinkedIn per-agent intermediates. The ranked linkedin-leads.json
                # and people-*.json stay: the invite drip runs for weeks after the
                # fire and they are the audit trail for who was queued and why.
                "li-batch-*.txt", "lif-batch-*.txt", "li-email-batch-*.txt"):
        for f in run.glob(pat):
            f.unlink(missing_ok=True)
            removed += 1
    if removed:
        print(f"  cleanup [{run.name}]: removed raw_html, the .fire-work dir and "
              f"{max(0, removed - 2)} intermediate files (KEEP_RUN_ARTIFACTS=1 to keep).")


def cleanup_stale_runs(current: Path):
    """Sweep the bulk out of PAST runs that died without reaching cleanup —
    killed mid-fire, machine slept, or pre-policy. Called once per fire, so
    accumulation is bounded even when a run never exits through die()/DONE
    (SIGKILL leaves nothing else to hook). A run counts as dead only when its
    status.txt has been untouched for 6h: a live parallel fire rewrites its
    status at every stage and agent completion, so a fresh timestamp means
    hands off (parallel runs are normal and must never lose their raw_html)."""
    if PLAN or os.environ.get("KEEP_RUN_ARTIFACTS") == "1":
        return
    import shutil
    import time
    now = time.time()
    for d in sorted((PROJECT / "runs").iterdir()):
        if not d.is_dir() or d.resolve() == current.resolve():
            continue
        st = d / "status.txt"
        try:
            mtime = st.stat().st_mtime if st.exists() else d.stat().st_mtime
        except OSError:
            continue
        if now - mtime > 6 * 3600:
            cleanup_run_artifacts(d)
    # Orphaned work dirs: a .fire-work/<slug> whose run folder is gone (the
    # operator deleted the run by hand) is never reached by the loop above, and
    # one whose run is >6h stale is swept together with that run's raw_html.
    if WORK_ROOT.is_dir():
        for w in sorted(WORK_ROOT.iterdir()):
            if not w.is_dir() or w.name == current.name:
                continue
            run_d = PROJECT / "runs" / w.name
            st = run_d / "status.txt"
            try:
                if run_d.is_dir():
                    mtime = st.stat().st_mtime if st.exists() else run_d.stat().st_mtime
                    if now - mtime <= 6 * 3600:
                        continue          # a live parallel fire — hands off
            except OSError:
                continue
            shutil.rmtree(w, ignore_errors=True)
            print(f"  cleanup: removed stale work dir {w}")


def count_lines(p: Path) -> int:
    return sum(1 for ln in p.read_text().splitlines() if ln.strip()) if p.exists() else 0


def count_json(p: Path) -> int:
    """Entries in a pretty-printed JSON array.

    The LinkedIn stages write `json.dumps(..., indent=2)`, so count_lines() reads
    a 3-person file as ~30 — fine for the JSONL artifacts it was written for,
    nonsense here."""
    if not p.exists():
        return 0
    try:
        v = json.loads(p.read_text())
    except Exception:
        return 0
    return len(v) if isinstance(v, list) else 0


def merge_people(run: Path) -> int:
    """Fold the search-found people into people-raw.json, deduped by profile URL.

    Both routes (company-page walk, li-finder search) produce the same record
    shape and face the same gates, so from here on they are indistinguishable —
    qualify_people.py never learns which way a person was found, and must not.
    """
    def load(name: str) -> list:
        p = run / name
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text())
        except Exception:
            return []

    walked = load("people-raw.json")
    searched = load("people-found-harvested.json")
    by_url = {}
    for p in walked + searched:          # walk wins ties: it is first-hand
        u = (p.get("profile_url") or "").strip().lower().rstrip("/")
        if u and u not in by_url:
            by_url[u] = p
    merged = list(by_url.values())
    (run / "people-raw.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    print(f"  people: {len(walked)} walked + {len(searched)} found by search "
          f"-> {len(merged)} unique")
    return len(merged)


def _agent_output_path(prompt: str) -> Path | None:
    """The file this sub-agent was told to write, or None if the prompt names none.

    Every prep stage emits `OutputFile: <abs path>` into its batch
    (enrich_contact_person, draft_custom, draft_lead_custom,
    draft_whatsapp_custom, draft_linkedin, resolve_li_profiles). Two prompt
    shapes carry it: the batch text passed inline, or the path-based dispatch
    used by name-finder, whose prompt only names the INPUT file so the
    orchestrator stays lean — there the OutputFile lives one hop away."""
    m = _re.search(r"^OutputFile:\s*(.+)$", prompt, _re.M)
    if not m:
        src = _re.search(r"^Your input file:\s*(.+)$", prompt, _re.M)
        if not src:
            return None
        try:
            m = _re.search(r"^OutputFile:\s*(.+)$",
                           Path(src.group(1).strip()).read_text(), _re.M)
        except OSError:
            return None
        if not m:
            return None
    return Path(m.group(1).strip())


def fan_out(agent: str, prompts: list[str], stage: str, max_workers: int, timeout: int = 420,
            include: set[str] | None = None):
    """Dispatch one sub-agent per prompt, in parallel, via headless claude -p.

    `include` opts into the agent definition's OPTIONAL blocks; anything not
    named is stripped from the system prompt (see agent_dispatch.agent_body).
    Every `stage` label already names the agent ("Stage 5.5 name-finder"), so
    the status line reads `<stage> — N/M agents finished`."""
    print(f"\n=== {stage}: dispatching {len(prompts)} × {agent} (≤{max_workers} parallel, headless)")
    status(f"{stage} — 0/{len(prompts)} agents finished (dispatching)")
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
        status(f"{stage} — {done['n']}/{len(prompts)} agents finished")

    try:
        res = ad.dispatch_pool(agent, prompts, max_workers=max_workers, timeout=timeout,
                               cwd=str(ad.REPO), on_done=_cb, include=include)
    except RuntimeError as e:
        # The nested-CLAUDE.md guard: a batch file inside project/ would ship
        # project/CLAUDE.md to every agent of this fan-out. Not one is spawned.
        die(f"{stage} context hygiene", str(e))
    # A non-zero exit is NOT proof the agent did no work. dispatch_one returns
    # rc=-1 on a wall-clock timeout, and wall clock includes machine sleep, so a
    # closed lid fails agents that already finished. Measured on
    # 2026-08-17-au-trades-1: the gate reported 12/37 name-finders failed while
    # 34/37 enrich-out files sat on disk — 9 agents had written valid JSON and
    # were still counted toward the 30% abort. The run was killed on a 3/37 real
    # failure rate. So an agent counts as failed only if it ALSO left no output.
    def _failed(results, idxs):
        out_f, salv = [], 0
        for i, (rc, _o, _e) in zip(idxs, results):
            if rc == 0:
                continue
            out = _agent_output_path(prompts[i])
            if out is not None and out.exists() and out.stat().st_size > 0:
                salv += 1
                continue
            out_f.append(i)
        return out_f, salv

    fails, salvaged = _failed(res, range(len(prompts)))
    if salvaged:
        print(f"  {salvaged}/{len(prompts)} {agent} dispatches exited non-zero but WROTE "
              f"their output — counted as done (timeout/sleep, not lost work).")
    if fails:
        # ONE retry before judging the fan-out. The failures that leave no output
        # are overwhelmingly transient: on 2026-08-31-gcc-receptionist 23/235
        # tier-2 name-finders died on wall-clock timeouts (17) and
        # `API Error: Unable to connect to API (ConnectionRefused)` (6) — each
        # one a lead that never got its web pass.
        print(f"  retrying {len(fails)}/{len(prompts)} {agent} dispatch(es) that failed "
              f"without writing output …")
        status(f"{stage} — retrying {len(fails)} failed agents")
        res2 = ad.dispatch_pool(agent, [prompts[i] for i in fails], max_workers=max_workers,
                                timeout=timeout, cwd=str(ad.REPO), include=include)
        for j, i in enumerate(fails):
            res[i] = res2[j]
        fails, _ = _failed(res2, fails)
        if fails:
            print(f"  {len(fails)} still failed after retry: "
                  + "; ".join(f"[{i + 1}] {(res[i][2] or res[i][1]).strip()[-90:]}" for i in fails[:5]))
    if fails:
        # A few sub-agent failures are tolerable (the merge stage drops them),
        # but a large partial fan-out is a DEGRADED run — kill-on-fallback.
        # (2026-06-25-eu-hotels shipped with 24/94 enrich agents completed;
        # that must halt, not send a fraction of the campaign.)
        frac = len(fails) / len(prompts)
        print(f"  WARNING: {len(fails)}/{len(prompts)} {agent} dispatches returned non-zero "
              f"AND produced no output file.")
        if frac > 0.3:
            die(stage, f"{len(fails)}/{len(prompts)} {agent} sub-agents failed "
                       f"(>{0.3:.0%} — degraded fan-out, kill-on-fallback).", 2)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="run folder name under runs/ (e.g. 2026-06-23-eu-hotels)")
    ap.add_argument("--dry-run", action="store_true", help="draft only; do NOT send or persist")
    ap.add_argument("--plan", action="store_true", help="trace the stage sequence + config; execute nothing")
    # 12 -> 16 (2026-08-19): the sub-agents are I/O-bound web researchers, and
    # at 12 workers the 243-agent Stage 5.5 fan-out of 2026-08-18-au-trades ran
    # 36 minutes (~106s/agent -> the pool, not the agent, was the constraint).
    # 16 -> 32 (2026-09-02): still pool-bound. On 2026-09-02-gcc-receptionist
    # the agents averaged 76 s and 8 API calls each (network-bound, not
    # compute-bound), and 326 x 76 s / 16 workers = 25.8 min against the
    # observed 27.8. Each worker is a full headless `claude -p` process, so
    # memory and subscription-side throttling scale with this number —
    # AGENT_WORKERS overrides without an argv change.
    ap.add_argument("--max-workers", type=int,
                    default=int(os.environ.get("AGENT_WORKERS", "32")),
                    help="parallel sub-agent cap (env AGENT_WORKERS, default 32)")
    ap.add_argument("--target-leads", type=int, default=0,
                    help="aim the run at OUTREACHING ~N leads: search sourcing dispatches "
                         "agents in adaptive waves until enough qualified leads exist "
                         "(N / TARGET_SURVIVAL_RATE, default 0.5, to absorb enrich/draft "
                         "attrition) or queries run out; the send itself is capped at N. "
                         "0 = classic behavior (all queries in one wave, default caps).")
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

    # Per-agent batch/out files go to <repo>/.fire-work/<slug>/ (see WORK_ROOT)
    # and every prep/merge script is told so via --work-dir. Nothing else of the
    # run moves. Created here so a fan-out never finds it missing; removed by
    # cleanup_run_artifacts() on every exit path.
    work = work_dir_for(a.slug)
    if not PLAN:
        work.mkdir(parents=True, exist_ok=True)
    print(f"work dir (per-agent batch/out files, outside project/): {work}")
    WD = ["--work-dir", str(work)]

    # Nothing a sub-agent never reads may ride along in its context. A CLAUDE.md
    # in a parent directory of the checkout is auto-discovered by every headless
    # `claude -p`, so one stray file costs (its size x every dispatch) per fire.
    # Caught here, before sourcing burns any ledger ground.
    foreign = [] if os.environ.get("ALLOW_FOREIGN_CLAUDE_MD") else ad.foreign_memory_files()
    if foreign:
        listing = "; ".join(f"{p} ({p.stat().st_size // 1024} KB)" for p in foreign)
        die("pre-flight context hygiene",
            f"{len(foreign)} CLAUDE.md file(s) above the checkout would be loaded into "
            f"EVERY sub-agent dispatch: {listing}. They belong to other projects and no "
            f"stage of this run reads them. Move each into its own project folder "
            f"(e.g. `mkdir -p <dir>/<project> && mv <dir>/CLAUDE.md <dir>/<project>/`), "
            f"or set ALLOW_FOREIGN_CLAUDE_MD=1 to fire anyway.")

    # The headless fan-outs are the whole enrichment stage; prove `claude -p`
    # can reach the model NOW, before Stage 2 spends ledger ground. On
    # 2026-09-01-gcc-receptionist 247/247 name-finders failed `Not logged in`
    # after 45 minutes of sourcing + fetch, and the abort retired 238 domains.
    if not PLAN and os.environ.get("SKIP_HEADLESS_PREFLIGHT") != "1":
        status("pre-flight — probing headless claude -p")
        ok, detail = ad.preflight()
        if not ok:
            die("pre-flight headless dispatch",
                f"`claude -p` cannot run a sub-agent: {detail!r}. Every fan-out of "
                f"this run would fail the same way, after sourcing had spent ledger "
                f"ground. Fix it first — `claude update` (model support), `claude` "
                f"then /login (auth), and fire from a plain terminal, not a "
                f"sandboxed shell — or SKIP_HEADLESS_PREFLIGHT=1 to fire anyway.")
        print(f"  pre-flight: headless claude -p answered ({detail[:40]!r}).")

    # Outbound port 25: the enrich merge's SMTP rescue probe (smtp_email_probe.py)
    # can only ever answer when this network lets us reach a mail exchanger.
    # On 2026-09-02-gcc-receptionist 24/71 probes were doomed by no-MX or an
    # unreachable port 25 and each still paid its 10 s timeout; 54 "smtp
    # unreachable" notes sit across 8 runs. One 3 s check here decides it for
    # the whole run: SMTP_PROBE=0 (honoured by the merge) skips every probe.
    # The helper is being added to smtp_email_probe.py; until it exists, or if
    # the probe itself errors, the merge keeps its own per-lead behaviour.
    smtp_note = ""
    if not PLAN and os.environ.get("SMTP_PROBE") is None:
        try:
            import smtp_email_probe as _sep
            _p25 = getattr(_sep, "port25_reachable", None)
        except (Exception, SystemExit):   # the probe sys.exit()s on a missing dnspython
            _p25 = None
        if _p25 is not None:
            try:
                reachable = bool(_p25(timeout=3.0))
            except Exception:
                reachable = True          # an erroring check must not disable the rescue
            if not reachable:
                os.environ["SMTP_PROBE"] = "0"     # inherited by every stage subprocess
                smtp_note = " [smtp: port 25 blocked, rescue skipped]"
                print("  pre-flight: outbound port 25 unreachable — SMTP rescue probes "
                      "skipped for this run (SMTP_PROBE=0).")

    cleanup_stale_runs(abs_run)   # sweep bulk left by runs that died before their own cleanup

    # --- Step 1: resolve config (deterministic) ---
    sourcing = resolve_sourcing(run)
    draft_mode = read_cfg(run, "draft_mode.txt", "template").lower()
    channels_raw = read_cfg(run, "channels.json", '["email"]')
    try:
        channels = set(json.loads(channels_raw))
    except Exception:
        channels = {"email"}
    email_on = "email" in channels
    # "whatsapp"          -> full dual channel (every phone-carrying lead gets WA)
    # "whatsapp-fallback" -> keep leads whose owner/CEO direct email could NOT be
    #                        verified (they'd otherwise die at the email gate) and
    #                        reach them on WhatsApp instead.
    # BOTH together       -> PHONE-PRIORITY mode: keep the phone-only leads AND send
    #                        WhatsApp to every phone-carrying lead, not just those.
    #                        Used by the Lebanon campaigns (LB decision-makers reply
    #                        on WhatsApp far more than on email).
    wa_full = "whatsapp" in channels
    wa_fallback = "whatsapp-fallback" in channels      # enrich: rescue phone-only leads
    wa_send_fallback_only = wa_fallback and not wa_full  # draft: restrict WA to those leads
    wa_on = wa_full or wa_fallback
    # LinkedIn is an opt-in ARM of the same run, exactly like WhatsApp: same
    # sourcing, same merge/dedup/qualify, different outreach surface. It walks
    # the qualified COMPANIES to people, then DMs a person instead of emailing
    # a mailbox.
    li_on = "linkedin" in channels
    # "linkedin-email-lookup" is a much smaller opt-in than full LinkedIn
    # outreach: it never invites or DMs anyone, and spends none of the
    # invite/DM ramp in linkedin/config/limits.json. It only rescues leads
    # that died at Stage 5.5 with a real decision-maker name but no email —
    # li-finder locates their profile (reusing that name, not a fresh search),
    # then lookup_contact_email.js checks LinkedIn's Contact Info panel for a
    # personal address. A hit rejoins the ordinary email path. Added
    # 2026-08-20 after 2026-08-20-eu-hotels: 80 of 99 qualified leads died for
    # exactly that one reason.
    li_email_lookup = "linkedin-email-lookup" in channels
    # A LinkedIn-ONLY run never touches a mailbox, so every email-shaped stage
    # (the no-email drop at extract, the email-quality gates at qualify, the
    # decision-maker email enrichment, the email drafter) is skipped rather than
    # run and ignored. Before this, such a run halted at Stage 5.5 on
    # "0 leads with resolved contact + direct email" — an email condition
    # killing a channel that needs no email.
    li_only = li_on and not email_on and not wa_on
    # The same argument for WhatsApp-only runs (e.g. lb-enterprise,
    # channels.json = ["whatsapp"]). Without this they took the EMAIL path and
    # died three ways: enrich dropped every lead whose decision-maker publishes
    # no direct email — on a run that never sends email — then draft_custom.py
    # burned one Sonnet gap-writer per surviving lead to produce an
    # emails-drafted.json that draft_whatsapp_custom.py does not read, and
    # finally exited 5 when that email drafter had nothing left, aborting the
    # whole run. WhatsApp still needs enrichment (it wants the person's NAME and
    # MOBILE), so unlike li_only this keeps Stage 5.5 and only removes the
    # email-shaped parts of it.
    wa_only = wa_on and not email_on and not li_on
    combined_custom = (draft_mode == "custom" and email_on and not wa_on)
    # A non-map source carries its verticals on its TARGETS (places) or on the
    # source itself (directory). Reading only the source-level key printed
    # "places:?" for every places fixture, which tells the operator nothing in a
    # status line whose whole job is to say what this run is sourcing.
    def _src_verticals(x: dict) -> str:
        vs = [t["vertical"] for t in (x.get("targets") or []) if t.get("vertical")]
        return f"{x['type']}:{'+'.join(dict.fromkeys(vs)) or x.get('vertical') or '?'}"

    src_label = ",".join(
        [t["vertical"] for x in sourcing["sources"] if x["type"] == "map"
         for t in (x.get("targets") or sourcing["targets"])]
        + [_src_verticals(x) for x in sourcing["sources"]
           if x["type"] != "map"]) or "none"
    print(f"slug={a.slug} sourcing=map({src_label}) draft_mode={draft_mode} "
          f"email={email_on} whatsapp={wa_on} linkedin={li_on} li_email_lookup={li_email_lookup} "
          f"wa_fallback={wa_fallback} "
          f"wa_phone_priority={wa_full and wa_fallback} "
          f"combined_custom={combined_custom} dry_run={a.dry_run}")

    # --- Steps 2..5.3: source -> merge -> fetch -> extract -> qualify ---
    # With --target-leads N (search method) this is an adaptive LOOP: dispatch a
    # wave of query agents, run the funnel, measure the qualified yield, size the
    # next wave from the observed rate, until needed_q qualified leads exist or
    # queries run out. Without a target: ONE wave of all queries — the classic
    # single-pass behavior, unchanged.
    target = max(0, a.target_leads)
    survival = float(os.environ.get("TARGET_SURVIVAL_RATE", "0.5"))
    needed_q = math.ceil(target / survival) if target else 0
    if target:
        print(f"target: outreach≈{target} leads -> aiming for {needed_q} qualified "
              f"(enrich/draft survival est {survival:.0%}; TARGET_SURVIVAL_RATE to tune)")

    # --- LinkedIn preflight: check the browser BEFORE spending the ground ---
    # Sourcing consumes the OSM city ledger permanently, so a run that cannot
    # possibly send must not start. Everything the browser side needs (port,
    # session, right account, no cooldown) is knowable now, in ~5 seconds.
    # Skipped on --plan/--dry-run, which never reach a browser.
    if li_on and not PLAN and not a.dry_run:
        rc = sh(["node", "linkedin/scripts/preflight.js"], "Stage 0 LinkedIn preflight",
                tolerate=(2,))
        if rc == 2:
            die("Stage 0 LinkedIn preflight",
                "browser not ready — see the fix line above (nothing was sourced, "
                "no ground consumed; re-fire once it is fixed)")

    for f in run.glob("candidates-batch-*.txt"):
        f.unlink()

    # per-run enrich_cap.txt overrides ENRICH_MAX_LEADS; an explicit target wins
    env_cap = read_cfg(run, "enrich_cap.txt")
    if env_cap.isdigit():
        os.environ["ENRICH_MAX_LEADS"] = env_cap

    def die_no_supply(stage: str, cause: str) -> int:
        """Halt a run that has nothing left to source (final pass only).

        REMOVED 2026-08-05 (user directive): the qualified-pending backlog used to
        rescue this path by replaying leads that qualified on an earlier fire but
        were never contacted. Measured across every run on disk, 92% of those leads
        (3,760 / 4,095) died for ONE reason — their decision-maker was found but no
        direct personal email exists to publish. That does not change on a retry, so
        the backlog replayed permanently unreachable businesses at full agent cost:
        three fires on 2026-08-05 each spent ~2h and 208 name-finder agents on the
        SAME 208 hotels, converting 38 / 34 / 34.

        A dry campaign now halts here and says what actually adds supply, instead of
        looking productive while re-researching leads it can never reach."""
        die(stage,
            f"{cause}. This campaign's sourcing ground is exhausted — add new "
            f"places.txt rows (more cities), widen the sourcing.json selector, or "
            f"declare a `places` source. (The qualified-pending backlog that used "
            f"to carry this path was removed: those leads have no reachable direct "
            f"email and re-running them produced nothing.)")
        return 0        # unreachable; die() exits

    def funnel(final: bool) -> int:
        """Stages 3-5.3 over everything sourced so far; returns qualified count.
        Safe to re-run per wave: merge's sourced-ledger writes are idempotent for
        the current run slug, fetch skips already-fetched files, extract/qualify
        recompute. Between waves (final=False) an empty result is an expected
        keep-sourcing state, not kill-on-fallback; on the last pass it halts."""
        sh([PY, "tools/scripts/merge_candidates.py", "--run-dir", str(run),
            "--sent-log", SENT_LOG], "Stage 3 merge")
        merged = count_lines(run / "candidates-all.txt")
        if not PLAN and merged == 0:
            if final:
                return die_no_supply("Stage 3 merge", "0 candidates after dedup")
            print("  NOTE: 0 candidates so far; sourcing next wave.")
            return 0
        if not PLAN and merged < 50 and final:
            print(f"  NOTE: only {merged} merged candidates (<50).")
        sh(["bash", "tools/scripts/fetch_html.sh", str(run)], "Stage 4 fetch")
        ecmd = [PY, "tools/scripts/extract_leads.py", "--run-dir", str(run),
                "--sent-log", SENT_LOG]
        # A run whose only channel is not email must not have its leads deleted
        # at extract for lacking a mailbox. WhatsApp reaches a MOBILE, so a
        # business with a phone and no published address is a perfectly good
        # lead — but extract_leads.py drops it outright without this flag, which
        # meant the wa_only rescue at Stage 5.5 never saw those leads at all.
        if li_only or wa_only:
            ecmd.append("--allow-no-email")
        sh(ecmd, "Stage 5 extract")
        if not PLAN and count_lines(run / "leads-extracted.json") == 0:
            if final:
                return die_no_supply("Stage 5 extract", "0 leads extracted")
            print("  NOTE: 0 leads extracted so far; sourcing next wave.")
            return 0
        qcmd = [PY, "tools/scripts/qualify_leads.py", "--run-dir", str(run)]
        if li_only:
            qcmd.append("--linkedin-run")
        elif wa_only:
            # Same reasoning as li_only. Without this a WhatsApp-only lead whose
            # only found address is freemail is dropped "freemail-only" AND
            # written to the PERMANENT disqualified-log, which blocks that
            # business from every future run on every channel — over a mailbox
            # this run was never going to use. qualify_leads.py's own comment
            # says an email verdict "is not evidence about the business" and
            # "must never reach the PERMANENT ledger"; that protection existed
            # only for LinkedIn.
            qcmd.append("--no-email-channel")
        if needed_q:
            qcmd += ["--cap", str(needed_q)]   # the target-derived need IS the cap (top-N by fit)
        # exit 7 = nothing sourced this run qualified. Tolerated between waves
        # ("keep sourcing"); on the final pass it halts the run below.
        rc = sh(qcmd, "Stage 5.3 qualify", tolerate=(7,))
        if not rc:
            return count_lines(run / "leads-qualified.json")
        # The qualify gate rejected everything sourced this run (exit 7).
        if final:
            return die_no_supply("Stage 5.3 qualify", "0 leads survived the qualify gate")
        return 0

    # --- Stage 2: the ONE sourcing method on the main chain ---
    # Deterministic OSM enumeration, for EVERY campaign. There is no method
    # switch and no per-campaign sourcing branch: the only things that differ
    # run to run are WHERE it looks (places.txt) and WHO it looks for
    # (selector/vertical). Zero LLM tokens, exhaustive per place, and the city
    # ledger guarantees each fire opens fresh ground.
    max_cand = int(sourcing.get("max_per_run", 350))
    if target:
        # deterministic enumeration: over-source ~3 candidates per needed
        # qualified lead to absorb dedup/country/email/score attrition
        max_cand = max(max_cand, needed_q * 3)
    # A multi-vertical campaign sweeps its targets in RANKED order, each with its
    # own share of the candidate cap and its own batch-file prefix. The city
    # ledger is keyed per-vertical, so the verticals never block each other and
    # each one keeps its own record of exhausted ground.
    resolve_on = bool(sourcing.get("resolve_domains"))
    sources = sourcing["sources"]
    units = []          # flattened (source, target-or-None) work list
    for s in sources:
        if s["type"] == "map":
            units += [(s, t) for t in (s.get("targets") or sourcing["targets"])]
        else:
            units.append((s, None))
    if not units:
        die("pre-flight", "sourcing.json declares no sources to run")
    # Equal split by default; a source may carry its own `max_candidates` to
    # override its share. Overture is the case that needs it: free, no request
    # ceiling, half its rows domain-carrying — and it obeys the share exactly
    # (gcc-receptionist 2026-08-31: Riyadh held 1,214 POIs, Overture stopped at
    # its 40), while gmaps overshoots it by a whole city regardless.
    share = max(1, max_cand // len(units))
    dry_sources: list[str] = []      # sources that reported no fresh ground (rc=8)

    # Build every unit's command FIRST; the pool below only runs them.
    jobs: list[dict] = []            # {i, label, label_bit, lane, cmd}
    for i, (s, t) in enumerate(units, 1):
        if t:
            label_bit = t["vertical"]
        elif s["type"] in ("places", "overture", "gmaps"):
            # one process sweeps all of this source's targets
            label_bit = "-".join(sorted({x.get("vertical", "")
                                         for x in (s.get("targets") or [])})) or s["type"]
        else:
            label_bit = s.get("vertical") or s["type"]
        label = ("Stage 2 source" if len(units) == 1 else
                 f"Stage 2 source [{i}/{len(units)}] {label_bit}")
        if s["type"] == "map":
            cmd = [PY, "tools/scripts/source_overpass.py", "--run-dir", str(run),
                   "--selector", t["selector"],
                   "--vertical", t["vertical"],
                   "--batch-prefix", f"{i:02d}-{t['vertical']}",
                   "--max-candidates", str(s.get("max_candidates") or share)]
            if resolve_on:
                cmd += ["--capture-unresolved"]
            if s.get("region") or sourcing.get("region"):
                cmd += ["--region", str(s.get("region") or sourcing["region"])]
            else:
                cmd += ["--auto"]
        elif s["type"] == "places":
            # Google Places API (New), adaptive quadtree. Same places.txt, same
            # candidate contract; reaches the businesses OSM has no premises for.
            cmd = [PY, "tools/scripts/source_places.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}",
                   "--max-candidates", str(s.get("max_candidates") or share)]
        elif s["type"] == "overture":
            # Bulk open POI data (Overture Maps), queried in place over S3.
            # Same places.txt, same candidate contract — but no request ceiling
            # and no per-call cost, and ~half its rows already carry the
            # business's own domain, so they skip Stage 2.5 entirely.
            cmd = [PY, "tools/scripts/source_overture.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}",
                   "--max-candidates", str(s.get("max_candidates") or share)]
        elif s["type"] == "gmaps":
            # Google Maps enumerated directly via the gosom scraper binary.
            # Same places.txt, same candidate contract — but it returns the
            # business's own website AND its Maps category in ONE pass, so it
            # skips Stage 2.5 entirely and filters off-ICP rows before they cost
            # a fetch. Measured 2026-08-18: 95% of rows carry a domain, against
            # 0% from the Places Pro tier (which requests no website by design)
            # and ~27% surviving Stage 2.5's serial resolve.
            cmd = [PY, "tools/scripts/source_gmaps.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}",
                   "--max-candidates", str(s.get("max_candidates") or share)]
        else:   # directory: any paginated listing on the open web
            cmd = [PY, "tools/scripts/source_directory.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}"]
        # Record which batch prefix this source owns, BEFORE it runs. Without
        # this, source attribution is unrecoverable: cleanup_run_artifacts()
        # deletes candidates-batch-* after a successful send, so on 2026-08-18
        # the only runs that still carried attribution were the ABORTED ones —
        # making "which source is worth keeping?" unanswerable from the archive.
        # This file is not matched by any cleanup pattern, so it survives.
        # Written here, BEFORE the pool starts, so it has exactly one writer.
        try:
            _pfx = cmd[cmd.index("--batch-prefix") + 1]
            _smap = run / "source-map.json"
            _cur = json.loads(_smap.read_text()) if _smap.exists() else {}
            _cur[_pfx] = {"type": s["type"], "vertical": label_bit}
            _smap.write_text(json.dumps(_cur, indent=2))
        except (ValueError, IndexError, OSError):
            pass    # attribution is diagnostics, never a reason to fail a fire
        # LANES: units that could touch the same city-ledger key never run at
        # the same time. Each adapter reads its key's rows once at start and
        # appends only its own key (`<vertical>` for OSM, `gmaps:<v>`,
        # `overture:<v>`, the places key), so different keys are independent;
        # two units on ONE key would both read the ledger before either
        # appended and sweep the same cities twice. All OSM units also share a
        # single lane because Overpass grants ~2 concurrent slots per IP.
        lane = "overpass" if s["type"] == "map" else f"{s['type']}:{label_bit}"
        # Short display name for the status line and the abort/dry lists: the
        # vertical for an OSM unit, the source type otherwise ("gmaps,
        # overture"), since label_bit is "clinic" for four different units of
        # a typical fixture.
        disp = t["vertical"] if t else s["type"]
        if any(j["disp"] == disp for j in jobs):
            disp = f"{s['type']}:{label_bit}"
        jobs.append({"i": i, "label": label, "label_bit": label_bit, "disp": disp,
                     "lane": lane, "cmd": cmd})

    # --- run the source units CONCURRENTLY (2026-09-02) ---
    # The loop used to be serial. Measured on 2026-09-02-gcc-receptionist: the
    # gmaps unit (900 s per-city timeout, one city at a time) held the run for
    # ~28 min while overture, map and places sat queued behind it, though none
    # of them shares anything with it: each unit writes its OWN
    # candidates-batch-<prefix>-* / unresolved-<prefix>-* files (the prefix
    # carries the unit index), source_gmaps' queries.txt lives in a tempdir,
    # status.txt and source-map.json are written only by this process, and the
    # city ledger is append-only per key (lanes above). What DID need care is
    # stdout: each unit's output is streamed through a pipe and prefixed with
    # its label under one lock (PYTHONUNBUFFERED so progress lines arrive live).
    #
    # rc=8 still means "this source has no fresh ground left" — an expected end
    # state, not a fallback. One dry source must not kill a run whose other
    # sources still have ground; if EVERY source is dry the run halts at the
    # Stage 3 "0 candidates after dedup" gate. Any OTHER non-zero code is a
    # real crash and halts the fire at once: running units are terminated (a
    # unit ledgers a city only after finishing it, so a kill retires no
    # ground) and unstarted ones are dropped — and the abort line names both,
    # because on 2026-08-11-au-trades a crash at [2/4] silently pre-empted the
    # source at [4/4] that held the campaign's whole supply.
    source_workers = max(1, int(os.environ.get("SOURCE_WORKERS", "4")))

    def _run_sources(js: list[dict]) -> tuple[dict[int, int], set[int]]:
        """Run every job, lanes in parallel, units within a lane in order.
        Returns ({unit index: rc}, {unit indexes terminated after a crash})."""
        n = len(js)
        lanes: dict[str, list[dict]] = {}
        for j in js:
            lanes.setdefault(j["lane"], []).append(j)
        if PLAN:
            for j in js:
                print(f"\n=== {j['label']}: {' '.join(j['cmd'])}")
            print(f"  [plan] {n} source unit(s) on {len(lanes)} lane(s), up to "
                  f"{source_workers} concurrent (SOURCE_WORKERS); OSM units share one lane.")
            return {j["i"]: 0 for j in js}, set()
        rcs: dict[int, int] = {}
        killed: set[int] = set()
        running: set[str] = set()
        procs: dict[int, subprocess.Popen] = {}
        lock = threading.Lock()
        abort = threading.Event()
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}

        def _heartbeat():
            with lock:
                bits = ", ".join(sorted(running)) or "-"
                done = len(rcs)
            status(f"Stage 2 source — running: {bits} · done {done}/{n}")

        def _one(j: dict) -> None:
            with lock:
                running.add(j["disp"])
            _heartbeat()
            with _PRINT_LOCK:
                print(f"\n=== {j['label']}: {' '.join(j['cmd'])}", flush=True)
            p = subprocess.Popen(j["cmd"], cwd=str(PROJECT), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace")
            with lock:
                procs[j["i"]] = p
            tag = f"[{j['disp']}] "
            assert p.stdout is not None
            for line in p.stdout:
                with _PRINT_LOCK:
                    sys.stdout.write(tag + line)
                    sys.stdout.flush()
            rc = p.wait()
            with lock:
                running.discard(j["disp"])
                procs.pop(j["i"], None)
                rcs[j["i"]] = rc
                crashed = rc not in (0, 8) and j["i"] not in killed
                if crashed:
                    abort.set()
                    for k, q in procs.items():
                        if q.poll() is None:      # still running -> stop it
                            killed.add(k)
                            q.terminate()
            _heartbeat()

        def _lane(lane_jobs: list[dict]) -> None:
            for j in lane_jobs:
                if abort.is_set():
                    return
                _one(j)

        with concurrent.futures.ThreadPoolExecutor(max_workers=source_workers) as ex:
            list(ex.map(_lane, lanes.values()))
        return rcs, killed

    rcs, killed = _run_sources(jobs)
    for j in jobs:
        rc = rcs.get(j["i"])
        if rc == 8:
            dry_sources.append(j["disp"])
        elif rc not in (0, None) and j["i"] not in killed:
            stopped = [f"[{x['i']}/{len(units)}] {x['disp']}" for x in jobs
                       if x["i"] in killed and rcs.get(x["i"]) not in (0, 8)]
            never = [f"[{x['i']}/{len(units)}] {x['disp']}" for x in jobs if x["i"] not in rcs]
            die(j["label"],
                f"command exited {rc}. NOT a dry-source state (that is rc=8) — this "
                f"source crashed. Because of it: terminated mid-run: "
                f"{', '.join(stopped) or 'none'}; never started: "
                f"{', '.join(never) or 'none'}", rc)
    status(f"Stage 2 source — done {len(rcs)}/{len(units)}"
           + (f" (dry: {', '.join(dry_sources)})" if dry_sources else ""))

    # --- Stage 2.5: name -> own domain, VERIFIED ---
    # Opt-in per branch (`"resolve_domains": true`). OSM enumerates the ICP far
    # better than it records websites, so in weakly-mapped markets most elements
    # are name-only and Stage 2 alone starves. This recovers them deterministically
    # (search + fetch + name-token proof, no LLM). Unproven domains are DROPPED,
    # never guessed: a wrong domain pitches business A at business B and burns it
    # in the sent-log forever.
    #
    # RUN IT CONCURRENTLY WITH A PRE-FETCH (2026-08-19). The resolver only ever
    # ADDS candidates (candidates-batch-resolved-*); every domain the gmaps /
    # overture / map sources delivered is final the moment Stage 2 ends —
    # measured on 2026-08-19-au-trades that was 551 of the eventual 722, sitting
    # idle for the resolver's whole runtime. merge and fetch are re-runnable by
    # design (append_sourced dedupes per run slug; fetch skips files already on
    # disk — the same properties funnel() already relies on between waves), so
    # the pre-fetch costs nothing extra: funnel() re-merges afterwards, picks up
    # the resolved batch, and its fetch pass downloads ONLY the new domains.
    # Failure semantics are unchanged in both directions: a resolver crash still
    # kills the run (checked at wait()), and a pre-fetch crash kills the
    # resolver before dying (kill-on-fallback, no orphaned process).
    # Logs from the two interleave; each line is still stage-prefixed.
    if resolve_on and not PLAN:
        rcmd = [PY, "tools/scripts/resolve_domains.py", "--run-dir", str(run),
                "--config", json.dumps(sourcing)]
        print(f"\n=== Stage 2.5 resolve domains (background, overlapped with pre-fetch): "
              f"{' '.join(rcmd)}")
        status("Stage 2.5 resolve + Stage 4 pre-fetch — running in parallel")
        rproc = subprocess.Popen(rcmd, cwd=str(PROJECT))
        prefetch_ok = False
        try:
            sh([PY, "tools/scripts/merge_candidates.py", "--run-dir", str(run),
                "--sent-log", SENT_LOG], "Stage 3 pre-merge (domain-carrying sources)")
            if count_lines(run / "candidates-all.txt"):
                sh(["bash", "tools/scripts/fetch_html.sh", str(run)],
                   "Stage 4 pre-fetch (parallel with resolve)")
            else:
                print("  pre-fetch skipped: 0 domain-carrying candidates yet "
                      "(everything rides on the resolver).")
            prefetch_ok = True
        finally:
            if not prefetch_ok:
                rproc.terminate()
            rrc = rproc.wait()
        if rrc != 0:
            die("Stage 2.5 resolve domains", f"command exited {rrc}", rrc)
        status("Stage 2.5 resolve domains — done")
    elif resolve_on:
        sh([PY, "tools/scripts/resolve_domains.py", "--run-dir", str(run),
            "--config", json.dumps(sourcing)], "Stage 2.5 resolve domains")

    if dry_sources:
        msg = (f"NO FRESH GROUND from source(s): {', '.join(dry_sources)} "
               f"({len(dry_sources)}/{len(units)} dry). See the source's message "
               f"above for what actually adds new leads.")
        print(f"  {msg}")
        status(msg)

    qualified = funnel(final=True)
    if target and not PLAN and qualified < needed_q:
        msg = (f"TARGET SHORTFALL: only {qualified} qualified (wanted {needed_q} "
               f"for ~{target} sends) after sweeping the un-ledgered places for "
               f"verticals '{src_label}'. Add rows to this fixture's "
               f"places.txt to give the sweep more ground.")
        print(f"  {msg}")
        status(msg)
    print(f"  qualified: {qualified}")

    # --- Step 6: per-lead path ---
    if li_only:
        # Nothing here applies: the "contact" for this run is a LinkedIn profile,
        # resolved at Stage 8.6 by walking the qualified companies to their people.
        print("  LinkedIn-only run: skipping decision-maker email enrichment "
              "and the email drafter (Stage 8.6 resolves the person instead).")
    elif combined_custom:
        # ONE lead-writer per lead = find + write in a single pass (email-only custom).
        sh([PY, "tools/scripts/draft_lead_custom.py", "--phase", "prep",
            "--run-dir", str(run), *WD], "Stage 6.5C prep")
        batches = sorted(work.glob("lead-batch-*.txt"))
        if PLAN:
            print(f"  [plan] lead-writer batches: {work}/lead-batch-NNN.txt (one agent each)")
        prompts = [b.read_text() for b in batches]   # lead-writer reads scraped pages itself; keep inline (unchanged behavior)
        fan_out("lead-writer", prompts, "Stage 6.5C lead-writer", a.max_workers)
        sh([PY, "tools/scripts/draft_lead_custom.py", "--phase", "merge",
            "--run-dir", str(run), *WD], "Stage 6.5C merge")
    else:
        # 5.5 enrich (name-finder) — phone only when WhatsApp on.
        prep = [PY, "tools/scripts/enrich_contact_person.py", "--phase", "prep",
                "--run-dir", str(run), *WD]
        if wa_on:
            prep.append("--enrich-phone")
        sh(prep, "Stage 5.5 enrich prep")
        ebatches = sorted(work.glob("enrich-batch-*.txt"))
        if PLAN:
            print(f"  [plan] name-finder batches: {work}/enrich-batch-NNN.txt (one agent each)")
            _prov = os.environ.get("RESEARCH_PROVIDER", "hybrid").strip().lower()
            if _prov == "hybrid":
                try:
                    import perplexity_research as _pxr
                    _key = "present" if _pxr.api_key() else "MISSING"
                    _model = _pxr.DEFAULT_MODEL
                except SystemExit:
                    _key, _model = "MISSING (OPEN_ROUTER_API_KEY not in project/.env)", "perplexity/sonar"
                except Exception as _e:
                    _key, _model = f"unavailable ({_e})", "perplexity/sonar"
                print(f"  [plan] Stage 5.5a naming: {_model}, one request per lead, writes "
                      f"KnownDecisionMaker into each batch (API key: {_key})")
                print(f"  [plan] Stage 5.5b name-finder: identity supplied, searches spent "
                      f"on the ADDRESS; KnownEmailFormat replayed from "
                      f"vault/lead-outreach/email-formats.txt when the domain is known")
            else:
                print(f"  [plan] research provider: {_prov}")

        # name-finder reads its own file (validated path-based dispatch) -> orchestrator stays lean
        def _nf_prompt(b: Path) -> str:
            return (f"Your input file: {b.resolve()}\n"
                    "Read it, then follow your name-finder instructions and write "
                    "your JSON to the OutputFile named inside it.")

        # ONE pass per lead, full tool set. The Read+Write-only "tier 1" that
        # ran ahead of it (2026-08-26 .. 2026-09-01) was removed 2026-09-02 on
        # measurement: it closed 11/337 leads on 2026-09-02-gcc-receptionist
        # (3.3%) and 0/64 on eu-hotels, for 337 extra Haiku sessions, 10.3M
        # cache-write + 40M cache-read + 0.69M output tokens and 11.7 minutes of
        # wall clock — a net cost, not a saving. The phone ladder is still an
        # OPTIONAL block, shipped only when EnrichPhone is on.
        # RESEARCH PROVIDER (2026-09-03). "perplexity" answers the same question
        # in ONE search-grounded request per lead instead of a 10-call agent
        # loop that re-reads a ~15k prefix every turn (~220k tokens/lead). It
        # writes the SAME enrich-out-NNN.json, so the merge, the direct-email
        # gate and the SMTP rescue below are untouched. See
        # perplexity_research.py for the measured split: it identifies the
        # decision-maker well and must NOT construct addresses.
        # DEFAULT IS `hybrid`, and the split is measured, not assumed. On the 133
        # identical batch files of 2026-09-04-eu-hotels: perplexity named 106 vs
        # the agent's 91, the agent closed 39 addresses vs perplexity's 5. So
        # perplexity names and the agent addresses. `perplexity` (naming only,
        # no agent) and `agent` (the old fan-out) remain available; the former
        # was scored end-to-end against the 8 labelled winners of
        # 2026-09-02-eu-hotels-2 (perplexity/sonar, real batch files with
        # SitePages + harvested addresses, then the SAME merge + SMTP rescue):
        #   agent      8/8 sendable
        #   perplexity 1/8 closed outright (verbatim, name-linked)
        #            + 5/8 named -> SMTP rescue -> 3 had an address path
        #              (1 RCPT-verified, 2 catch-all needing site-format
        #               construction that may still fail)
        #            = 2-4 of 8, i.e. 25-50% of the yield
        # Losing a lead is not free: a no_match drop retires that domain in
        # disqualified-log.txt for NO_EMAIL_RETRY_DAYS (90), so an unvalidated
        # provider swap burns ground the next fire cannot re-source. Flip it with
        # RESEARCH_PROVIDER=perplexity once a --dry-run fire shows the survival
        # rate holding; compare `survived` in enrich-summary.json.
        provider = os.environ.get("RESEARCH_PROVIDER", "hybrid").strip().lower()
        # HYBRID (default): perplexity names the decision-maker, the agent spends
        # its searches on the address. Each side does the half it measurably
        # wins — see annotate_batches() for the 133-lead head-to-head. Falls
        # back to a plain agent fan-out if the key or the API is unavailable,
        # because a naming pass failing must never cost the run its leads.
        if ebatches and provider == "hybrid" and not PLAN:
            try:
                import perplexity_research as pxr
                status(f"Stage 5.5 naming — perplexity ({pxr.DEFAULT_MODEL}), "
                       f"{len(ebatches)} lead(s)")
                print(f"\n=== Stage 5.5a naming: {pxr.DEFAULT_MODEL} x{len(ebatches)} "
                      f"(one request per lead, no agent loop)", flush=True)
                s = pxr.annotate_batches(work, max_workers=a.max_workers)
                print(f"  naming: {s['named']}/{s['n']} decision-maker(s) named in "
                      f"{s['elapsed']:.0f}s for ${s['cost']:.4f} "
                      f"({s.get('tier1_named', 0)} by tier-1 from site text, "
                      f"{s.get('sonar_requests', s['n'])} sonar search(es)). The "
                      f"name-finders below now spend their searches on the ADDRESS, "
                      f"not the person.", flush=True)
                # Durable proof in the run folder. This file is how a later
                # session confirms the naming pass actually ran, instead of
                # inferring it from the code — the standing rule in CLAUDE.md.
                (run / "research-provider.json").write_text(json.dumps({
                    "provider": "hybrid", "model": pxr.DEFAULT_MODEL,
                    "leads": s["n"], "named": s["named"],
                    "cost_usd": round(s["cost"], 4),
                    "elapsed_s": round(s["elapsed"], 1),
                    # The tier-1/sonar split, so a later session can confirm the
                    # cheap pass ran and what it saved instead of inferring it
                    # from the code. sonar_requests x ~$0.00616 is the bill.
                    "tier1_model": pxr.TIER1_MODEL if pxr.TIER1_ENABLED else None,
                    "tier1_named": s.get("tier1_named", 0),
                    "tier1_cost_usd": round(s.get("tier1_cost", 0.0), 4),
                    "sonar_requests": s.get("sonar_requests", s["n"]),
                }, indent=2) + "\n")
            except Exception as e:
                # Never fail the run: the agents can still resolve identity. But
                # say so LOUDLY and leave it in the artifacts, so a silent
                # fallback is never mistaken for a naming pass that ran.
                print(f"  WARN: perplexity naming pass unavailable ({e}); the "
                      f"name-finders will resolve identity themselves as before.",
                      flush=True)
                status(f"Stage 5.5 naming FELL BACK to agents — {e}")
                try:
                    (run / "research-provider.json").write_text(json.dumps({
                        "provider": "agent-fallback", "error": str(e)[:300]}, indent=2) + "\n")
                except Exception:
                    pass
            fan_out("name-finder", [_nf_prompt(b) for b in ebatches],
                    "Stage 5.5 name-finder", a.max_workers,
                    include={"phone"} if wa_on else None)
        elif ebatches and provider == "perplexity" and not PLAN:
            import perplexity_research as pxr
            status(f"Stage 5.5 research — perplexity ({pxr.DEFAULT_MODEL}), "
                   f"{len(ebatches)} lead(s)")
            print(f"\n=== Stage 5.5 research: perplexity/{pxr.DEFAULT_MODEL} "
                  f"x{len(ebatches)} (one request per lead)", flush=True)
            try:
                s = pxr.run_batches(work, max_workers=a.max_workers)
                print(f"  perplexity: {s['n']} lead(s) in {s['elapsed']:.0f}s — "
                      f"{s.get('named', 0)} decision-maker(s) named, {s['found']} with a "
                      f"verbatim email, ${s['cost']:.4f}. Addresses for the rest are the "
                      f"SMTP rescue's job in the merge.", flush=True)
            except SystemExit as e:
                die("Stage 5.5 research (perplexity)", str(e))
        elif ebatches:
            fan_out("name-finder", [_nf_prompt(b) for b in ebatches],
                    "Stage 5.5 name-finder", a.max_workers,
                    include={"phone"} if wa_on else None)
        merge = [PY, "tools/scripts/enrich_contact_person.py", "--phase", "merge",
                 "--run-dir", str(run), *WD]
        # A WhatsApp-ONLY run must always keep phone-reachable leads: requiring a
        # direct EMAIL on a channel that sends none is the gate that used to
        # empty these runs. `wa_fallback` covers the explicit
        # "whatsapp-fallback" channel; wa_only covers a plain ["whatsapp"].
        if wa_fallback or wa_only:
            merge.append("--wa-fallback")
        sh(merge, "Stage 5.5 enrich merge")
        if not PLAN and count_lines(run / "leads-with-contact.json") == 0 and not li_email_lookup:
            die("Stage 5.5", "0 leads with a resolved decision-maker"
                             + ("" if wa_only else " + direct email"))

        # --- Stage 5.6 (opt-in `linkedin-email-lookup`) ---------------------
        # Take the leads Stage 5.5 dropped for "name found, no email" and give
        # each one a li-finder pass (reusing the name, not a fresh search) plus
        # a LinkedIn Contact Info check. Survivors rejoin leads-with-contact.json
        # through the exact same direct-email gate as everyone else.
        if li_email_lookup and email_on and not PLAN:
            dropped_path = run / "leads-dropped.json"
            candidates = []
            if dropped_path.exists():
                for line in dropped_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("drop_stage") == "no_match" and (d.get("found_first_name") or d.get("found_last_name")):
                        candidates.append(d)
            if not candidates:
                print("  linkedin-email-lookup: no name-found-but-no-email leads to rescue.")
            else:
                qpath = run / "leads-qualified.json"
                epath = qpath if qpath.exists() else (run / "leads-extracted.json")
                extracted_by_id = {}
                if epath.exists():
                    for line in epath.read_text().splitlines():
                        if line.strip():
                            l = json.loads(line)
                            extracted_by_id[l["lead_id"]] = l

                # Per-agent files: the work dir, like every other fan-out.
                liout = work / "li-email-out"
                for f in work.glob("li-email-batch-*.txt"):
                    f.unlink()
                if liout.exists():
                    for f in liout.glob("*.json"):
                        f.unlink()
                liout.mkdir(parents=True, exist_ok=True)

                index_to_leadid = {}
                batches = []
                for i, d in enumerate(candidates, 1):
                    lead = extracted_by_id.get(d["lead_id"])
                    if not lead:
                        continue
                    country = COUNTRY_NAMES_ISO.get(lead.get("country_code", ""), lead.get("country_code", ""))
                    bpath = work / f"li-email-batch-{i:03d}.txt"
                    bpath.write_text(
                        f"OutputFile: {(liout / f'{i:03d}.json').resolve()}\n"
                        f"Company:    {lead.get('name', '')}\n"
                        f"City:       \n"
                        f"Country:    {country}\n"
                        f"Vertical:   {lead.get('vertical', '')}\n"
                        f"Website:    {lead.get('website', '')}\n")
                    index_to_leadid[f"{i:03d}"] = d["lead_id"]
                    batches.append(bpath)

                if not batches:
                    print("  linkedin-email-lookup: candidates had no matching extracted-lead record; skipping.")
                else:
                    prompts = [f"Your input file: {b.resolve()}\n"
                               "Read it, then follow your li-finder instructions and write your JSON "
                               "to the OutputFile named inside it."
                               for b in batches]
                    fan_out("li-finder", prompts, "Stage 5.6 li-finder (email recovery)", a.max_workers)

                    profiles = []
                    for f in sorted(liout.glob("*.json")):
                        try:
                            r = json.loads(f.read_text())
                        except Exception:
                            continue
                        lead_id = index_to_leadid.get(f.stem)
                        url = canonical_profile_url(r.get("profile_url"))
                        if lead_id and r.get("found") and url:
                            profiles.append({"lead_id": lead_id, "url": url})
                    print(f"  Stage 5.6 li-finder: {len(profiles)}/{len(batches)} profile(s) resolved")

                    if not profiles:
                        print("  linkedin-email-lookup: no profiles resolved; nothing to check on LinkedIn.")
                    else:
                        li_in = run / "li-email-lookup-in.json"
                        li_out = run / "li-email-lookup-out.json"
                        li_in.write_text(json.dumps(profiles))
                        sh(["node", "linkedin/scripts/lookup_contact_email.js",
                            "--in", str(li_in), "--out", str(li_out)],
                           "Stage 5.6 LinkedIn contact-info lookup", tolerate=(10,))
                        sh([PY, "tools/scripts/enrich_contact_person.py", "--phase", "rescue",
                            "--run-dir", str(run), *WD, "--linkedin-results", str(li_out)],
                           "Stage 5.6 rescue merge")
            if count_lines(run / "leads-with-contact.json") == 0:
                die("Stage 5.5/5.6", "0 leads with a resolved decision-maker + direct email, "
                                     "even after the LinkedIn Contact Info rescue")

        # Step 7 draft
        if wa_only:
            # draft_whatsapp_custom.py reads leads-with-contact.json directly, so
            # an email drafter here produces a file with no consumer.
            print("  WhatsApp-only run: skipping the email drafter "
                  "(Stage 8.5 writes the messages from leads-with-contact.json).")
        elif draft_mode == "custom":   # custom + WhatsApp path (gap-writer)
            sh([PY, "tools/scripts/draft_custom.py", "--phase", "prep",
                "--run-dir", str(run), *WD], "Stage 6 gap prep")
            gbatches = sorted(work.glob("gap-batch-*.txt"))
            if PLAN:
                print(f"  [plan] gap-writer batches: {work}/gap-batch-NNN.txt (one agent each)")
            prompts = [b.read_text() for b in gbatches]   # gap-writer reads pages itself; inline unchanged
            fan_out("gap-writer", prompts, "Stage 6 gap-writer", a.max_workers)
            sh([PY, "tools/scripts/draft_custom.py", "--phase", "merge",
                "--run-dir", str(run), *WD], "Stage 6 gap merge")
        else:                        # template
            sh([PY, "tools/scripts/draft_emails.py", "--run-dir", str(run)],
               "Stage 6 template draft")

    drafted = count_lines(run / "emails-drafted.json")
    print(f"  drafted: {drafted}")
    if not PLAN and email_on and drafted == 0:
        if wa_on:
            # WhatsApp may still carry the run (fallback leads have no email);
            # the combined zero-output check happens after Stage 8.5.
            print("  WARN: 0 emails drafted; relying on the WhatsApp channel.")
        else:
            die("Stage 6 draft", "0 emails drafted")

    # --- Step 8: send + persist (email) ---
    if email_on and not a.dry_run:
        # Refresh the dead-letter suppression list from Brevo FIRST — sending
        # while the bounce-list is stale re-mails known-dead addresses and
        # burns sender reputation. Aborts the run if Brevo is unreachable.
        sh([PY, "tools/scripts/sync_brevo_events.py"], "Stage 7 bounce-sync")
        cap = os.environ.get("MAX_EMAILS_PER_RUN", "1000")
        if target:
            cap = str(min(int(cap), target))   # outreach exactly the asked-for volume
        # rc=6 means "some emails failed at the Brevo API". It must NOT skip
        # Stage 8. On a PARTIAL failure (batch 1 of 3 sends, batch 2 fails)
        # Brevo really did deliver those first messages, and if we abort before
        # persisting them they never reach sent-log.md — so a later fire happily
        # re-contacts people we already mailed. That is worse than the reporting
        # bug rc=6 was added to fix, and it breaks CLAUDE.md's flat guarantee
        # that "every email is logged to sent-log.md after the send call fires".
        # persist_sent_log.py only writes rows with result == "sent", so running
        # it here records exactly what went out and nothing more. THEN we fail
        # the run.
        send_rc = sh([PY, "tools/scripts/send_batch_brevo.py", "--run-dir", str(run),
                      "--send", "--cap", cap], "Stage 7 send", tolerate=(6,))
        sh([PY, "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
            "--sent-log", SENT_LOG], "Stage 8 persist")
        if send_rc == 6:
            die("Stage 7 send", "Brevo rejected one or more emails — the messages that "
                                "DID send are persisted to the sent-log, the rest stay "
                                "uncontacted and can be re-sent once the cause is fixed "
                                "(see the 'error' field in emails-sent.jsonl)", 6)
    elif email_on:
        print("\n[DRY-RUN] drafts written; send + persist skipped.")

    # --- Step 8.5: WhatsApp (opt-in) ---
    wa_drafted = 0
    if wa_on:
        if draft_mode == "custom":
            # custom path: wa-writer agents compose per-company messages.
            sh([PY, "tools/scripts/draft_whatsapp_custom.py", "--phase", "prep",
                "--run-dir", str(run), *WD], "Stage 8.5 WA prep")
            wbatches = sorted(work.glob("wa-batch-*.txt"))
            if PLAN:
                print(f"  [plan] wa-writer batches: {work}/wa-batch-NNN.txt (one agent each)")
            if wbatches:
                prompts = [b.read_text() for b in wbatches]
                fan_out("wa-writer", prompts, "Stage 8.5 wa-writer", a.max_workers)
                sh([PY, "tools/scripts/draft_whatsapp_custom.py", "--phase", "merge",
                    "--run-dir", str(run), *WD], "Stage 8.5 WA merge")
        else:
            # template path: deterministic drafter renders pitch.json's
            # wa_body_template. --fallback-only keeps email primary: only leads
            # whose direct email failed verification go out on WhatsApp.
            wa_cmd = [PY, "tools/scripts/draft_whatsapp.py", "--run-dir", str(run)]
            if wa_send_fallback_only:
                wa_cmd.append("--fallback-only")
            sh(wa_cmd, "Stage 8.5 WA template draft")
        wa_drafted = count_lines(run / "whatsapp-drafted.json")
        print(f"  whatsapp drafted: {wa_drafted}")
        if wa_drafted == 0:
            print("  no WhatsApp drafts; skipping WA send.")
        elif not a.dry_run:
            # --send is required: without it send_campaign.js is a dry-run that
            # exits before connecting (this silently shipped nothing before).
            sh(["node", "bridge/send_campaign.js", "--run-dir", str(run), "--send"],
               "Stage 8.5 WA send")
            # WhatsApp sends must land in the sent-log too (idempotent —
            # previously a WhatsApp-ONLY run never persisted at all).
            sh([PY, "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
                "--sent-log", SENT_LOG], "Stage 8.5 WA persist")
        else:
            print("[DRY-RUN] WhatsApp drafts written; send skipped.")
    # --- Step 8.6: LinkedIn (opt-in arm of the same run) ---
    # Same sourcing/merge/qualify as any run; only the outreach surface differs.
    # The chain PREPARES and QUEUES here but does not send: LinkedIn drips
    # 8-25 actions a day inside a business-hours window, so the send is a
    # separate, paced process (linkedin/send/*.js), not part of a fire.
    li_drafted = 0
    if li_on:
        sh([PY, "tools/scripts/draft_linkedin.py", "--phase", "prep",
            "--run-dir", str(run), *WD], "Stage 8.6 LI prep")
        walk = ["node", "linkedin/scripts/walk_companies.js",
                "--companies", str(run / "li-companies.json"),
                "--out", str(run / "people-raw.json"),
                # read_cfg takes the FILE name — the fixture knob is
                # li_max_per_company.txt, and asking for it without the
                # extension silently pinned this to the default forever.
                "--max-per-company", str(read_cfg(run, "li_max_per_company.txt", "3") or "3"),
                # A free account spends 8-18 invites a day, 90 a week. Harvesting
                # far past that buys nothing and spends account trust, so the walk
                # stops at a few weeks of invite supply unless the fixture says
                # otherwise.
                "--max-people", read_cfg(run, "li_max_people.txt", "60") or "60"]
        if a.dry_run:
            walk.append("--dry-run")
        sh(walk, "Stage 8.6 LI company walk")

        # --- Stage 8.6b: the SECOND route to a person (user directive) ---
        # Plenty of real GCC businesses have no LinkedIn company page at all, so
        # the walk above never reaches them even though the owner personally has
        # a profile. Those companies are resolved the ordinary way instead — web
        # search, one li-finder agent each — and the profiles it finds are then
        # harvested through exactly the same gates. Sourcing is unchanged; only
        # how we learn the profile URL differs. The outreach is still LinkedIn.
        # Size the profile-find fan-out to what the channel can actually SPEND.
        # The walk stops at --max-people because a free account sends 8-18
        # invites a day; the search route was not bounded the same way and would
        # dispatch one li-finder agent per uncovered company — up to 120 agents
        # to find people the drip could not reach for months. Ask only for the
        # shortfall, plus a small margin for the ones that come back empty.
        li_max_people = int(read_cfg(run, "li_max_people.txt", "60") or "60")
        walked = count_json(run / "people-raw.json")
        find_cap = max(0, li_max_people - walked) * 2
        sh([PY, "tools/scripts/resolve_li_profiles.py", "--phase", "prep",
            "--run-dir", str(run), *WD, "--cap", str(find_cap)],
           "Stage 8.6b LI profile-find prep")
        lif_batches = sorted(work.glob("lif-batch-*.txt"))
        if PLAN:
            print(f"  [plan] li-finder batches: {work}/lif-batch-NNN.txt -> {work}/lif-out/")
        if a.dry_run and lif_batches:
            # A dry run's walk never opens a browser and writes an empty people
            # list, so EVERY company looks like a gap and the fan-out would
            # dispatch a real li-finder agent per company — a preview that costs
            # a full run's worth of agents. The route is already proven by the
            # live path; a preview does not need to re-prove it.
            print(f"  [dry-run] skipping {len(lif_batches)} li-finder agents "
                  f"(the walk found nobody because it never ran).")
            lif_batches = []
        if lif_batches:
            prompts = [f"Your input file: {b.resolve()}\n"
                       "Read it, then follow your li-finder instructions and write your JSON "
                       "to the OutputFile named inside it."
                       for b in lif_batches]
            fan_out("li-finder", prompts, "Stage 8.6b li-finder", a.max_workers)
        rc_find = sh([PY, "tools/scripts/resolve_li_profiles.py", "--phase", "merge",
                      "--run-dir", str(run), *WD], "Stage 8.6b LI profile-find merge", tolerate=(7,))
        if rc_find == 0:
            harvest = ["node", "linkedin/scripts/walk_companies.js",
                       "--profiles", str(run / "people-found.json"),
                       "--out", str(run / "people-found-harvested.json"),
                       "--max-people", str(read_cfg(run, "li_max_people.txt", "60") or "60")]
            if a.dry_run:
                harvest.append("--dry-run")
            sh(harvest, "Stage 8.6b LI harvest found profiles")
            merge_people(run)

        li_cfg = read_cfg(run, "linkedin.json", "{}")
        sh([PY, "tools/scripts/qualify_people.py", "--run-dir", str(run),
            "--config", li_cfg], "Stage 8.6 LI qualify people", tolerate=(7,))
        # qualify_people ROUTES dormant/unreachable owners to the email chain
        # rather than dropping them. On a LinkedIn-ONLY run there is no email
        # chain in this fire, so people-to-email.json is a terminus: real,
        # correctly-titled decision-makers whose name and exact title we already
        # paid to harvest. Say so out loud — silently writing a file nobody reads
        # is how a channel looks like it qualified nobody.
        n_email = count_json(run / "people-to-email.json")
        if li_only and n_email:
            msg = (f"Stage 8.6: {n_email} right-title people were dormant or "
                   f"unreachable on LinkedIn and are parked in "
                   f"{run.name}/people-to-email.json. This run has no email arm, "
                   f"so nothing contacts them — add \"email\" to channels.json to "
                   f"pick them up on the next fire.")
            print(f"  {msg}")
            status(msg)
        sh([PY, "tools/scripts/draft_linkedin.py", "--phase", "batch",
            "--run-dir", str(run), *WD], "Stage 8.6 LI batch")
        li_batches = sorted(work.glob("li-batch-*.txt"))
        if PLAN:
            print(f"  [plan] li-writer batches: {work}/li-batch-NNN.txt -> {work}/li-out/")
        if li_batches:
            prompts = [f"Your input file: {b.resolve()}\n"
                       "Read it, then follow your li-writer instructions and write your JSON "
                       "to the OutputFile named inside it."
                       for b in li_batches]
            fan_out("li-writer", prompts, "Stage 8.6 li-writer", a.max_workers)
        rc = sh([PY, "tools/scripts/draft_linkedin.py", "--phase", "merge",
                 "--run-dir", str(run), *WD], "Stage 8.6 LI merge", tolerate=(7,))
        if rc == 0:
            sh([PY, "tools/scripts/linkedin_queue.py", "--run-dir", str(run),
                "--config", li_cfg], "Stage 8.6 LI rank")
            # TWO-PHASE, because LinkedIn has a consent gate that email does not.
            # You cannot DM a stranger: the invite comes first, the DM only after
            # they accept. So a fire queues INVITES (generate.js), each carrying
            # the DM li-writer composed; sweep_acceptance.js later notices who
            # accepted and releases that DM to generate-dm.js. Queueing DMs here
            # would be rejected outright as "not accepted, invite first".
            sh(["node", "linkedin/queue/generate.js",
                "--leads-file", str(run / "linkedin-leads.json")], "Stage 8.6 LI invite queue")
            # Anyone reachable with NO invite (Open Profile, or already a 1st-degree
            # connection) skips the wait entirely and can be messaged today.
            sh(["node", "linkedin/queue/generate-dm.js",
                "--leads-file", str(run / "linkedin-leads.json")], "Stage 8.6 LI direct-DM queue")
            try:   # a JSON array, not line-oriented — count entries, not lines
                li_drafted = len(json.loads((run / "linkedin-leads.json").read_text()))
            except Exception:
                li_drafted = 0
            print("  LinkedIn queued (invites + any directly-messageable people).")
            print("  The send side is ONE command, every weekday — it tops up from the")
            print("  backlog, sends, sweeps for acceptances and DMs whoever accepted:")
            print("      cd linkedin && bash scripts/daily.sh --live")
            print("  (drop --live for a dry run; needs the LinkedIn Chrome open —")
            print("   bash linkedin/scripts/start_chrome.sh)")
        else:
            print("  no LinkedIn messages composed; nothing queued.")

    # (The Stage 8.6 qualified-pending ledger was removed 2026-08-05 — see
    # die_no_supply(). A lead that qualifies but cannot be reached this run is now
    # retired at Stage 5.5 rather than queued for an identical retry.)

    if (not PLAN and drafted == 0 and wa_drafted == 0 and li_drafted == 0
            and (email_on or wa_on or li_on)):
        die("Stage 8.5", "0 drafts on every enabled channel (email + WhatsApp + LinkedIn)")

    if not a.dry_run:
        cleanup_run_artifacts(run)

    tgt = ""
    if target:
        tgt = f" target={target}"
        if not PLAN and drafted < target:
            tgt += f" (SHORT: {drafted}/{target} drafted after enrich/draft attrition)"
    if dry_sources:
        tgt += f" [no-fresh-ground: {','.join(dry_sources)}]"

    # Report what was ACTUALLY sent, counted from the send log — never a blanket
    # "sent+persisted". The old line asserted success for any non-dry run, so a
    # run where every draft was suppressed (all already in sent-log/bounce-list)
    # reported identically to a run that really mailed 300 people.
    outcome = "(dry-run, nothing sent)" if a.dry_run else _send_outcome(run, email_on)
    status(f"DONE — qualified={qualified} drafted={drafted}{tgt} {outcome}{smtp_note}")
    print(f"\nDONE: {a.slug} — qualified={qualified} drafted={drafted}{tgt} {outcome}{smtp_note}")


if __name__ == "__main__":
    main()
