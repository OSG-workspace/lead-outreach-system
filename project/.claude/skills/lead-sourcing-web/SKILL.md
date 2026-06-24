---
name: lead-sourcing-web
description: Sources leads from the open web using ddgs (free DuckDuckGo Python library, no key) for discovery and crawl4ai (free Apache 2.0, LLM-friendly markdown) for fetching pages. Use when targets live on directories, niche sites, e-commerce platforms, or anywhere not covered by Google Maps. Also used as enrichment by business-gap-analysis to fetch lead websites.
---

# Web Sourcing (Free Stack)

Two free tools, both installed by setup-tools:

- **`ddgs`** (Python, MIT) — search DuckDuckGo with no API key, no auth, no real rate limits if you're polite
- **`crawl4ai`** (Python, Apache 2.0) — fetch any URL and get clean LLM-friendly markdown back, handles JavaScript pages, the most-starred crawler on GitHub

These two cover everything Apify's `rag-web-browser` did, for free.

## Two modes

### Mode A — Discovery (find new leads from search)

Use `ddgs` to find candidate URLs. Build queries from the ICP:

```python
from ddgs import DDGS

def search(query, max_results=20):
    with DDGS() as ddgs:
        return list(ddgs.text(
            query,
            region="wt-wt",
            safesearch="off",
            max_results=max_results
        ))

# Triangulate with multiple queries
queries = [
    "list of independent dental clinics Beirut",
    "best dentists Beirut 2024 site:*.lb",
    "dental clinic Beirut OR Hamra OR Verdun -site:facebook.com",
]
all_results = []
for q in queries:
    all_results.extend(search(q, max_results=20))

# Dedupe by URL
seen = set()
unique = []
for r in all_results:
    if r["href"] not in seen:
        seen.add(r["href"])
        unique.append(r)
```

DDG search results give you `title`, `href`, and `body` (snippet). For each result, decide if it's:

- A **directory aggregator** (Yelp, Yellow Pages, etc.) → fetch and extract individual listings, each becomes a candidate
- A **specific business website** → fetch and extract contact info, becomes one candidate
- **Noise** (forum, news article, irrelevant) → skip, OR follow links from it if it mentions specific businesses

### Mode B — Enrichment (extract emails/info from a known URL)

Used both standalone (when user provides a list of company URLs) AND by `business-gap-analysis` to research each qualified lead's website.

**For gap-analysis enrichment, do NOT use the inline crawl4ai code below.** Use [`tools/scripts/extract_signals.py`](../../../tools/scripts/extract_signals.py) instead — it returns ~1KB of structured signals per page rather than ~10KB of full markdown. The inline code below is only for cases where you genuinely need the full page (rare).

```python
import asyncio
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

async def fetch_page(url):
    config = CrawlerRunConfig(
        word_count_threshold=10,
        excluded_tags=["nav", "footer", "header", "script", "style"],
        only_text=False,  # keep links/images for analysis
    )
    async with AsyncWebCrawler(headless=True) as crawler:
        result = await crawler.arun(url=url, config=config)
        if result.success:
            return {
                "url": result.url,
                "markdown": result.markdown,
                "html": result.cleaned_html,
                "metadata": result.metadata,
                "links": result.links,
                "media": result.media,
            }
        return None

# Fetch homepage + standard contact pages
async def fetch_business_pages(homepage_url):
    pages = {}
    pages["home"] = await fetch_page(homepage_url)

    # Try standard sub-pages for contact info
    base = homepage_url.rstrip("/")
    for path in ["/contact", "/about", "/team", "/legal", "/privacy"]:
        try:
            page = await fetch_page(base + path)
            if page:
                pages[path.lstrip("/")] = page
        except Exception:
            continue
    return pages
```

## Email extraction

```python
import re

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+\.\w+|[\w.+-]+@[\w-]+\.\w{2,}')

GENERIC_PREFIXES = {"noreply", "no-reply", "donotreply", "do-not-reply",
                    "mailer-daemon", "postmaster", "webmaster"}

LOW_PRIORITY_PREFIXES = {"info", "admin", "support", "help", "office",
                          "contact", "hello", "hi", "team"}

def extract_emails(text, current_domain=None):
    """Extract emails, ranked by quality."""
    raw = set(EMAIL_RE.findall(text))
    valid = []
    for email in raw:
        local = email.split("@")[0].lower()
        domain = email.split("@")[1].lower()
        if local in GENERIC_PREFIXES:
            continue
        # Prefer same-domain emails over random ones in HTML
        if current_domain and domain != current_domain.lower():
            continue
        valid.append(email)

    # Rank: named emails > function emails > generic
    def rank(email):
        local = email.split("@")[0].lower()
        if local in LOW_PRIORITY_PREFIXES:
            return 2
        if "." in local or "_" in local or "-" in local:
            return 0  # likely first.last@ — best
        if len(local) > 4:
            return 1  # likely a name like sarah@
        return 2
    return sorted(valid, key=rank)
```

## Mapping to lead schema

For business URLs (Mode A or user-provided):

```json
{
  "id": "web-<domain-slug>",
  "name": "<extracted business name>",
  "source": "web",
  "url": "<canonical homepage>",
  "email": "<best ranked email or null>",
  "phone": "<phone if found>",
  "location": "<address if found>",
  "raw": {
    "domain": "<root domain>",
    "discovered_via": "<query that surfaced this>",
    "page_title": "<title>",
    "meta_description": "<meta>",
    "tech_stack_signals": [...],
    "ssl": <bool from URL https?>,
    "all_emails": [...],
    "social_links": [...]
  }
}
```

## Tech stack signals (useful for gap analysis later)

While fetching, note signals from `result.html`:

- `wp-content/`, `wp-includes/` → WordPress
- `cdn.shopify.com`, `myshopify.com` → Shopify
- `wixstatic.com`, `wix.com` → Wix
- `squarespace` → Squarespace
- `<meta name="generator" content="...">` → tells you the CMS directly
- `<meta name="viewport">` → mobile-friendly?
- Last copyright year in footer (regex `©\s*(20\d\d)`) → "stale" if >2 years old
- Has `<script src="...gtag">` or `<script src="...fbevents">` → has tracking pixels (sophisticated)
- Has `calendly`, `acuityscheduling`, `square.site/book` etc. → has booking flow

Pass these to gap-analysis as `raw.tech_stack_signals`.

## Quality filters

Drop URLs where:

- Page returned 404/403/5xx (`result.success == False`)
- URL clearly resolves to a social profile (`instagram.com`, `linkedin.com`, `facebook.com`) — these channels are not part of this stack; if a target's only presence is there, mark it unsourceable and move on
- Page is a forum thread, news article, or aggregator referring TO businesses — but DO follow links from those pages to find the actual business URLs
- Page has < 200 words of substantive content (likely a parking page or under-construction)

## Politeness rules

- 1-2 second delay between fetches to the same domain
- Max 5 concurrent crawl4ai sessions (bigger numbers eat memory)
- Set `User-Agent` to a real browser string (crawl4ai does this by default)
- Respect `robots.txt` if a site explicitly disallows scraping — crawl4ai supports this via `respect_robots_txt=True`

## Failure modes

- **DDG returns 0 results**: query too narrow OR DDG temporarily blocked your IP. Try simpler query first; if persistent, switch to a VPN or try Bing.
- **All results are aggregators (Yelp, Yellow Pages)**: that's actually fine. Fetch the aggregator pages and extract the individual listings.
- **Page fetch fails for many URLs**: site uses heavy bot protection (Cloudflare). Try `crawl4ai` with `magic=True` for stealth mode. If still failing, skip those URLs.
- **Page renders as JavaScript-only with no useful markdown**: crawl4ai handles JS by default. If still empty, the site uses extreme client-side rendering. Skip.

## Cost

**$0.** No keys. Limits are practical (your IP and your patience), not contractual.
