#!/usr/bin/env python3
"""
Token-cheap signal extractor for gap analysis.

Fetches a URL via crawl4ai, then returns ONLY the structured signals Claude
needs to identify a business gap — no full-page markdown.

Typical full-page markdown is 5-20KB. This script returns ~500-1500 bytes per
page, dropping context cost by ~10x while preserving everything that drives
gap identification:

  - title, meta description, language
  - h1/h2/h3 (the structural skeleton)
  - emails, phones, social links
  - footer copyright year (staleness signal)
  - tech stack signals (WordPress, Shopify, Wix, etc.)
  - presence of booking widgets, contact forms, tracking pixels, mobile viewport
  - word count and SSL flag
  - first 240 chars of any "about" section (only)

Full markdown is still saved to <output>.full.md alongside, so a follow-up
deep-dive can read it on demand without re-fetching.

Usage:
    # Single URL
    python3 extract_signals.py --url https://example.com \\
        --output runs/<slug>/signals/<lead-id>.json

    # Batch (read URLs from JSONL with {url, id} per line)
    python3 extract_signals.py --batch runs/<slug>/qualified-urls.jsonl \\
        --out-dir runs/<slug>/signals/
"""

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

try:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
except ImportError:
    print("error: crawl4ai not installed. Run setup-tools first.", file=sys.stderr)
    sys.exit(1)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
COPYRIGHT_RE = re.compile(r"©\s*(20\d\d)")
ABOUT_RE = re.compile(
    r"(?:^|\n)#{1,3}\s*(?:about(?:\s+us)?|who\s+we\s+are|our\s+story)[^\n]*\n+(.+?)(?=\n#{1,3}\s|\Z)",
    re.IGNORECASE | re.DOTALL,
)

TECH_PATTERNS = {
    "wordpress": ["wp-content/", "wp-includes/", "wp-json"],
    "shopify": ["cdn.shopify.com", "myshopify.com", "/cdn/shop/"],
    "wix": ["wixstatic.com", "wix.com/website-builder"],
    "squarespace": ["squarespace.com", "static1.squarespace.com"],
    "webflow": ["webflow.com", "assets.website-files.com"],
    "framer": ["framer.com", "framerusercontent.com"],
    "next.js": ["/_next/", "__NEXT_DATA__"],
    "react": ["react-dom", "data-reactroot"],
}

BOOKING_PATTERNS = ["calendly.com", "acuityscheduling.com", "square.site/book",
                    "cal.com", "/book", "booking.com", "tidycal.com",
                    "savvycal.com", "youcanbook.me"]

TRACKING_PATTERNS = {
    "google_analytics": ["gtag/js", "google-analytics.com", "googletagmanager"],
    "facebook_pixel": ["fbevents.js", "connect.facebook.net"],
    "hotjar": ["hotjar.com"],
    "intercom": ["intercom.io", "intercomcdn.com"],
}

SOCIAL_DOMAINS = {
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "linkedin": "linkedin.com",
    "twitter": "twitter.com",
    "tiktok": "tiktok.com",
    "youtube": "youtube.com",
}

GENERIC_EMAIL_LOCAL = {"noreply", "no-reply", "donotreply", "mailer-daemon",
                       "postmaster", "webmaster"}


def detect_tech_stack(html: str) -> list[str]:
    found = []
    h = html.lower()
    for tech, patterns in TECH_PATTERNS.items():
        if any(p.lower() in h for p in patterns):
            found.append(tech)
    return found


def detect_tracking(html: str) -> list[str]:
    found = []
    h = html.lower()
    for kind, patterns in TRACKING_PATTERNS.items():
        if any(p.lower() in h for p in patterns):
            found.append(kind)
    return found


def has_booking(html: str) -> bool:
    h = html.lower()
    return any(p in h for p in BOOKING_PATTERNS)


def has_mobile_viewport(html: str) -> bool:
    return bool(re.search(r'<meta[^>]+name=["\']?viewport["\']?', html, re.IGNORECASE))


def has_form(html: str) -> bool:
    return bool(re.search(r"<form\b", html, re.IGNORECASE))


def extract_social_links(html: str, links_data) -> dict:
    """Pull social URLs from the structured links list and the raw HTML."""
    found: dict[str, str] = {}
    candidates = []

    if isinstance(links_data, dict):
        for bucket in ("internal", "external"):
            for entry in links_data.get(bucket, []) or []:
                href = (entry.get("href") if isinstance(entry, dict) else entry) or ""
                if href:
                    candidates.append(href)
    elif isinstance(links_data, list):
        for entry in links_data:
            href = (entry.get("href") if isinstance(entry, dict) else entry) or ""
            if href:
                candidates.append(href)

    candidates.extend(re.findall(r'https?://[^"\s<>]+', html or ""))

    for href in candidates:
        for name, domain in SOCIAL_DOMAINS.items():
            if domain in href and name not in found:
                found[name] = href.split("?")[0].rstrip("/")
    return found


def headings(markdown: str, limit_per_level: int = 8) -> dict:
    out = {"h1": [], "h2": [], "h3": []}
    if not markdown:
        return out
    for match in HEADING_RE.finditer(markdown):
        level = f"h{len(match.group(1))}"
        text = match.group(2).strip()[:120]
        if len(out[level]) < limit_per_level and text:
            out[level].append(text)
    return out


def quality_emails(text: str, current_domain: Optional[str] = None) -> list[str]:
    raw = set(EMAIL_RE.findall(text or ""))
    valid = []
    for e in raw:
        local = e.split("@")[0].lower()
        if local in GENERIC_EMAIL_LOCAL:
            continue
        if current_domain:
            domain = e.split("@")[1].lower()
            if domain != current_domain.lower() and not domain.endswith("." + current_domain.lower()):
                continue
        valid.append(e)
    return sorted(set(valid))[:5]


def about_excerpt(markdown: str) -> Optional[str]:
    m = ABOUT_RE.search(markdown or "")
    if not m:
        return None
    text = m.group(1).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:240]


_FETCH_CONFIG = CrawlerRunConfig(
    word_count_threshold=10,
    excluded_tags=["script", "style"],
    only_text=False,
)


async def fetch(url: str, crawler: Optional[AsyncWebCrawler] = None) -> Optional[dict]:
    """Fetch one URL.

    When `crawler` is provided, it is reused (no per-call browser launch).
    When omitted, a fresh crawler is spun up — kept for the --url single-call
    code path.
    """
    own_crawler = crawler is None
    if own_crawler:
        crawler = AsyncWebCrawler(headless=True, verbose=False)
        await crawler.__aenter__()
    try:
        result = await crawler.arun(url=url, config=_FETCH_CONFIG)
        if not result.success:
            return None
        return {
            "final_url": result.url,
            "markdown": result.markdown or "",
            "html": result.cleaned_html or "",
            "metadata": result.metadata or {},
            "links": result.links,
        }
    except Exception:
        return None
    finally:
        if own_crawler:
            await crawler.__aexit__(None, None, None)


def build_signals(url: str, page: dict) -> dict:
    md = page.get("markdown") or ""
    html = page.get("html") or ""
    meta = page.get("metadata") or {}
    parsed = urlparse(page.get("final_url") or url)
    root_domain = ".".join(parsed.netloc.split(".")[-2:]) if parsed.netloc else None

    copyright_year = None
    cm = COPYRIGHT_RE.search(html) or COPYRIGHT_RE.search(md)
    if cm:
        copyright_year = int(cm.group(1))

    word_count = len(re.findall(r"\w+", md))

    return {
        "url": url,
        "final_url": page.get("final_url"),
        "fetched": True,
        "title": (meta.get("title") or "").strip()[:200] or None,
        "meta_description": (meta.get("description") or "").strip()[:300] or None,
        "language": meta.get("language"),
        "ssl": parsed.scheme == "https",
        "headings": headings(md),
        "emails": quality_emails(md + "\n" + html, root_domain),
        "phones": list({p.strip() for p in PHONE_RE.findall(md)})[:5],
        "social_links": extract_social_links(html, page.get("links")),
        "copyright_year": copyright_year,
        "tech_stack": detect_tech_stack(html),
        "tracking_pixels": detect_tracking(html),
        "has_booking_widget": has_booking(html),
        "has_contact_form": has_form(html),
        "has_mobile_viewport": has_mobile_viewport(html),
        "word_count": word_count,
        "about_excerpt": about_excerpt(md),
    }


async def process_one(url: str, signals_path: Path,
                      full_md_dir: Optional[Path] = None,
                      lead_id: Optional[str] = None,
                      crawler: Optional[AsyncWebCrawler] = None) -> dict:
    page = await fetch(url, crawler=crawler)
    if page is None:
        signals = {
            "url": url,
            "fetched": False,
            "error": "fetch_failed",
        }
    else:
        signals = build_signals(url, page)
        if lead_id:
            signals["lead_id"] = lead_id
        if full_md_dir is not None:
            full_md_dir.mkdir(parents=True, exist_ok=True)
            md_name = (lead_id or re.sub(r"[^a-z0-9]+", "-", url.lower())[:60]) + ".md"
            (full_md_dir / md_name).write_text(page.get("markdown") or "", encoding="utf-8")
            signals["full_markdown_path"] = str(full_md_dir / md_name)

    signals_path.parent.mkdir(parents=True, exist_ok=True)
    signals_path.write_text(json.dumps(signals, ensure_ascii=False, indent=2), encoding="utf-8")
    return signals


async def main_async():
    p = argparse.ArgumentParser()
    p.add_argument("--url", help="single URL to extract signals from")
    p.add_argument("--output", help="output JSON file (single mode)")
    p.add_argument("--batch", help="JSONL file with {url, id} per line")
    p.add_argument("--out-dir", help="output dir (batch mode)")
    p.add_argument("--save-full-markdown", action="store_true",
                   help="also write full markdown alongside signals (default: off)")
    p.add_argument("--concurrency", type=int, default=10)
    args = p.parse_args()

    if args.url and args.output:
        out = Path(args.output)
        full_dir = out.parent / "_full" if args.save_full_markdown else None
        signals = await process_one(args.url, out, full_dir)
        print(json.dumps({
            "url": args.url,
            "output": str(out),
            "fetched": signals.get("fetched"),
            "size_bytes": out.stat().st_size if out.exists() else 0,
        }, indent=2))
        return

    if args.batch and args.out_dir:
        batch_path = Path(args.batch)
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        full_dir = out_dir / "_full" if args.save_full_markdown else None

        jobs = []
        with batch_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                url = rec.get("url")
                lead_id = rec.get("id") or rec.get("lead_id")
                if not url or not lead_id:
                    continue
                jobs.append((url, lead_id))

        sem = asyncio.Semaphore(args.concurrency)
        results = []

        # ONE browser, reused across every URL in the batch. Previous version
        # spawned a fresh Chromium per URL (3-5s overhead each); for 100 leads
        # that was 5-10 minutes of pure browser startup. Now it's one launch.
        async with AsyncWebCrawler(headless=True, verbose=False) as crawler:
            async def run_one(u, lid):
                async with sem:
                    signals_file = out_dir / f"{lid}.json"
                    sig = await process_one(u, signals_file, full_dir, lid, crawler=crawler)
                    results.append({
                        "id": lid,
                        "url": u,
                        "fetched": sig.get("fetched"),
                        "output": str(signals_file),
                    })

            await asyncio.gather(*(run_one(u, lid) for u, lid in jobs))
        print(json.dumps({
            "batch_size": len(jobs),
            "fetched_ok": sum(1 for r in results if r["fetched"]),
            "failed": sum(1 for r in results if not r["fetched"]),
            "out_dir": str(out_dir),
        }, indent=2))
        return

    p.print_help()
    sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main_async())
