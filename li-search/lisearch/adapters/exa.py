"""Exa — neural people index. The recommended primary.

WHY PRIMARY
Exa's open benchmark (1,400 queries, github.com/exa-labs/benchmarks) reports the
right person at rank 1 on ~72-75% of queries against 20.8% (Parallel) and 40.5%
(Tavily). Vendor-authored, so treat the exact numbers with suspicion — but the
harness is open, and the direction matches the brief's core claim: the win comes
from a specialised index, not from touching LinkedIn.

It is an authorised index, so nothing here puts the operator's own LinkedIn
account at risk. That is the property that made it primary, not the score.

QUERY MATRIX, NOT ONE QUERY
/search has no cursor. Recall comes from breadth instead: every (title x geo)
pair becomes its own query. An audience with 6 titles and 4 cities issues 24
narrow queries rather than 1 vague one, which is also how you stop a neural
index from returning the same capital city's biggest company on every page.
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List

from .. import cache
from ..audience import Audience
from ..http import post_json, HttpError
from ..lead import canonical_account, country_from_url, make_lead
from .base import Adapter, split_headline

ENDPOINT = "https://api.exa.ai/search"


class ExaAdapter(Adapter):
    name = "exa"
    kind = "index"
    env_key = "EXA_API_KEY"
    cost_note = "usage-based; free tier available. Authorised index — no account risk."
    doc = "https://docs.exa.ai"

    def _headers(self) -> Dict[str, str]:
        return {"x-api-key": self.key, "Content-Type": "application/json"}

    def queries(self, a: Audience, cap: int) -> List[str]:
        """Natural-language matrix: '<title> at a <subject> in <geo>'. One
        subject per query — the industry first, then each keyword — so the
        neural index gets 'Owner at a real estate brokerage in Dubai', not the
        whole keyword list glued into one sentence."""
        titles = a.titles()[: self.config.get("max_titles", 8)]
        geos = a.geo_terms()[: self.config.get("max_geos", 8)]
        subjects = a.subjects() or [""]
        tiers = [
            [(t, subjects[0], g) for t in titles[:6] for g in geos],
            [(t, s, g) for s in subjects[1:] for t in titles[:6] for g in geos],
            [(t, subjects[0], g) for t in titles[6:] for g in geos],
        ]
        out: List[str] = []
        for tier in tiers:
            for title, what, geo in tier:
                bits = [title]
                if what:
                    bits.append(what if what.lower().startswith(("a ", "an ")) else "at a %s" % what)
                bits.append("in %s" % geo)
                q = " ".join(bits)
                if q not in out:
                    out.append(q)
        return out[:cap]

    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        per_query = int(self.config.get("results_per_query", 25))
        max_queries = max(1, -(-limit // max(1, per_query // 3)))   # over-issue; dedupe collapses
        leads: List[Dict[str, Any]] = []
        seen = set()

        for q in self.queries(audience, max_queries):
            payload = {
                "query": q,
                "category": "linkedin profile",
                "type": "auto",
                "numResults": per_query,
            }
            cached = cache.get(self.name, payload, ttl)
            if cached is None:
                try:
                    cached = post_json(ENDPOINT, payload, headers=self.headers())
                except HttpError as e:
                    if e.status in (401, 403):
                        raise SystemExit("exa: key rejected (HTTP %s). Check EXA_API_KEY." % e.status)
                    if e.status == 402:
                        print("    exa: out of credits, stopping this provider.")
                        break
                    print("    exa: query failed (%s), continuing." % e.status)
                    continue
                cache.put(self.name, payload, cached)
                age = 0
            else:
                age = cache.age_days(self.name, payload) or 0

            for r in (cached.get("results") or []):
                acct = canonical_account(r.get("url", ""))
                if not acct or acct in seen:
                    continue
                seen.add(acct)
                parsed = split_headline(r.get("title", ""), r.get("author", "") or "")
                leads.append(make_lead(
                    source=self.name,
                    account=acct,
                    full_name=parsed["full_name"],
                    title=parsed["title"],
                    company=parsed["company"],
                    country=country_from_url(r.get("url", "")),
                    summary=(r.get("text") or "")[:600],
                    freshness_days=age,
                    raw_query=q,
                    extra={"score": r.get("score"), "published": r.get("publishedDate")},
                ))
            if len(leads) >= limit:
                break
        return leads[:limit]
