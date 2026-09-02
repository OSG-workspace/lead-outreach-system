"""People Data Labs — licensed/co-op batch database. Enrichment fallback.

WHY FALLBACK AND NOT PRIMARY
PDL's own coverage skew is the reason: ~260M of its records are US, and work
email fill is strongest for US/UK/CA decision-makers. For a MENA-first operator
that is a fallback, not a foundation. Where it earns its place is structured
filtering (exact titles, headcount bands, country codes) and a real cursor, so
it returns complete rows where an index returns a headline.

Search API takes Elasticsearch. Records are monthly-batch, so freshness is
reported as 'unknown' rather than pretending to be live.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .. import cache
from ..audience import Audience, COUNTRY_NAMES
from ..http import get_json, qs, HttpError
from ..lead import canonical_account, make_lead
from .base import Adapter

ENDPOINT = "https://api.peopledatalabs.com/v5/person/search"


class PDLAdapter(Adapter):
    name = "pdl"
    kind = "licensed"
    env_key = "PDL_API_KEY"
    cost_note = "free 100/mo; ~$300-600/mo teams. Monthly batch refresh."
    doc = "https://docs.peopledatalabs.com/docs/person-search-api"

    def _headers(self) -> Dict[str, str]:
        return {"X-Api-Key": self.key, "Accept": "application/json"}

    def es_query(self, a: Audience) -> Dict[str, Any]:
        must: List[Dict[str, Any]] = [{"exists": {"field": "linkedin_username"}}]
        titles = a.titles()
        if titles:
            must.append({"bool": {"should": [
                {"match_phrase": {"job_title": t}} for t in titles[:15]
            ], "minimum_should_match": 1}})
        countries = [COUNTRY_NAMES.get(c, c).lower() for c in (a.get("countries") or [])]
        if countries:
            must.append({"terms": {"location_country": countries}})
        if a.get("cities"):
            must.append({"bool": {"should": [
                {"match_phrase": {"location_locality": c.lower()}} for c in a["cities"][:20]
            ], "minimum_should_match": 1}})
        kw = list(a.get("keywords") or []) + ([a["industry"]] if a.get("industry") else [])
        if kw:
            must.append({"bool": {"should": [
                {"match": {"job_company_name": k}} for k in kw[:10]
            ] + [{"match": {"industry": k}} for k in kw[:10]], "minimum_should_match": 1}})
        hc = a.get("headcount") or []
        if len(hc) == 2:
            must.append({"range": {"job_company_size": {"gte": hc[0], "lte": hc[1]}}})

        must_not = []
        for t in (a.get("exclude_titles") or []):
            must_not.append({"match_phrase": {"job_title": t}})
        for k in (a.get("exclude_keywords") or []):
            must_not.append({"match": {"job_company_name": k}})

        return {"bool": {"must": must, "must_not": must_not}}

    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        query = self.es_query(audience)
        leads: List[Dict[str, Any]] = []
        scroll = None
        page_size = min(100, max(1, limit))

        while len(leads) < limit:
            payload = {"query": query, "size": page_size, "scroll_token": scroll,
                       "titlecase": True, "pretty": False}
            cached = cache.get(self.name, payload, ttl)
            if cached is None:
                url = ENDPOINT + "?" + qs({
                    "query": __import__("json").dumps(query),
                    "size": page_size,
                    "scroll_token": scroll,
                    "titlecase": "true",
                })
                try:
                    cached = get_json(url, headers=self.headers())
                except HttpError as e:
                    if e.status in (401, 403):
                        raise SystemExit("pdl: key rejected (HTTP %s). Check PDL_API_KEY." % e.status)
                    if e.status == 402:
                        print("    pdl: quota exhausted, stopping this provider.")
                        break
                    if e.status == 404:
                        break                      # no more matches
                    print("    pdl: page failed (%s), stopping this provider." % e.status)
                    break
                cache.put(self.name, payload, cached)

            rows = cached.get("data") or []
            if not rows:
                break
            for r in rows:
                acct = canonical_account(r.get("linkedin_username") or r.get("linkedin_url") or "")
                if not acct:
                    continue
                leads.append(make_lead(
                    source=self.name,
                    account=acct,
                    full_name=r.get("full_name") or "",
                    title=r.get("job_title") or "",
                    company=r.get("job_company_name") or "",
                    location=r.get("location_name") or "",
                    country=(r.get("location_country_code") or _cc(r.get("location_country"))),
                    email=(r.get("work_email") or (r.get("emails") or [{}])[0].get("address", "")) or "",
                    summary=r.get("headline") or "",
                    freshness_days=None,           # monthly batch: honestly unknown
                    raw_query="pdl es_dsl",
                    extra={"company_size": r.get("job_company_size"),
                           "seniority": r.get("job_title_levels")},
                ))
            scroll = cached.get("scroll_token")
            if not scroll:
                break
        return leads[:limit]


def _cc(country_name):
    if not country_name:
        return ""
    inv = {v.lower(): k for k, v in COUNTRY_NAMES.items()}
    return inv.get(str(country_name).lower(), "")
