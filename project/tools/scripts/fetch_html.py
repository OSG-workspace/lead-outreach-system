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

Usage: fetch_html.py <run-dir>
Env:   FETCH_CONCURRENCY (default 10), FETCH_PAGE_TIMEOUT_MS (default 25000)
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
# Override with FETCH_PAGES=full to restore the wide list if a vertical needs it.
import os as _os
PAGES: list[tuple[str, str]] = [
    ("home", "/"),
    ("contact", "/contact"),
    ("about", "/about"),
    ("team", "/team"),
    ("leadership", "/leadership"),
    ("people", "/people"),
]
if _os.environ.get("FETCH_PAGES") == "full":
    PAGES += [
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
CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "12"))
PAGE_TIMEOUT_MS = int(os.environ.get("FETCH_PAGE_TIMEOUT_MS", "25000"))
# Hard per-page wall-clock guard so one hung page can't stall the whole run.
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


async def run(run_dir: Path) -> int:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

    out_dir = run_dir / "raw_html"
    out_dir.mkdir(parents=True, exist_ok=True)
    domains = load_domains(run_dir)
    if not domains:
        print("No domains in candidates-all.txt — nothing to fetch.", file=sys.stderr)
        return 3

    browser_cfg = BrowserConfig(headless=True, browser_type="chromium", verbose=False)
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        page_timeout=PAGE_TIMEOUT_MS,
        wait_until="domcontentloaded",
        verbose=False,
        scan_full_page=False,
    )
    sem = asyncio.Semaphore(CONCURRENCY)

    written = 0
    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        tasks = [
            fetch_page(crawler, run_cfg, sem, out_dir, d, slug, path)
            for d in domains
            for slug, path in PAGES
        ]
        for coro in asyncio.as_completed(tasks):
            if await coro:
                written += 1

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
