#!/usr/bin/env python3
"""
Website email enrichment.

Leads scraped from Google Maps frequently lack a public email even though the
business has one on its `/contact` or `/about` page. This script takes those
no-email leads, crawls a small set of likely pages per lead with a SHARED
browser, extracts emails (filtering image filenames, role inboxes when the
score won't earn one later, freemail placeholders, etc.), and writes the
result back into the lead record under `dm_email` / `email`.

Designed for batches of 100+ leads: one Chromium launch, async semaphore,
short page timeout, hard cap on per-lead pages fetched.

Usage:
    ./tools/run.sh tools/scripts/enrich_emails.py \\
        --input  runs/<slug>/leads-raw.json \\
        --output runs/<slug>/leads-enriched.json \\
        [--concurrency 10] [--per-lead-page-cap 3] [--timeout 12]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

try:
    from crawl4ai import AsyncWebCrawler, CrawlerRunConfig
except ImportError:
    print("error: crawl4ai not installed. Run setup-tools first.", file=sys.stderr)
    sys.exit(1)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ROLE_LOCAL_RE = re.compile(
    r"^(info|contact|hello|hi|sales|admin|support|noreply|no-reply|"
    r"marketing|hr|jobs|careers|booking|appointment|reception|office|"
    r"team|enquiry|enquiries|inquiry|inquiries|mail|email|service|"
    r"customercare|customerservice|help|feedback|complaints|webmaster|"
    r"finance|accounts|billing|news|newsletter|abuse|postmaster|"
    r"general|operations|ops|frontdesk|press|media|pr)@",
    re.IGNORECASE,
)
JUNK_EMAIL_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|html?)$|"
    r"@(example\.com|example\.org|yourdomain\.com|domain\.com|test\.com|"
    r"email\.com|mail\.com|abc\.xyz|sentry\.io|wpforms\.com|wixpress\.com)$|"
    r"@.*\.(gov|mil|edu)$|\.(gov|mil|edu)\.|"
    r"^(\d+x|[a-f0-9]{16,})@",  # CDN hash filenames
    re.IGNORECASE,
)
FREEMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "live.com", "aol.com",
}

# Pages that most often hold a real contact email.
CANDIDATE_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/team", "/our-team"]

ROLE_KEYWORDS_RE = re.compile(
    r"\b(founder|co-?founder|owner|ceo|cto|coo|cfo|managing\s+director|"
    r"general\s+manager|principal|partner|director|president|"
    r"head\s+of|chief\s+\w+|md|gm)\b",
    re.IGNORECASE,
)


def _looks_like_owner_signal(text: str) -> bool:
    return bool(ROLE_KEYWORDS_RE.search(text or ""))


def root_of(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        if not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return ""


def domain_of(url: str) -> str:
    try:
        host = urlparse(url if "://" in url else "https://" + url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def root_domain(host_or_email_or_url: str) -> str:
    s = (host_or_email_or_url or "").lower().strip()
    if not s:
        return ""
    if "://" in s:
        # It's a URL — extract netloc properly.
        s = urlparse(s).netloc
    elif "@" in s:
        s = s.split("@", 1)[1]
    # Strip explicit port (host:port), keep hostname only.
    s = s.split(":", 1)[0]
    if s.startswith("www."):
        s = s[4:]
    parts = [p for p in s.split(".") if p]
    if len(parts) < 2:
        return s
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "co", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def classify_email(email: str) -> str:
    e = email.strip().lower()
    if not e or "@" not in e or e.count("@") != 1:
        return "junk"
    local, dom = e.split("@", 1)
    if len(local) < 3 or JUNK_EMAIL_RE.search(e):
        return "junk"
    if ROLE_LOCAL_RE.match(e):
        return "role"
    if dom in FREEMAIL_DOMAINS:
        return "personal"
    return "person"


def extract_emails(text: str) -> list[str]:
    seen = []
    for m in EMAIL_RE.finditer(text or ""):
        e = m.group(0).strip().lower()
        if e and e not in seen:
            seen.append(e)
    return seen


def pick_best(candidates: list[str], lead_domain: str) -> tuple[str, str]:
    """Return (best_email, classification) ranking person > personal > role > junk.

    Person-class emails MUST share the lead's website root domain — otherwise
    they're someone else's address on a directory page.
    """
    scored: list[tuple[int, str, str]] = []
    for e in candidates:
        c = classify_email(e)
        if c == "junk":
            continue
        if c == "person" and lead_domain:
            if root_domain(e) != root_domain(lead_domain):
                continue
        rank = {"person": 0, "personal": 1, "role": 2}[c]
        scored.append((rank, e, c))
    if not scored:
        return "", "missing"
    scored.sort(key=lambda x: x[0])
    _, email, klass = scored[0]
    return email, klass


async def fetch(crawler: AsyncWebCrawler, url: str, timeout: int) -> str:
    """Fetch one URL, return concatenated markdown + html text. Empty on failure."""
    cfg = CrawlerRunConfig(
        word_count_threshold=5,
        excluded_tags=["script", "style"],
        only_text=False,
        page_timeout=timeout * 1000,
    )
    try:
        r = await asyncio.wait_for(crawler.arun(url=url, config=cfg), timeout=timeout + 5)
    except (asyncio.TimeoutError, Exception):
        return ""
    if not r or not getattr(r, "success", False):
        return ""
    return (r.markdown or "") + "\n" + (r.cleaned_html or "")


async def enrich_one(crawler: AsyncWebCrawler, lead: dict,
                     per_lead_cap: int, timeout: int,
                     queue_for_claude: bool = False,
                     queue_dir: Path | None = None) -> dict:
    """Try to attach a real email to one lead. Mutates and returns the lead.

    If `queue_for_claude` is set, leads whose regex extraction failed but
    whose page text shows an owner/founder/CEO are written as a structured
    review task to `queue_dir/<lead_id>.json`. The Claude session (Haiku
    or Opus, in-context) processes those queue files later — no network
    calls from this script.
    """
    website = lead.get("url") or lead.get("website") or (lead.get("raw") or {}).get("website") or ""
    if not website or not website.startswith(("http://", "https://")):
        return lead

    root = root_of(website)
    if not root:
        return lead

    pages_to_try = [website] + [urljoin(root + "/", p.lstrip("/")) for p in CANDIDATE_PATHS]
    seen, ordered = set(), []
    for p in pages_to_try:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    ordered = ordered[: per_lead_cap + 1]

    all_emails: list[str] = []
    fetched_text_chunks: list[str] = []
    for url in ordered:
        text = await fetch(crawler, url, timeout)
        if text:
            fetched_text_chunks.append(text)
        for e in extract_emails(text):
            if e not in all_emails:
                all_emails.append(e)
        best, klass = pick_best(all_emails, root)
        if best and klass == "person":
            break

    best, klass = pick_best(all_emails, root)

    # If regex failed AND the page shows an owner/founder/CEO, write a
    # structured task file for the Claude session to review. Never call
    # the Anthropic API from here.
    if (queue_for_claude and queue_dir is not None
            and (not best or klass not in {"person", "personal"})):
        merged_text = "\n".join(fetched_text_chunks)[:4000]
        if _looks_like_owner_signal(merged_text):
            queue_dir.mkdir(parents=True, exist_ok=True)
            lead_id = lead.get("id") or re.sub(r"[^a-z0-9]+", "-", (lead.get("name") or "").lower())[:60] or "lead"
            task_path = queue_dir / f"{lead_id}.json"
            task_path.write_text(json.dumps({
                "lead_id": lead_id,
                "lead_name": lead.get("name"),
                "lead_domain": domain_of(website),
                "website": website,
                "page_excerpt": merged_text,
                "task": "extract_owner_email",
                "schema": {
                    "name": "string (verbatim from page)",
                    "title": "founder|owner|ceo|managing director|director|principal|partner|president|gm",
                    "email": "string ending in lead_domain, or empty if none can be inferred",
                },
                "rules": [
                    "Names must appear verbatim in page_excerpt.",
                    "Email must end with lead_domain. If only the name is found, guess firstname@domain, firstname.lastname@domain, or f.lastname@domain in that order.",
                    "If multiple people are mentioned, pick the most senior.",
                    "Skip board members and advisors.",
                ],
            }, ensure_ascii=False, indent=2))

    if best:
        lead["email"] = best
        lead["email_class"] = klass
        lead.setdefault("email_source", "website_enrichment")
        if klass in {"person", "personal"} and not lead.get("dm_email"):
            lead["dm_email"] = best
        raw = lead.get("raw") or {}
        raw_emails = list(raw.get("all_emails") or [])
        for e in all_emails:
            if e not in raw_emails:
                raw_emails.append(e)
        raw["all_emails"] = raw_emails
        lead["raw"] = raw
    return lead


def needs_enrichment(lead: dict) -> bool:
    """Lead only goes through the crawler if it has no usable email already."""
    candidates = []
    for k in ("dm_email", "email"):
        v = lead.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    raw = lead.get("raw") or {}
    for e in (raw.get("all_emails") or []):
        if isinstance(e, str) and e.strip():
            candidates.append(e.strip())
    domain = lead.get("url") or lead.get("website") or ""
    best, klass = pick_best(candidates, domain)
    return not (best and klass in {"person", "personal"})


async def main_async() -> None:
    p = argparse.ArgumentParser(description="Website email enrichment for leads with no person-class email.")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--report", default=None)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--per-lead-page-cap", type=int, default=3,
                   help="Max non-home pages to try per lead before giving up.")
    p.add_argument("--timeout", type=int, default=12,
                   help="Per-page fetch timeout in seconds.")
    p.add_argument("--per-lead-timeout", type=int, default=30,
                   help="Hard wall-clock cap per lead. Stuck workers are abandoned.")
    p.add_argument("--global-timeout", type=int, default=900,
                   help="Total wall-clock cap (s). Writes partial result if hit.")
    p.add_argument("--limit", type=int, default=0,
                   help="Only enrich this many leads (0 = no cap). Useful for smoke tests.")
    p.add_argument("--claude-review-queue", default="",
                   help="Directory to write claude-review task files into when "
                        "regex extraction fails but the page shows an owner. "
                        "The Claude session processes these files in-context — "
                        "no network calls are made from this script.")
    args = p.parse_args()

    queue_dir = Path(args.claude_review_queue) if args.claude_review_queue else None

    input_path = Path(args.input)
    output_path = Path(args.output)
    leads: list[dict] = []
    for line in input_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            leads.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    work_indexes = [i for i, l in enumerate(leads) if needs_enrichment(l)]
    if args.limit > 0:
        work_indexes = work_indexes[: args.limit]
    already_ok = len(leads) - len(work_indexes)

    start = time.time()
    if not work_indexes:
        output_path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in leads))
        print(json.dumps({"input": len(leads), "needing_enrichment": 0, "already_ok": already_ok,
                          "enriched": 0, "elapsed_s": 0.0, "output": str(output_path)}, indent=2))
        return

    sem = asyncio.Semaphore(args.concurrency)
    enriched_count = 0

    completed = 0
    timed_out_leads = 0
    progress_every = max(20, len(work_indexes) // 20)

    async with AsyncWebCrawler(headless=True, verbose=False) as crawler:
        async def worker(idx: int) -> None:
            nonlocal enriched_count, completed, timed_out_leads
            async with sem:
                before = (leads[idx].get("email") or "").lower()
                try:
                    # Hard per-lead wall-clock cap. Without this, one stuck
                    # Chromium page can block the whole batch for hours.
                    leads[idx] = await asyncio.wait_for(
                        enrich_one(crawler, leads[idx],
                                   args.per_lead_page_cap, args.timeout,
                                   queue_for_claude=queue_dir is not None,
                                   queue_dir=queue_dir),
                        timeout=args.per_lead_timeout,
                    )
                except (asyncio.TimeoutError, Exception):
                    timed_out_leads += 1
                after = (leads[idx].get("email") or "").lower()
                if after and after != before:
                    enriched_count += 1
                completed += 1
                if completed % progress_every == 0:
                    print(f"  progress: {completed}/{len(work_indexes)} "
                          f"enriched={enriched_count} timed_out={timed_out_leads} "
                          f"elapsed={round(time.time()-start,1)}s",
                          file=sys.stderr, flush=True)

        try:
            # Hard global cap. If reached, asyncio cancels pending workers
            # and we write whatever was enriched so far.
            await asyncio.wait_for(
                asyncio.gather(*(worker(i) for i in work_indexes), return_exceptions=True),
                timeout=args.global_timeout,
            )
            global_timeout_hit = False
        except asyncio.TimeoutError:
            global_timeout_hit = True
            print(f"  GLOBAL TIMEOUT after {args.global_timeout}s — "
                  f"{completed}/{len(work_indexes)} done. Writing partial result.",
                  file=sys.stderr, flush=True)

    elapsed = round(time.time() - start, 2)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in leads))

    report = {
        "input": len(leads),
        "needing_enrichment": len(work_indexes),
        "already_ok": already_ok,
        "enriched": enriched_count,
        "still_missing": len(work_indexes) - enriched_count,
        "timed_out_leads": timed_out_leads,
        "global_timeout_hit": global_timeout_hit,
        "elapsed_s": elapsed,
        "rate_per_s": round(len(work_indexes) / max(elapsed, 0.01), 2),
        "output": str(output_path),
    }
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main_async())
