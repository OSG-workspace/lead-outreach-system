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
import math
import os
import re as _re
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


def sh(args: list[str], stage: str, tolerate: tuple[int, ...] = ()):
    """Run a pipeline script; abort the whole run on non-zero (kill-on-fallback).
    `tolerate` lists exit codes that are an expected mid-run state, not a
    fallback (e.g. qualify's rc=7 "0 qualified yet" between sourcing waves of a
    --target-leads run). Returns the exit code."""
    print(f"\n=== {stage}: {' '.join(args)}")
    status(f"{stage} — running")
    if PLAN:
        return 0
    r = subprocess.run(args, cwd=str(PROJECT))
    if r.returncode != 0 and r.returncode not in tolerate:
        die(stage, f"command exited {r.returncode}", r.returncode)
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
            if s["type"] not in ("map", "directory", "places"):
                die("pre-flight", f'sourcing.json sources[{i}] unknown type {s["type"]!r} '
                                  '(known: "map", "places", "directory")')
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
    if "resolve_domains" not in cfg:
        needs_resolve = any(s["type"] == "places" and (s.get("tier") or "pro") != "enterprise"
                             for s in cfg["sources"])
        if needs_resolve:
            cfg["resolve_domains"] = True

    cfg["method"] = "map"   # retained for status/log wording only; never branches
    return cfg


def slug_base_of(slug: str) -> str:
    """`2026-07-27-lb-insurance-tpa-1` -> `lb-insurance-tpa` (the fixture name)."""
    base = _re.sub(r"^\d{4}-\d{2}-\d{2}-", "", slug)
    return _re.sub(r"-\d+$", "", base)


# Words that carry no company identity, so they must never match a domain.
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
                "gap-batch-*.txt", "gap-out-*.json", "wa-batch-*.txt", "wa-out-*.json",
                # LinkedIn per-agent intermediates. The ranked linkedin-leads.json
                # and people-*.json stay: the invite drip runs for weeks after the
                # fire and they are the audit trail for who was queued and why.
                "li-batch-*.txt", "lif-batch-*.txt"):
        for f in run.glob(pat):
            f.unlink(missing_ok=True)
            removed += 1
    print(f"  cleanup: removed raw_html + {removed - 1 if removed else 0} intermediate files "
          f"(KEEP_RUN_ARTIFACTS=1 to keep).")


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


def fan_out(agent: str, prompts: list[str], stage: str, max_workers: int, timeout: int = 420,
            include: set[str] | None = None):
    """Dispatch one sub-agent per prompt, in parallel, via headless claude -p.

    `include` opts into the agent definition's OPTIONAL blocks; anything not
    named is stripped from the system prompt (see agent_dispatch.agent_body)."""
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
                           cwd=str(ad.REPO), on_done=_cb, include=include)
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
    # A LinkedIn-ONLY run never touches a mailbox, so every email-shaped stage
    # (the no-email drop at extract, the email-quality gates at qualify, the
    # decision-maker email enrichment, the email drafter) is skipped rather than
    # run and ignored. Before this, such a run halted at Stage 5.5 on
    # "0 leads with resolved contact + direct email" — an email condition
    # killing a channel that needs no email.
    li_only = li_on and not email_on and not wa_on
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
          f"email={email_on} whatsapp={wa_on} linkedin={li_on} wa_fallback={wa_fallback} "
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
        sh(["python3", "tools/scripts/merge_candidates.py", "--run-dir", str(run),
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
        ecmd = ["python3", "tools/scripts/extract_leads.py", "--run-dir", str(run),
                "--sent-log", SENT_LOG]
        if li_only:
            ecmd.append("--allow-no-email")
        sh(ecmd, "Stage 5 extract")
        if not PLAN and count_lines(run / "leads-extracted.json") == 0:
            if final:
                return die_no_supply("Stage 5 extract", "0 leads extracted")
            print("  NOTE: 0 leads extracted so far; sourcing next wave.")
            return 0
        qcmd = ["python3", "tools/scripts/qualify_leads.py", "--run-dir", str(run)]
        if li_only:
            qcmd.append("--linkedin-run")
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
    share = max(1, max_cand // len(units))
    dry_sources: list[str] = []      # sources that reported no fresh ground (rc=8)

    for i, (s, t) in enumerate(units, 1):
        if t:
            label_bit = t["vertical"]
        elif s["type"] == "places":
            # one process sweeps all of this source's place types
            label_bit = "-".join(sorted({x.get("vertical", "")
                                         for x in (s.get("targets") or [])})) or "places"
        else:
            label_bit = s.get("vertical") or s["type"]
        label = ("Stage 2 source" if len(units) == 1 else
                 f"Stage 2 source [{i}/{len(units)}] {label_bit}")
        if s["type"] == "map":
            cmd = ["python3", "tools/scripts/source_overpass.py", "--run-dir", str(run),
                   "--selector", t["selector"],
                   "--vertical", t["vertical"],
                   "--batch-prefix", f"{i:02d}-{t['vertical']}",
                   "--max-candidates", str(share)]
            if resolve_on:
                cmd += ["--capture-unresolved"]
            if s.get("region") or sourcing.get("region"):
                cmd += ["--region", str(s.get("region") or sourcing["region"])]
            else:
                cmd += ["--auto"]
        elif s["type"] == "places":
            # Google Places API (New), adaptive quadtree. Same places.txt, same
            # candidate contract; reaches the businesses OSM has no premises for.
            cmd = ["python3", "tools/scripts/source_places.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}",
                   "--max-candidates", str(share)]
        else:   # directory: any paginated listing on the open web
            cmd = ["python3", "tools/scripts/source_directory.py", "--run-dir", str(run),
                   "--source", json.dumps(s),
                   "--batch-prefix", f"{i:02d}-{label_bit}"]
        # rc=8 means "this source has no fresh ground left" — an expected end
        # state, not a fallback. One dry source must not kill a run whose other
        # sources still have ground. If EVERY source is dry the run halts at the
        # Stage 3 "0 candidates after dedup" gate.
        rc = sh(cmd, label, tolerate=(8,))
        if rc == 8:
            dry_sources.append(label_bit)

    # --- Stage 2.5: name -> own domain, VERIFIED ---
    # Opt-in per branch (`"resolve_domains": true`). OSM enumerates the ICP far
    # better than it records websites, so in weakly-mapped markets most elements
    # are name-only and Stage 2 alone starves. This recovers them deterministically
    # (search + fetch + name-token proof, no LLM). Unproven domains are DROPPED,
    # never guessed: a wrong domain pitches business A at business B and burns it
    # in the sent-log forever.
    if resolve_on:
        sh(["python3", "tools/scripts/resolve_domains.py", "--run-dir", str(run),
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
        # The phone ladder is half the agent's body and is only reachable when
        # EnrichPhone is on; an email-only run ships the lean prompt.
        fan_out("name-finder", nf_prompts, "Stage 5.5 name-finder", a.max_workers,
                include={"phone"} if wa_on else None)
        merge = ["python3", "tools/scripts/enrich_contact_person.py", "--phase", "merge",
                 "--run-dir", str(run)]
        if wa_fallback:
            merge.append("--wa-fallback")
        sh(merge, "Stage 5.5 enrich merge")
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
        sh(["python3", "tools/scripts/sync_brevo_events.py"], "Stage 7 bounce-sync")
        cap = os.environ.get("MAX_EMAILS_PER_RUN", "1000")
        if target:
            cap = str(min(int(cap), target))   # outreach exactly the asked-for volume
        sh(["python3", "tools/scripts/send_batch_brevo.py", "--run-dir", str(run),
            "--send", "--cap", cap], "Stage 7 send")
        sh(["python3", "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
            "--sent-log", SENT_LOG], "Stage 8 persist")
    elif email_on:
        print("\n[DRY-RUN] drafts written; send + persist skipped.")

    # --- Step 8.5: WhatsApp (opt-in) ---
    wa_drafted = 0
    if wa_on:
        if draft_mode == "custom":
            # custom path: wa-writer agents compose per-company messages.
            sh(["python3", "tools/scripts/draft_whatsapp_custom.py", "--phase", "prep",
                "--run-dir", str(run)], "Stage 8.5 WA prep")
            wbatches = sorted(run.glob("wa-batch-*.txt"))
            if wbatches:
                prompts = [b.read_text() for b in wbatches]
                fan_out("wa-writer", prompts, "Stage 8.5 wa-writer", a.max_workers)
                sh(["python3", "tools/scripts/draft_whatsapp_custom.py", "--phase", "merge",
                    "--run-dir", str(run)], "Stage 8.5 WA merge")
        else:
            # template path: deterministic drafter renders pitch.json's
            # wa_body_template. --fallback-only keeps email primary: only leads
            # whose direct email failed verification go out on WhatsApp.
            wa_cmd = ["python3", "tools/scripts/draft_whatsapp.py", "--run-dir", str(run)]
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
            sh(["python3", "tools/scripts/persist_sent_log.py", "--run-dir", str(run),
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
        sh(["python3", "tools/scripts/draft_linkedin.py", "--phase", "prep",
            "--run-dir", str(run)], "Stage 8.6 LI prep")
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
        sh(["python3", "tools/scripts/resolve_li_profiles.py", "--phase", "prep",
            "--run-dir", str(run), "--cap", str(find_cap)],
           "Stage 8.6b LI profile-find prep")
        lif_batches = sorted(run.glob("lif-batch-*.txt"))
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
        rc_find = sh(["python3", "tools/scripts/resolve_li_profiles.py", "--phase", "merge",
                      "--run-dir", str(run)], "Stage 8.6b LI profile-find merge", tolerate=(7,))
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
        sh(["python3", "tools/scripts/qualify_people.py", "--run-dir", str(run),
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
        sh(["python3", "tools/scripts/draft_linkedin.py", "--phase", "batch",
            "--run-dir", str(run)], "Stage 8.6 LI batch")
        li_batches = sorted(run.glob("li-batch-*.txt"))
        if li_batches:
            prompts = [f"Your input file: {b.resolve()}\n"
                       "Read it, then follow your li-writer instructions and write your JSON "
                       "to the OutputFile named inside it."
                       for b in li_batches]
            fan_out("li-writer", prompts, "Stage 8.6 li-writer", a.max_workers)
        rc = sh(["python3", "tools/scripts/draft_linkedin.py", "--phase", "merge",
                 "--run-dir", str(run)], "Stage 8.6 LI merge", tolerate=(7,))
        if rc == 0:
            sh(["python3", "tools/scripts/linkedin_queue.py", "--run-dir", str(run),
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
    status(f"DONE — qualified={qualified} drafted={drafted}{tgt} "
           f"{'(dry-run, nothing sent)' if a.dry_run else 'sent+persisted'}")
    print(f"\nDONE: {a.slug} — qualified={qualified} drafted={drafted}{tgt} "
          f"{'(dry-run, nothing sent)' if a.dry_run else 'sent+persisted'}")


if __name__ == "__main__":
    main()
