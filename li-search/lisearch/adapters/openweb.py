"""Keyless stdlib fallback: one engine's public HTML results page.

WHY IT EXISTS AT ALL
So the tool does something with zero API keys, zero spend and zero installs,
which is this repo's standing constraint. It needs nothing but python3.

WHY IT IS LAST
It talks to ONE engine (DuckDuckGo's HTML endpoint) and that engine throttles
freely: it answers a client it dislikes with HTTP 202 and an interstitial, and
keeps doing so for that IP for a while. The `ddgs` provider rotates engines and
is the better free source; this one is the belt to its braces. Expect thin rows
— a slug and a headline, sometimes a company, never an email.

THE 2026-09-01 LESSON
The engine's throttle is keyed on the User-Agent first. Four fires in a row
produced zero leads because this adapter announced itself as "li-search/1.0" and
was handed the interstitial on the very first query. It now presents as a
browser, which is what the engine serves that page to, and the interstitial is
detected and reported rather than read as "nobody in this market".

It does not contact linkedin.com (compliance.assert_allowed_host would refuse);
it reads only what the engine already publishes.
"""
from __future__ import annotations

import html
import re
import time
import urllib.parse
from typing import Any, Dict, List

from .. import cache
from ..audience import Audience
from ..http import request, qs, HttpError
from ..lead import canonical_account, country_from_url, make_lead
from .base import Adapter, location_from_snippet, split_headline, web_queries

ENDPOINT = "https://html.duckduckgo.com/html/"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>', re.S | re.I)
SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(?P<s>.*?)</a>', re.S | re.I)
# The engine answers a rate-limited client with HTTP 202 and an interstitial
# instead of an error. Left undetected that reads as "this market has nobody in
# it", which is the worst possible failure for a sourcing tool.
BLOCK_MARKERS = ("anomaly", "challenge-form", "detected unusual", "captcha")


class RateLimited(Exception):
    pass


def looks_blocked(body: str) -> bool:
    low = (body or "").lower()
    if 'class="result__a"' in low:
        return False
    return any(m in low for m in BLOCK_MARKERS)


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps results as /l/?uddg=<encoded>."""
    if "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        return urllib.parse.unquote(q.get("uddg", [""])[0])
    return href


class OpenWebAdapter(Adapter):
    name = "openweb"
    kind = "fallback"
    env_key = None
    cost_note = "free, keyless, stdlib-only. One engine; throttles easily."
    doc = "n/a"

    def _headers(self) -> Dict[str, str]:
        return {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9"}

    def queries(self, a: Audience, cap: int) -> List[str]:
        return web_queries(a, cap, max_titles=int(self.config.get("max_titles", 8)))

    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        leads: List[Dict[str, Any]] = []
        seen = set()
        for q in self.queries(audience, int(self.config.get("max_queries", 24))):
            payload = {"q": q}
            body = cache.get(self.name, payload, ttl)
            if body is not None and (looks_blocked(body) or 'class="result__a"' not in body):
                body = None            # never trust a cached block page or an empty one
            if body is None:
                try:
                    status, body = request(ENDPOINT + "?" + qs({"q": q, "kl": "wt-wt"}),
                                           headers=self.headers())
                except HttpError:
                    continue
                if status == 202 or looks_blocked(body):
                    # Never cache an interstitial — it would poison this query
                    # for the whole TTL.
                    err = RateLimited(
                        "engine rate-limited this IP (HTTP 202 interstitial); not an empty "
                        "market. Wait ~1h or rely on ddgs")
                    err.partial = leads          # fire() keeps what we already got
                    raise err
                cache.put(self.name, payload, body)
                time.sleep(float(self.config.get("delay_seconds", 2.0)))
                age = 0
            else:
                age = cache.age_days(self.name, payload) or 0

            snippets = [_strip(m.group("s")) for m in SNIPPET_RE.finditer(body)]
            for i, m in enumerate(RESULT_RE.finditer(body)):
                href = _unwrap(m.group("href"))
                acct = canonical_account(href)
                if not acct or acct in seen:
                    continue
                seen.add(acct)
                parsed = split_headline(_strip(m.group("title")))
                leads.append(make_lead(
                    source=self.name,
                    account=acct,
                    full_name=parsed["full_name"],
                    title=parsed["title"],
                    company=parsed["company"],
                    location=location_from_snippet(snippets[i] if i < len(snippets) else ""),
                    country=country_from_url(href),
                    summary=snippets[i] if i < len(snippets) else "",
                    freshness_days=age,
                    raw_query=q,
                ))
            if len(leads) >= limit:
                break
        return leads[:limit]
