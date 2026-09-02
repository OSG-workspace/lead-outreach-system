#!/usr/bin/env python3
"""Stage 4 (crawl4ai edition): JS-rendering fetch of homepage + contact/about/team
pages for every domain in candidates-all.txt.

Replaces the old static-curl fetch_html.sh. Static curl could not see emails on
JS-rendered SPAs, Cloudflare-challenged pages, or sites that inject the mailto
link client-side — which was ~75% of the extract-stage leak (300 fetched pages
-> only 13 leads). crawl4ai drives a real headless Chromium (Playwright), so the
DOM is fully rendered before we scrape it. Smoke-verified: cosmesurge.com/contact
yields info@cosmesurge.com under crawl4ai where static curl returned zero emails.

Output contract is identical to the old script so extract_leads.py is unchanged:
  raw_html/{safe_domain}__{slug}.html   slug in {home,contact,contactus,about,aboutus,team}

Idempotent: skips files already present. Tiny (<500 char) renders are dropped.

Per the kill-on-fallback rule, this script does NOT silently fall back to curl.
If crawl4ai/Playwright is unavailable it exits non-zero so the orchestrator halts
rather than shipping a degraded (homepage-only, JS-blind) campaign.

MULTI-BROWSER ARCHITECTURE (2026-08-19). One AsyncWebCrawler owns ONE Chromium
process, and cranking its internal semaphore does not scale throughput past
that process's own ceiling — measured on this machine: concurrency 4/8/12/20
inside a single browser plateaus at ~1.1-1.2 pages/sec, with avg per-page time
climbing from 4.8s to 12.1s as concurrency rises (pure contention, no gain).
Splitting the same work across independent browser PROCESSES roughly multiplies
that ceiling instead: 1 browser at concurrency=8 measured 1.08 pages/sec; 2
browsers at concurrency=6 each measured 2.02 pages/sec (near-linear). This is
why 2026-08-18-au-trades' fetch of 612 domains x 6 pages took ~3h50m on a
single-browser concurrency=12 setup — that setup was already past its own
throughput ceiling and adding more concurrency only added latency, never yield.
Domains are sharded round-robin across FETCH_BROWSERS independent
AsyncWebCrawler instances, each with its own FETCH_CONCURRENCY semaphore.

PER-FIXTURE PAGE TRIMMING. The default PAGES list below fetches 6 paths per
domain, including /team, /leadership, /people — pages that plausibly exist for
a clinic, law firm, or property manager, but essentially never exist for a
solo plumber or electrician (a trades run pays the SAME fetch cost for those
3 speculative paths as for home+contact, for near-zero yield). A fixture can
override the page list by shipping `fetch_pages.txt` (one `slug|path` per
line, e.g. `home|/`) alongside its other config files — copied into the run
dir exactly like icp.yaml/sourcing.json/pitch.json already are. Absent that
file, every fixture gets the full default list, unchanged.

Usage: fetch_html.py <run-dir>
Env:   FETCH_BROWSERS (default cpu_count//2+1, capped [2,8], independent
       Chromium processes), FETCH_CONCURRENCY (default 6, PER BROWSER — total
       effective concurrency is FETCH_BROWSERS x FETCH_CONCURRENCY),
       FETCH_PAGE_TIMEOUT_MS (default 25000)
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Path slug -> URL path. Homepage + contact/about + people pages. The people
# pages (team/leadership/management/doctors) are where a decision-maker's real
# personal email most often lives — they feed the Stage 5.5 email harvester.
# Trimmed from 10 to 6: the dropped variants (contactus/aboutus/ourteam/
# management/doctors) almost never resolve to a DISTINCT page from their
# canonical sibling, so fetching them mostly cost wall-clock + raw_html bulk
# (which then bloats every downstream agent prompt) for ~no extra emails.
# Override with FETCH_PAGES=full to restore the wide list if a vertical needs it,
# or ship a per-fixture fetch_pages.txt (see module docstring) to trim it further.
DEFAULT_PAGES: list[tuple[str, str]] = [
    ("home", "/"),
    ("contact", "/contact"),
    ("about", "/about"),
    ("team", "/team"),
    ("leadership", "/leadership"),
    ("people", "/people"),
]
if os.environ.get("FETCH_PAGES") == "full":
    DEFAULT_PAGES += [
        ("contactus", "/contact-us"),
        ("aboutus", "/about-us"),
        ("ourteam", "/our-team"),
        ("management", "/management"),
        ("doctors", "/doctors"),
    ]

MIN_HTML_LEN = 500
# Below this share of domains yielding ANY page, the fetch is probably broken
# rather than the market being thin. Warn, never halt — see the note at the
# yield check in run() for why a hard gate here would cost more than it saves.
FETCH_YIELD_WARN_PCT = float(os.environ.get("FETCH_YIELD_WARN_PCT", "40"))
# CPU-aware default: measured on a 10-core machine, browser count 3/4/6/8 gave
# 70s/57s/38s/44s for the same 180-task batch — throughput climbs steadily
# through 6, then contention eats the gain by 8. cpu_count//2+1 lands on 6 for
# 10 cores and scales down for smaller machines rather than assuming everyone
# has 10 cores to spare; capped at 8 as a safety ceiling, not a proven optimum
# above this machine. FETCH_BROWSERS overrides outright if a fixture or
# operator knows better for their hardware.
_DEFAULT_BROWSERS = max(2, min(8, (os.cpu_count() or 8) // 2 + 1))
BROWSERS = max(1, int(os.environ.get("FETCH_BROWSERS", str(_DEFAULT_BROWSERS))))
CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "6"))
PAGE_TIMEOUT_MS = int(os.environ.get("FETCH_PAGE_TIMEOUT_MS", "25000"))
# Hard per-page wall-clock guard so one hung page can't stall its browser.
HARD_TIMEOUT_S = PAGE_TIMEOUT_MS / 1000 + 12


def load_domains(run_dir: Path) -> list[str]:
    cand = run_dir / "candidates-all.txt"
    domains: list[str] = []
    seen: set[str] = set()
    for line in cand.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        domain = line.split("|", 1)[0].strip().lower()
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def load_pages(run_dir: Path) -> list[tuple[str, str]]:
    """A fixture's fetch_pages.txt (`slug|path` per line) if present, else the
    built-in default. Copied into the run dir like every other fixture file."""
    override = run_dir / "fetch_pages.txt"
    if not override.exists():
        return DEFAULT_PAGES
    pages: list[tuple[str, str]] = []
    for line in override.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            pages.append((parts[0].strip(), parts[1].strip()))
    if not pages:
        print(f"WARN: {override} has no usable `slug|path` rows — falling back "
              f"to the built-in default page list.", file=sys.stderr)
        return DEFAULT_PAGES
    print(f"fetch_pages.txt: using {len(pages)} page(s) per domain "
          f"({', '.join(s for s, _ in pages)}) instead of the "
          f"{len(DEFAULT_PAGES)}-page default.")
    return pages


async def fetch_page(crawler, run_cfg, sem, out_dir: Path, domain: str, slug: str, path: str) -> bool:
    safe_domain = domain.replace("/", "_")
    outfile = out_dir / f"{safe_domain}__{slug}.html"
    if outfile.exists():  # idempotent: never refetch
        return False
    url = f"https://{domain}{path}"
    async with sem:
        try:
            result = await asyncio.wait_for(crawler.arun(url=url, config=run_cfg), timeout=HARD_TIMEOUT_S)
        except Exception:
            return False
    if not getattr(result, "success", False):
        return False
    html = getattr(result, "html", "") or ""
    if len(html) < MIN_HTML_LEN:
        return False
    try:
        outfile.write_text(html, errors="ignore")
    except TypeError:  # Path.write_text in some builds rejects errors=
        outfile.write_text(html)
    return True


async def browser_worker(browser_cfg, run_cfg, out_dir: Path, tasks: list[tuple[str, str, str]]) -> int:
    """One independent Chromium process working its shard of (domain, slug, path)
    tuples. Each worker owns its own AsyncWebCrawler — see the module docstring
    for why this, not a bigger semaphore on one shared browser, is what scales."""
    from crawl4ai import AsyncWebCrawler
    sem = asyncio.Semaphore(CONCURRENCY)
    written = 0
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        coros = [fetch_page(crawler, run_cfg, sem, out_dir, d, slug, path)
                for d, slug, path in tasks]
        for coro in asyncio.as_completed(coros):
            if await coro:
                written += 1
    return written


async def run(run_dir: Path) -> int:
    from crawl4ai import BrowserConfig, CrawlerRunConfig, CacheMode

    out_dir = run_dir / "raw_html"
    out_dir.mkdir(parents=True, exist_ok=True)
    domains = load_domains(run_dir)
    if not domains:
        print("No domains in candidates-all.txt — nothing to fetch.", file=sys.stderr)
        return 3
    pages = load_pages(run_dir)

    browser_cfg = BrowserConfig(headless=True, browser_type="chromium", verbose=False)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
        verbose=False,
        scan_full_page=False,
    )

    all_tasks = [(d, slug, path) for d in domains for slug, path in pages]
    # Round-robin shard so each browser gets a representative mix of domains
    # rather than one browser inheriting a long run of slow/dead sites.
    shards: list[list[tuple[str, str, str]]] = [[] for _ in range(BROWSERS)]
    for i, t in enumerate(all_tasks):
        shards[i % BROWSERS].append(t)

    print(f"Fetching {len(all_tasks)} pages across {len(domains)} domains -> "
          f"{BROWSERS} browser(s) x {CONCURRENCY} concurrency each "
          f"({BROWSERS * CONCURRENCY} effective).")
    results = await asyncio.gather(
        *[browser_worker(browser_cfg, run_cfg, out_dir, shard) for shard in shards]
    )
    written = sum(results)

    distinct = len({p.name.split("__", 1)[0] for p in out_dir.glob("*.html")})
    total_pages = len(list(out_dir.glob("*.html")))
    print(f"Fetched {written} new pages (crawl4ai JS-render). "
          f"raw_html now holds {total_pages} pages across {distinct}/{len(domains)} domains -> {out_dir}")

    # Domain-yield health. ARCHITECTURE.md long claimed Stage 4 "halts if <40%
    # of domains yielded a page"; no such check existed anywhere, so a collapsed
    # fetch looked identical to a healthy one and only showed up later as a thin
    # extract. This WARNS rather than halts, deliberately: lifetime
    # fetch->extract survival is 50.7% and ranges 39-82% across real runs, so a
    # hard gate at 40% would abort legitimate runs in weakly-mapped markets and
    # throw away sourcing that already cost real work. The operator gets the
    # signal; the run keeps its leads.
    if domains:
        pct = 100.0 * distinct / len(domains)
        if pct < FETCH_YIELD_WARN_PCT:
            print(f"WARN: only {distinct}/{len(domains)} domains ({pct:.0f}%) yielded any "
                  f"page, below the {FETCH_YIELD_WARN_PCT:.0f}% health line. Everything "
                  f"downstream is capped by this. Usual causes: network/DNS trouble, a "
                  f"crawl4ai/playwright browser that failed to start, or bulk-blocked "
                  f"requests. Check a couple of these domains by hand before trusting "
                  f"this run's yield.")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: fetch_html.py <run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(sys.argv[1]).resolve()
    if not (run_dir / "candidates-all.txt").exists():
        print(f"No candidates-all.txt under {run_dir}", file=sys.stderr)
        return 2
    try:
        import crawl4ai  # noqa: F401
    except Exception as e:  # kill-on-fallback: do NOT degrade to curl
        print(f"FATAL: crawl4ai unavailable ({e}). Refusing to fall back to JS-blind curl. "
              f"Install with: pip install crawl4ai && python -m playwright install chromium", file=sys.stderr)
        return 4
    return asyncio.run(run(run_dir))


if __name__ == "__main__":
    raise SystemExit(main())
