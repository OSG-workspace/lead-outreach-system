#!/usr/bin/env python3
"""
End-to-end campaign driver.

Replaces the manual step-by-step orchestration with one script that runs the
whole funnel — sourcing through draft generation — and loops sourcing until N
qualified leads are produced (or max passes reached).

Concretely it:

  1. Reads runs/<slug>/icp.yaml and runs/<slug>/queries.txt
  2. Runs the Maps scraper with `-c` concurrency, writes maps-raw.json
  3. Filters to canonical schema (filter_maps_leads.py)
  4. Enriches no-email leads from their websites with a shared browser
     (enrich_emails.py — concurrency 10+)
  5. Applies source-fit (geography, vertical, contact, website) via funnel_lib
  6. Resolves the best clean email per lead via funnel_lib
  7. Scores via funnel_lib (data-driven verticals — see Fix #1)
  8. If qualified < N: broaden queries and loop (max 3 passes)
  9. Drafts a per-lead email referencing the lead's specific signal
 10. Prints a single run summary

Usage:
    ./tools/run.sh tools/scripts/run_campaign.py \\
        --run-slug 2026-05-11-real-estate-uae \\
        --target 100 \\
        --queries-file runs/2026-05-11-real-estate-uae/queries.txt

Optional:
    --max-passes 3           how many sourcing rounds before giving up
    --scraper-depth 2        gosom -depth (1=~60 leads/query, 2=~120, 3=~200)
    --scraper-concurrency 8  gosom -c (its internal concurrency)
    --enrich-concurrency 15  how many websites to crawl at once
    --skip-existing-source   reuse maps-raw.json if already present
    --dry-run                stop after scoring, don't write drafts
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "tools" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import funnel_lib as fl  # noqa: E402


def sh(cmd: list[str], *, env: dict | None = None, timeout: int = 1800) -> tuple[int, str, str]:
    """Run a subprocess, capture stdout/stderr. Return (rc, stdout, stderr).

    On timeout, returns rc=124 (the standard `timeout(1)` convention) with
    whatever output the process had buffered. Previously a TimeoutExpired
    bubbled up as an uncaught exception that silently killed the driver
    process — we lost batch 2 to exactly that failure mode.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""), \
               f"TIMEOUT after {timeout}s: {(exc.stderr or '') if not isinstance(exc.stderr, bytes) else exc.stderr.decode()}"


def run_python(script: str, args: list[str], timeout: int = 1800) -> tuple[int, str, str]:
    """Run a tools/scripts/<script>.py via the project venv."""
    venv_python = ROOT / "tools" / "venv" / "bin" / "python"
    runner = str(venv_python) if venv_python.exists() else sys.executable
    return sh([runner, str(SCRIPTS / script), *args], timeout=timeout)


def now() -> str:
    return time.strftime("%H:%M:%S")


def reap_chromium_zombies() -> int:
    """Kill orphaned Chromium processes left behind by crawl4ai/playwright.

    crawl4ai's AsyncWebCrawler.__aexit__ does not reliably tear down all
    Chromium child processes; they accumulate across passes, hold ~1.8 GB
    each, and eventually cause the NEXT crawl to fail with
    `Browser.close: Connection closed`. This call is a defensive sweep run
    between passes so each pass starts with a clean browser state.
    Returns the number of processes killed.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", "chrome-headless-shell"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return 0
    pids = [p for p in out.stdout.split() if p.isdigit()]
    if not pids:
        return 0
    try:
        subprocess.run(["kill", "-9", *pids], capture_output=True, timeout=10)
    except Exception:
        return 0
    return len(pids)


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def run_scraper(queries_file: Path, out_file: Path, *, depth: int, concurrency: int,
                inactivity: str = "2m") -> None:
    """Invoke gosom/google-maps-scraper."""
    scraper = os.environ.get("GMAPS_SCRAPER_PATH", str(ROOT / "tools" / "google-maps-scraper"))
    if not Path(scraper).exists():
        raise RuntimeError(f"google-maps-scraper binary missing at {scraper} — run setup-tools.")
    cmd = [
        scraper,
        "-input", str(queries_file),
        "-results", str(out_file),
        "-json",
        "-email",
        "-depth", str(depth),
        "-c", str(concurrency),
        "-lang", "en",
        "-exit-on-inactivity", inactivity,
    ]
    log(f"sourcing: {scraper} -depth {depth} -c {concurrency} {queries_file.name}")
    rc, out, err = sh(cmd, timeout=3600)
    if rc != 0:
        # The scraper sometimes exits non-zero even when it produced output
        # (e.g. exit-on-inactivity races). Treat empty output as fatal only.
        if not out_file.exists() or out_file.stat().st_size == 0:
            raise RuntimeError(f"scraper failed (rc={rc}): {err[-500:]}")
        log(f"scraper exited rc={rc} but output exists ({out_file.stat().st_size} bytes), continuing")


def _partition_queries(queries: list[str], partitions: int) -> list[list[str]]:
    """Split queries into N roughly-equal chunks, preserving order within each."""
    if partitions <= 1 or len(queries) <= partitions:
        return [queries]
    buckets: list[list[str]] = [[] for _ in range(partitions)]
    for idx, q in enumerate(queries):
        buckets[idx % partitions].append(q)
    return [b for b in buckets if b]


def run_scraper_parallel(queries_file: Path, out_file: Path, *, depth: int,
                         concurrency: int, partitions: int,
                         inactivity: str = "2m") -> None:
    """Geographic-partition variant of run_scraper.

    Splits queries.txt into N disjoint chunks and runs N gosom processes
    concurrently, each writing to its own results file, then concatenates
    the per-partition outputs into a single out_file. Yields a 2-3x wall
    clock speedup on broad briefs (e.g. multi-city queries) vs one gosom
    with internal concurrency, because gosom's `-exit-on-inactivity`
    triggers per-process, not per-query.
    """
    if partitions <= 1:
        return run_scraper(queries_file, out_file,
                           depth=depth, concurrency=concurrency, inactivity=inactivity)
    queries = [q.strip() for q in queries_file.read_text().splitlines() if q.strip()]
    chunks = _partition_queries(queries, partitions)
    if len(chunks) <= 1:
        return run_scraper(queries_file, out_file,
                           depth=depth, concurrency=concurrency, inactivity=inactivity)
    tmp_dir = out_file.parent / f"{out_file.stem}-partitions"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[subprocess.Popen, Path]] = []
    scraper = os.environ.get("GMAPS_SCRAPER_PATH", str(ROOT / "tools" / "google-maps-scraper"))
    if not Path(scraper).exists():
        raise RuntimeError(f"google-maps-scraper binary missing at {scraper} — run setup-tools.")
    log(f"sourcing[parallel]: {len(chunks)} partitions x {len(chunks[0])}-ish queries each "
        f"-depth {depth} -c {concurrency}")
    for i, chunk in enumerate(chunks):
        chunk_q = tmp_dir / f"queries-p{i}.txt"
        chunk_out = tmp_dir / f"results-p{i}.json"
        chunk_q.write_text("\n".join(chunk) + "\n")
        cmd = [
            scraper,
            "-input", str(chunk_q),
            "-results", str(chunk_out),
            "-json", "-email",
            "-depth", str(depth),
            "-c", str(concurrency),
            "-lang", "en",
            "-exit-on-inactivity", inactivity,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append((proc, chunk_out))

    start = time.time()
    for i, (proc, chunk_out) in enumerate(procs):
        try:
            _stdout, stderr = proc.communicate(timeout=3600)
        except subprocess.TimeoutExpired:
            proc.kill()
            _stdout, stderr = proc.communicate()
            log(f"  partition {i}: TIMEOUT, killed")
        rc = proc.returncode
        size = chunk_out.stat().st_size if chunk_out.exists() else 0
        log(f"  partition {i}: rc={rc} bytes={size} elapsed={round(time.time()-start,1)}s")
        if rc != 0 and size == 0:
            err_str = stderr.decode(errors="replace")[-300:] if isinstance(stderr, bytes) else (stderr or "")[-300:]
            log(f"  partition {i} failed: {err_str}")

    # Concatenate partition outputs into the canonical out_file (JSONL).
    with out_file.open("w") as outf:
        for _proc, chunk_out in procs:
            if chunk_out.exists():
                outf.write(chunk_out.read_text())
    log(f"sourcing[parallel] done: merged {len(procs)} partitions into {out_file.name}")


def filter_maps(in_file: Path, out_file: Path, *, city: str | None,
                min_rating: float, require_website: bool) -> int:
    args = ["--input", str(in_file), "--output", str(out_file),
            "--min-rating", str(min_rating)]
    if city:
        args += ["--city", city]
    if require_website:
        args.append("--require-website")
    rc, out, err = run_python("filter_maps_leads.py", args)
    if rc != 0:
        raise RuntimeError(f"filter_maps_leads failed (rc={rc}): {err}")
    log("filter: " + out.strip().splitlines()[-1] if out.strip() else "filter complete")
    return sum(1 for _ in out_file.open()) if out_file.exists() else 0


def enrich_emails_mailscout(in_file: Path, out_file: Path, *, num_threads: int = 5,
                            smtp_timeout: int = 2) -> dict:
    """Complement crawl4ai with MailScout pattern generation + SMTP validation.

    Free, local enrichment for leads without person-class emails. Generates
    email pattern candidates based on company name/domain, validates via SMTP.
    Expected to recover ~50-60% of remaining leads after crawl4ai.
    """
    report_path = out_file.with_suffix(".report.json")
    cmd_args = [
        "--input", str(in_file),
        "--output", str(out_file),
        "--smtp-threads", str(num_threads),
        "--smtp-timeout", str(smtp_timeout),
        "--report", str(report_path),
    ]
    rc, out, err = run_python("enrich_emails_mailscout.py", cmd_args, timeout=1800)
    if rc != 0 and not out_file.exists():
        log(f"mailscout enrichment failed (rc={rc}), falling back to input. err={err[-300:]}")
        shutil.copyfile(in_file, out_file)
        return {"input": 0, "enriched": 0, "skipped_due_to_error": True}
    if rc != 0:
        log(f"mailscout partial (rc={rc}), proceeding with partial result. err_tail={err[-200:]}")
    if report_path.exists():
        try:
            return json.loads(report_path.read_text())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(out)
    except Exception:
        return {}


def enrich_emails(in_file: Path, out_file: Path, *, concurrency: int, page_cap: int,
                  per_lead_timeout: int = 30, global_timeout: int = 900,
                  claude_review_queue: Path | None = None) -> dict:
    report_path = out_file.with_suffix(".report.json")
    cmd_args = [
        "--input", str(in_file),
        "--output", str(out_file),
        "--concurrency", str(concurrency),
        "--per-lead-page-cap", str(page_cap),
        "--per-lead-timeout", str(per_lead_timeout),
        "--global-timeout", str(global_timeout),
        "--report", str(report_path),
    ]
    if claude_review_queue:
        cmd_args += ["--claude-review-queue", str(claude_review_queue)]
    rc, out, err = run_python("enrich_emails.py", cmd_args, timeout=global_timeout + 120)
    if rc != 0 and not out_file.exists():
        # Hard failure (crash before any output): fall back to the input.
        log(f"enrichment failed (rc={rc}), falling back to unenriched leads. err={err[-300:]}")
        shutil.copyfile(in_file, out_file)
        return {"input": 0, "enriched": 0, "skipped_due_to_error": True}
    if rc != 0:
        # Partial output exists (timed out gracefully). Keep going.
        log(f"enrichment partial (rc={rc}), proceeding with partial enriched file. "
            f"err_tail={err[-200:]}")
    # Prefer reading the report file (deterministic JSON) over parsing stdout
    # (which is pretty-printed and got mangled by line-based parsing before).
    if report_path.exists():
        try:
            return json.loads(report_path.read_text())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(out)
    except Exception:
        return {}


def source_fit_and_resolve(in_file: Path, source_fit_out: Path, resolved_out: Path,
                           cfg: dict) -> tuple[list[dict], dict]:
    leads = fl.load_jsonl(in_file)
    fit, sf_drops = fl.apply_source_fit(leads, cfg)
    fl.write_jsonl(source_fit_out, fit)
    resolved, res_drops = fl.resolve_email_pool(fit)
    fl.write_jsonl(resolved_out, resolved)
    return resolved, {"source_fit_drops": dict(sf_drops), "email_drops": dict(res_drops),
                      "source_fit_kept": len(fit), "resolved": len(resolved)}


def score_pool(in_pool: list[dict], cfg: dict, out_file: Path) -> list[dict]:
    scored = [fl.score_lead(l, cfg) for l in in_pool]
    scored.sort(key=lambda r: -(r.get("score") or 0))
    fl.write_jsonl(out_file, scored)
    return scored


_BOOKING_KEYWORDS = ("calendly", "acuity", "booksy", "fresha", "zenoti", "book online",
                     "schedule online", "online booking", "reserve online")
_AI_KEYWORDS = ("artificial intelligence", " ai ", "ai-powered", "ai powered",
                "machine learning", "chatbot", "ai assistant", "automation",
                "ai solution", "ai tools", "ai-driven")
_SAAS_KEYWORDS = ("hubspot", "salesforce", "zoho", "monday.com", "asana",
                  "microsoft 365", "office 365", "google workspace")
_HIRING_KEYWORDS = ("we're hiring", "we are hiring", "now hiring", "join our team",
                    "open positions", "careers", "vacancies")
_PDF_HINTS = (".pdf", "menu.pdf", "brochure.pdf")


def classify_signal(sig: dict) -> dict:
    """Derive a concrete gap-signal record from raw signals.

    Output shape matches what `funnel_lib.has_concrete_signal` expects:
    populates one or more of REAL_SIGNAL_KEYS = (mode, owner_name,
    ai_mentioned, saas, branches, hiring_manual, pdf_menu).
    """
    out: dict = {"emails": sig.get("emails") or []}
    md_blob = " ".join([
        (sig.get("about_excerpt") or ""),
        " ".join(sig.get("headings", {}).get("h1", []) or []),
        " ".join(sig.get("headings", {}).get("h2", []) or []),
        " ".join(sig.get("headings", {}).get("h3", []) or []),
        sig.get("meta_description") or "",
        sig.get("title") or "",
    ]).lower()

    # Mode signal — how customers actually reach the business.
    if sig.get("social_links", {}).get("instagram") and not sig.get("has_booking_widget"):
        # No real booking, just a social presence
        pass
    if sig.get("has_booking_widget") or any(k in md_blob for k in _BOOKING_KEYWORDS):
        out["mode"] = "booking_widget"
    elif sig.get("has_contact_form"):
        out["mode"] = "contact_form"
    elif sig.get("phones"):
        out["mode"] = "phone"

    if any(k in md_blob for k in _AI_KEYWORDS):
        out["ai_mentioned"] = True
    if any(k in md_blob for k in _SAAS_KEYWORDS):
        out["saas"] = True
    if any(k in md_blob for k in _HIRING_KEYWORDS):
        out["hiring_manual"] = True
    if any(p in (sig.get("title") or "").lower() or p in md_blob for p in _PDF_HINTS):
        out["pdf_menu"] = True

    return out


def extract_signals_for(needs_signal_leads: list[dict], run_dir: Path,
                        concurrency: int = 40) -> list[dict]:
    """Run extract_signals.py on the URLs of needs_signal leads, merge signals back."""
    if not needs_signal_leads:
        return needs_signal_leads
    urls_jsonl = run_dir / "signal-urls.jsonl"
    sig_dir = run_dir / "signals"
    sig_dir.mkdir(exist_ok=True)
    with urls_jsonl.open("w") as f:
        for lead in needs_signal_leads:
            url = lead.get("url") or lead.get("website")
            lid = lead.get("id") or fl.slugify(lead.get("name"))
            if url and lid:
                f.write(json.dumps({"url": url, "id": lid}) + "\n")

    log(f"signal-extract: {sum(1 for _ in urls_jsonl.open())} URLs at concurrency={concurrency}")
    rc, out, err = run_python("extract_signals.py", [
        "--batch", str(urls_jsonl),
        "--out-dir", str(sig_dir),
        "--concurrency", str(concurrency),
    ], timeout=3600)
    if rc != 0:
        log(f"extract_signals failed (rc={rc}), skipping signal merge. err={err[-300:]}")
        return needs_signal_leads

    # Load signal JSONs and merge into leads
    merged = []
    for lead in needs_signal_leads:
        lid = lead.get("id") or fl.slugify(lead.get("name"))
        sig_file = sig_dir / f"{lid}.json"
        if not sig_file.exists():
            merged.append(lead)
            continue
        try:
            raw_sig = json.loads(sig_file.read_text())
        except json.JSONDecodeError:
            merged.append(lead)
            continue
        if not raw_sig.get("fetched"):
            merged.append(lead)
            continue
        lead["signals"] = classify_signal(raw_sig)
        lead["primary_gap"] = fl.signal_used(lead) or lead.get("primary_gap")
        merged.append(lead)
    return merged


def broaden_queries(queries_file: Path, pass_num: int) -> None:
    """In-place modify the queries file to broaden it for the next pass.

    Pass 1 -> 2: append a generic phrasing of every query (drop city qualifier).
    Pass 2 -> 3: append synonyms (services -> firm, agency, group).
    """
    base = [q.strip() for q in queries_file.read_text().splitlines() if q.strip()]
    extras: list[str] = []
    syn_map = {
        "agency": ["agencies", "broker", "brokers", "firm", "group"],
        "agencies": ["broker", "brokers", "firm", "group"],
        "service": ["services", "company", "provider"],
        "services": ["company", "provider"],
        "firm": ["company", "consultancy", "group"],
        "consultant": ["consultants", "consultancy", "firm"],
    }
    if pass_num >= 2:
        for q in base:
            tokens = q.split()
            # Drop the trailing geo token if any (UAE / Dubai / etc.)
            stripped = " ".join(tokens[:-1]) if len(tokens) > 2 else q
            if stripped and stripped not in base and stripped not in extras:
                extras.append(stripped)
    if pass_num >= 3:
        for q in base:
            for word, syns in syn_map.items():
                if word in q.lower():
                    for s in syns:
                        v = q.lower().replace(word, s)
                        if v not in base and v not in extras:
                            extras.append(v)
    if extras:
        queries_file.write_text("\n".join(base + extras) + "\n")
        log(f"broadened queries +{len(extras)} entries (total {len(base) + len(extras)})")


def draft_emails(qualified: list[dict], run_slug: str, out_file: Path) -> list[dict]:
    drafts = []
    for lead in qualified:
        drafts.append(fl.build_outreach_draft(lead, run_slug))
    fl.write_jsonl(out_file, drafts)
    return drafts


def passes_send_gate(drafts: list[dict], role_threshold: int) -> tuple[list[dict], dict]:
    return fl.gate_drafts(drafts, role_threshold=role_threshold)


def summarize_funnel(per_pass: list[dict], qualified: list[dict], drafts: list[dict],
                     elapsed_s: float) -> str:
    lines = [
        "=" * 60,
        "CAMPAIGN SUMMARY",
        "=" * 60,
    ]
    for i, p in enumerate(per_pass, 1):
        lines.append(
            f"PASS {i}: maps_raw={p.get('raw_count',0)} "
            f"-> filtered={p.get('filtered_count',0)} "
            f"-> enriched_emails={p.get('email_enriched',0)} "
            f"-> source_fit={p.get('source_fit_kept',0)} "
            f"-> resolved_email={p.get('resolved',0)} "
            f"-> qualified={p.get('qualified',0)} "
            f"({p.get('elapsed_s',0)}s)"
        )
        if p.get("source_fit_drops"):
            lines.append(f"   source_fit_drops: {p['source_fit_drops']}")
    lines.append("")
    lines.append(f"FINAL  qualified={len(qualified)}  drafts={len(drafts)}  total={elapsed_s:.1f}s")
    if drafts:
        signals = Counter(d.get("signal_used") or "fallback" for d in drafts)
        lines.append(f"DRAFT SIGNALS  {dict(signals)}")
        lines.append("")
        lines.append("Top 5 drafts:")
        for d in sorted(drafts, key=lambda r: -(r.get("score") or 0))[:5]:
            lines.append(f"  {d['score']:3} {(d.get('to_name') or '')[:35]:35} "
                         f"{(d.get('to_email') or '')[:45]:45} "
                         f"signal={d.get('signal_used') or '?'}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-slug", required=True,
                   help="Folder name under runs/. Must already contain icp.yaml and queries.txt.")
    p.add_argument("--target", type=int, default=100,
                   help="Target number of qualified leads to produce.")
    p.add_argument("--max-passes", type=int, default=3)
    p.add_argument("--scraper-depth", type=int, default=2)
    p.add_argument("--scraper-concurrency", type=int, default=8)
    p.add_argument("--scraper-partitions", type=int, default=1,
                   help="Split queries.txt into N partitions and run N gosom "
                        "processes in parallel. 1 = single process (legacy).")
    p.add_argument("--include-directories", action="store_true",
                   help="Also source from public business directories "
                        "(Yelp/YellowPages/BBB/Clutch/etc.) via "
                        "source_directories.py. Merged into maps-raw.json.")
    p.add_argument("--directory-per-query", type=int, default=30,
                   help="Max directory listing pages crawled per query.")
    p.add_argument("--claude-review-queue", action="store_true",
                   help="When regex enrichment fails but the page shows an "
                        "owner+role, write a review task to runs/<slug>/"
                        "claude-tasks/email-review/ for the Claude session "
                        "to process. No network calls are made.")
    p.add_argument("--enrich-concurrency", type=int, default=40,
                   help="Parallel website crawls during email enrichment.")
    p.add_argument("--signal-concurrency", type=int, default=40,
                   help="Parallel website crawls during signal extraction.")
    p.add_argument("--enrich-page-cap", type=int, default=2)
    p.add_argument("--enrich-per-lead-timeout", type=int, default=30,
                   help="Per-lead wall-clock cap inside enrich_emails.")
    p.add_argument("--enrich-global-timeout", type=int, default=900,
                   help="Whole-batch cap inside enrich_emails. Partial result is kept.")
    p.add_argument("--mailscout", action="store_true",
                   help="After crawl4ai enrichment, run MailScout pattern generation "
                        "on remaining leads (free, local, no API required).")
    p.add_argument("--skip-enrich", action="store_true",
                   help="Skip crawl4ai + mailscout enrichment entirely. Rely on the "
                        "gosom -email flag + funnel resolve. The prior run showed "
                        "crawl4ai timed out on every lead (0/1258 enriched) while "
                        "burning ~3h; this flag drops that pure overhead.")
    p.add_argument("--scraper-depth-cap", type=int, default=None,
                   help="Hard ceiling on scraper depth growth across passes. Without "
                        "this, depth = scraper-depth + (pass-1), so pass 3 ran depth=4 "
                        "and took 13.5h alone. Set e.g. 2 to keep depth flat.")
    p.add_argument("--min-rating", type=float, default=3.0)
    p.add_argument("--city", default=None,
                   help="If set, drop leads outside this city in filter_maps.")
    p.add_argument("--require-website", action="store_true")
    p.add_argument("--skip-existing-source", action="store_true",
                   help="Reuse runs/<slug>/maps-raw.json from a previous attempt.")
    p.add_argument("--dry-run", action="store_true",
                   help="Stop after scoring; don't generate drafts.")
    args = p.parse_args()

    run_dir = ROOT / "runs" / args.run_slug
    if not run_dir.exists():
        raise SystemExit(f"runs/{args.run_slug} does not exist. Create it with icp.yaml and queries.txt.")

    queries_file = run_dir / "queries.txt"
    icp_file = run_dir / "icp.yaml"
    if not queries_file.exists():
        raise SystemExit(f"missing {queries_file}")
    if not icp_file.exists():
        raise SystemExit(f"missing {icp_file}")

    cfg = fl.build_funnel_config(run_dir)
    log(f"cfg: target={args.target}  countries={cfg.get('target_countries')} "
        f"vertical_keywords={cfg.get('target_vertical_keywords') or 'fallback'}")

    overall_start = time.time()
    all_qualified: dict[str, dict] = {}
    per_pass_stats: list[dict] = []

    for pass_num in range(1, args.max_passes + 1):
        pass_start = time.time()
        log(f"---------- PASS {pass_num} ----------")

        if pass_num > 1:
            broaden_queries(queries_file, pass_num)

        maps_raw = run_dir / f"maps-raw-pass{pass_num}.json"
        # Pass 1 can reuse an existing maps-raw.json (--skip-existing-source).
        existing_default = run_dir / "maps-raw.json"
        if pass_num == 1 and args.skip_existing_source and existing_default.exists():
            log(f"reusing existing {existing_default}")
            shutil.copyfile(existing_default, maps_raw)
        else:
            effective_depth = args.scraper_depth + (pass_num - 1)
            if args.scraper_depth_cap is not None:
                effective_depth = min(effective_depth, args.scraper_depth_cap)
            run_scraper_parallel(queries_file, maps_raw,
                                 depth=effective_depth,
                                 concurrency=args.scraper_concurrency,
                                 partitions=args.scraper_partitions)
        raw_count = sum(1 for _ in maps_raw.open()) if maps_raw.exists() else 0
        log(f"maps raw: {raw_count} records")

        filtered = run_dir / f"leads-filtered-pass{pass_num}.json"
        filtered_count = filter_maps(maps_raw, filtered, city=args.city,
                                     min_rating=args.min_rating,
                                     require_website=args.require_website)
        log(f"after filter_maps: {filtered_count}")

        if args.include_directories:
            dir_out = run_dir / f"directory-raw-pass{pass_num}.json"
            dir_rc, dir_stdout, dir_err = run_python("source_directories.py", [
                "--queries", str(queries_file),
                "--output", str(dir_out),
                "--per-query", str(args.directory_per_query),
                "--concurrency", str(min(args.enrich_concurrency, 8)),
            ], timeout=1800)
            if dir_rc == 0 and dir_out.exists():
                dir_count = sum(1 for _ in dir_out.open())
                # Directory output is already in canonical schema, so append
                # directly to the post-filter pool. Skip filter_maps for these.
                with filtered.open("a") as f:
                    f.write(dir_out.read_text())
                filtered_count += dir_count
                raw_count += dir_count
                log(f"directory sourcing: +{dir_count} canonical records")
            else:
                log(f"directory sourcing skipped (rc={dir_rc}): {dir_err[-200:]}")

        # SPEED: run source-fit BEFORE enrich, so we only crawl websites for
        # leads that survived geo/vertical/website checks. In the first run we
        # enriched 359 leads but dropped 185 right after; the rework now skips
        # the wasted ~5 min of crawling.
        source_fit_file = run_dir / f"leads-source-fit-pass{pass_num}.json"
        prefilter = fl.load_jsonl(filtered)
        fit_kept, sf_drops = fl.apply_source_fit(prefilter, cfg)
        fl.write_jsonl(source_fit_file, fit_kept)
        log(f"source_fit kept={len(fit_kept)} of {filtered_count}  drops={dict(sf_drops)}")

        enriched = run_dir / f"leads-enriched-pass{pass_num}.json"
        if args.skip_enrich:
            shutil.copyfile(source_fit_file, enriched)
            enrich_report = {"input": len(fit_kept), "needing_enrichment": 0,
                             "enriched": 0, "skipped": True, "elapsed_s": 0.0}
            log("enrich: skipped (--skip-enrich)")
        else:
            claude_queue = (run_dir / "claude-tasks" / "email-review"
                            if args.claude_review_queue else None)
            enrich_report = enrich_emails(source_fit_file, enriched,
                                          concurrency=args.enrich_concurrency,
                                          page_cap=args.enrich_page_cap,
                                          per_lead_timeout=args.enrich_per_lead_timeout,
                                          global_timeout=args.enrich_global_timeout,
                                          claude_review_queue=claude_queue)
            log(f"after enrich_emails: needing={enrich_report.get('needing_enrichment','?')} "
                f"enriched={enrich_report.get('enriched',0)} "
                f"still_missing={enrich_report.get('still_missing',0)} "
                f"elapsed={enrich_report.get('elapsed_s',0)}s")

        # Optional: complement crawl4ai with MailScout pattern generation (free, local).
        if args.mailscout and not args.skip_enrich:
            enriched_mailscout = enriched.with_stem(enriched.stem + "-mailscout")
            mailscout_report = enrich_emails_mailscout(enriched, enriched_mailscout,
                                                       num_threads=5, smtp_timeout=2)
            log(f"after mailscout: needing={mailscout_report.get('needing_enrichment','?')} "
                f"enriched={mailscout_report.get('enriched',0)} "
                f"elapsed={mailscout_report.get('elapsed_s',0)}s")
            # Use mailscout output as the final enriched file.
            enriched = enriched_mailscout

        resolved_file = run_dir / f"leads-email-resolved-pass{pass_num}.json"
        post_enrich = fl.load_jsonl(enriched)
        resolved, res_drops = fl.resolve_email_pool(post_enrich)
        fl.write_jsonl(resolved_file, resolved)
        sf_report = {
            "source_fit_drops": dict(sf_drops),
            "email_drops": dict(res_drops),
            "source_fit_kept": len(fit_kept),
            "resolved": len(resolved),
        }
        log(f"resolved_email={len(resolved)}  email_drops={dict(res_drops)}")

        scored_file = run_dir / f"leads-scored-pass{pass_num}.json"
        scored = score_pool(resolved, cfg, scored_file)
        # Leads with status `needs_signal` need a website crawl before we can
        # confirm a concrete gap. Only crawl the top-ranked ones (target *
        # source_multiplier) — keeps signal work bounded.
        ranked = sorted(scored, key=lambda r: -(r.get("score") or 0))
        needs_signal = [l for l in ranked if l.get("funnel_status") in {"needs_signal", "signal_rescue"}]
        sig_budget = max(args.target * 3, 30)
        needs_signal_top = needs_signal[:sig_budget]
        if needs_signal_top:
            log(f"signal-extract: {len(needs_signal_top)} candidates "
                f"(top {sig_budget} of {len(needs_signal)} ranked needs_signal)")
            needs_signal_top = extract_signals_for(needs_signal_top, run_dir, args.signal_concurrency)
        # Re-qualify after signals are merged
        qualify = fl.qualify_ranked_signals(needs_signal_top + [l for l in ranked
                                                                if l.get("funnel_status") == "send_ready"],
                                            target=args.target * 2)
        pass_qualified = qualify["qualified"]
        log(f"qualify_ranked_signals: tested={qualify['tested']} "
            f"qualified={len(pass_qualified)} drops={qualify['drops']}")
        for q in pass_qualified:
            key = (q.get("email") or "") + "|" + (q.get("id") or q.get("name") or "")
            if key and key not in all_qualified:
                all_qualified[key] = q

        # STREAMING DRAFTS: emit per-pass drafts immediately so the user has
        # usable output after every pass instead of waiting for the final run.
        if not args.dry_run and pass_qualified:
            pass_drafts = [fl.build_outreach_draft(l, args.run_slug) for l in pass_qualified]
            fl.write_jsonl(run_dir / f"emails-drafted-pass{pass_num}.json", pass_drafts)
            eligible, gate_report = fl.gate_drafts(pass_drafts,
                                                   role_threshold=cfg.get("role_inbox_threshold", 85))
            fl.write_jsonl(run_dir / f"emails-eligible-pass{pass_num}.json", eligible)
            log(f"drafts pass{pass_num}: drafted={len(pass_drafts)} eligible={len(eligible)} "
                f"dropped={gate_report['dropped']}")

        elapsed = round(time.time() - pass_start, 1)
        per_pass_stats.append({
            "pass": pass_num,
            "raw_count": raw_count,
            "filtered_count": filtered_count,
            "email_enriched": enrich_report.get("enriched", 0),
            **sf_report,
            "qualified": len(pass_qualified),
            "qualified_running_total": len(all_qualified),
            "elapsed_s": elapsed,
        })
        log(f"PASS {pass_num} done in {elapsed}s — qualified so far: {len(all_qualified)}/{args.target}")

        # Reap Chromium zombies between passes so the next pass starts with a
        # clean browser pool (without this, RAM creep caused Pass 2's enrich
        # to crash with Browser.close: Connection closed in the previous run).
        killed = reap_chromium_zombies()
        if killed:
            log(f"reaped {killed} orphaned Chromium processes before next pass")

        if len(all_qualified) >= args.target:
            log(f"hit target ({len(all_qualified)} >= {args.target}), stopping early")
            break

    # Final aggregation
    qualified_sorted = sorted(all_qualified.values(), key=lambda r: -(r.get("score") or 0))
    top_n = qualified_sorted[: args.target]
    fl.write_jsonl(run_dir / "qualified-final.json", top_n)

    drafts: list[dict] = []
    if not args.dry_run and top_n:
        drafts = draft_emails(top_n, args.run_slug, run_dir / "emails-drafted.json")
        eligible, gate_report = passes_send_gate(drafts, cfg.get("role_inbox_threshold", 85))
        fl.write_jsonl(run_dir / "emails-eligible.json", eligible)
        log(f"send-gate: eligible={gate_report['eligible']} dropped={gate_report['dropped']}")

    total_elapsed = round(time.time() - overall_start, 1)
    summary = summarize_funnel(per_pass_stats, top_n, drafts, total_elapsed)
    (run_dir / "campaign-summary.txt").write_text(summary)
    print(summary)


if __name__ == "__main__":
    main()
