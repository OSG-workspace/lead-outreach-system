#!/usr/bin/env python3
"""
Directory-site sourcing channel.

Crawls public business directories (Yelp, Yellow Pages, BBB, niche directories
discovered via DDG) using a search query, paginates the listing, and extracts
business candidates with name/website/phone. Outputs the canonical lead schema
used by filter_maps_leads.py so the downstream funnel stays unchanged.

This is a complement to lead-sourcing-maps for briefs where Maps coverage is
sparse — niche B2B verticals, professional services, anything that lives in
industry directories rather than on Google Maps as a storefront.

Usage:
    ./tools/run.sh tools/scripts/source_directories.py \\
        --queries runs/<slug>/queries.txt \\
        --output  runs/<slug>/directory-raw.json \\
        [--per-query 30] [--concurrency 4]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

try:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
except ImportError:
    print("error: crawl4ai not installed. Run setup-tools first.", file=sys.stderr)
    sys.exit(1)

try:
    from ddgs import DDGS
except ImportError:
    print("error: ddgs not installed. Run setup-tools first.", file=sys.stderr)
    sys.exit(1)

DIRECTORY_HINTS = (
    "yelp.com", "yellowpages.com", "bbb.org", "manta.com", "thumbtack.com",
    "houzz.com", "angi.com", "chamberofcommerce.com", "dnb.com",
    "clutch.co", "goodfirms.co", "trustpilot.com",
    "europages.com", "kompass.com",
    "yellowpages.ae", "yellowpages.qa", "yellowpages.com.sa",
    "dubizzle.com", "connectsaudi.com",
)

LISTING_HINTS = ("biz/", "/business/", "/listing/", "/profile/", "/company/",
                 "/c/", "/find_", "directory/", "/listings/")

BUSINESS_LINK_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
EXTERNAL_LINK_RE = re.compile(r'<a[^>]+href=["\']https?://([^/"\'\s]+)[^"\']*["\']', re.IGNORECASE)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_directory_host(host: str) -> bool:
    return any(hint in host for hint in DIRECTORY_HINTS)


async def discover_directory_pages(query: str, per_query: int) -> list[str]:
    """Use DDG to find directory listing pages for the query."""
    urls: list[str] = []
    try:
        with DDGS() as ddgs:
            for site in DIRECTORY_HINTS[:8]:
                q = f"{query} site:{site}"
                try:
                    results = ddgs.text(q, max_results=max(per_query // 4, 5))
                except Exception:
                    continue
                for r in results or []:
                    href = r.get("href") or r.get("url") or ""
                    if not href:
                        continue
                    host = domain_of(href)
                    if is_directory_host(host) and href not in urls:
                        urls.append(href)
                    if len(urls) >= per_query:
                        break
                if len(urls) >= per_query:
                    break
    except Exception as exc:
        print(f"  ddg discovery failed for {query!r}: {exc}", file=sys.stderr)
    return urls


def extract_candidates_from_listing(html: str, page_url: str) -> list[dict]:
    """Pull external business links + names out of a directory listing page.

    We assume directories link out to each business's own site (or to a
    detail page); external domains that are NOT other directory hosts are
    treated as candidate business websites.
    """
    out: list[dict] = []
    seen_hosts: set[str] = set()
    page_host = domain_of(page_url)
    for m in EXTERNAL_LINK_RE.finditer(html or ""):
        host = m.group(1).lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host == page_host:
            continue
        if is_directory_host(host):
            continue
        if any(skip in host for skip in ("google.com", "facebook.com", "instagram.com",
                                          "twitter.com", "linkedin.com", "youtube.com",
                                          "tiktok.com", "pinterest.com", "x.com",
                                          "apple.com", "microsoft.com", "amazon.com",
                                          "wp.com", "wikipedia.org", "wordpress.com",
                                          "shopify.com", "godaddy.com")):
            continue
        if host in seen_hosts:
            continue
        seen_hosts.add(host)
        out.append({
            "id": f"dir-{re.sub(r'[^a-z0-9]+', '-', host)[:64]}",
            "name": host.split(".")[0].replace("-", " ").title(),
            "source": "web",
            "url": f"https://{host}",
            "raw": {
                "discovered_from": page_url,
                "directory_host": page_host,
            },
        })
    return out


async def crawl_listing(crawler: AsyncWebCrawler, url: str, timeout: int = 15) -> str:
    cfg = CrawlerRunConfig(
        word_count_threshold=5,
        excluded_tags=["script", "style"],
        only_text=False,
        page_timeout=timeout * 1000,
    )
    try:
        r = await asyncio.wait_for(crawler.arun(url=url, config=cfg), timeout=timeout + 5)
    except Exception:
        return ""
    if not r or not getattr(r, "success", False):
        return ""
    return r.cleaned_html or ""


async def source_from_queries(queries: list[str], per_query: int, concurrency: int) -> list[dict]:
    all_candidates: dict[str, dict] = {}
    sem = asyncio.Semaphore(concurrency)

    async with AsyncWebCrawler(headless=True, verbose=False) as crawler:
        async def handle_query(query: str) -> None:
            listing_urls = await discover_directory_pages(query, per_query=per_query)
            print(f"  [{query}] discovered {len(listing_urls)} listing pages",
                  file=sys.stderr, flush=True)

            async def handle_one(url: str) -> None:
                async with sem:
                    html = await crawl_listing(crawler, url)
                    if not html:
                        return
                    for cand in extract_candidates_from_listing(html, url):
                        host = domain_of(cand["url"])
                        if host and host not in all_candidates:
                            cand["raw"]["query"] = query
                            all_candidates[host] = cand

            await asyncio.gather(*(handle_one(u) for u in listing_urls), return_exceptions=True)

        await asyncio.gather(*(handle_query(q) for q in queries), return_exceptions=True)

    return list(all_candidates.values())


async def main_async() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--queries", required=True, help="Path to queries.txt")
    p.add_argument("--output", required=True, help="Output JSONL of canonical leads")
    p.add_argument("--per-query", type=int, default=30,
                   help="Max listing pages to crawl per query.")
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()

    queries_path = Path(args.queries)
    out_path = Path(args.output)
    queries = [q.strip() for q in queries_path.read_text().splitlines() if q.strip()]
    if not queries:
        print("error: queries file is empty", file=sys.stderr)
        sys.exit(2)

    print(f"directory sourcing: {len(queries)} queries, per_query={args.per_query} "
          f"concurrency={args.concurrency}", file=sys.stderr, flush=True)
    candidates = await source_from_queries(queries, args.per_query, args.concurrency)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in candidates))
    print(json.dumps({
        "queries": len(queries),
        "candidates": len(candidates),
        "output": str(out_path),
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async())
