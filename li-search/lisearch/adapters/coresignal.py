"""Coresignal — aggregated employee database. Alternative primary.

WHEN TO MAKE THIS PRIMARY INSTEAD OF EXA
When you need bulk structured rows with firmographics rather than best-match
discovery. Its per-record economics fall toward ~$0.005 at volume, which is the
cheapest structured route in the brief's table.

THE TWO-CALL SHAPE IS THE COST MODEL
search/es_dsl returns IDs only; each collect/<id> spends a credit. So the ID
page is cached aggressively and every collect is cached by ID — re-firing the
same audience next week re-reads collected people for free and only spends on
the genuinely new ones.

Endpoints are config-overridable on purpose: this vendor has moved its API
version more than once, and a path change should be a config edit, not a patch.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from .. import cache
from ..audience import Audience, COUNTRY_NAMES
from ..http import post_json, get_json, HttpError
from ..lead import canonical_account, make_lead
from .base import Adapter

SEARCH = "https://api.coresignal.com/cdapi/v2/employee_multi_source/search/es_dsl"
COLLECT = "https://api.coresignal.com/cdapi/v2/employee_multi_source/collect/%s"


class CoresignalAdapter(Adapter):
    name = "coresignal"
    kind = "licensed"
    env_key = "CORESIGNAL_API_KEY"
    cost_note = "$49 starter -> ~$1,500 premium; ~$0.196 down to ~$0.005/record."
    doc = "https://docs.coresignal.com"

    def _headers(self) -> Dict[str, str]:
        return {"apikey": self.key, "Content-Type": "application/json"}

    def _search_url(self) -> str:
        return self.config.get("search_endpoint", SEARCH)

    def _collect_url(self, cid: Any) -> str:
        return self.config.get("collect_endpoint", COLLECT) % cid

    def es_query(self, a: Audience) -> Dict[str, Any]:
        must: List[Dict[str, Any]] = []
        titles = a.titles()
        if titles:
            must.append({"bool": {"should": [
                {"match_phrase": {"active_experience_title": t}} for t in titles[:15]
            ], "minimum_should_match": 1}})
        countries = [COUNTRY_NAMES.get(c, c) for c in (a.get("countries") or [])]
        if countries:
            must.append({"bool": {"should": [
                {"match_phrase": {"location_country": c}} for c in countries
            ], "minimum_should_match": 1}})
        if a.get("cities"):
            must.append({"bool": {"should": [
                {"match_phrase": {"location_full": c}} for c in a["cities"][:20]
            ], "minimum_should_match": 1}})
        kw = list(a.get("keywords") or []) + ([a["industry"]] if a.get("industry") else [])
        if kw:
            must.append({"bool": {"should": [
                {"match": {"active_experience_company_name": k}} for k in kw[:10]
            ] + [{"match": {"active_experience_company_industry": k}} for k in kw[:10]],
                "minimum_should_match": 1}})

        must_not = [{"match_phrase": {"active_experience_title": t}}
                    for t in (a.get("exclude_titles") or [])]
        return {"query": {"bool": {"must": must, "must_not": must_not}}}

    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        body = self.es_query(audience)
        ids = cache.get(self.name + ":ids", body, ttl)
        if ids is None:
            try:
                ids = post_json(self._search_url(), body, headers=self.headers())
            except HttpError as e:
                if e.status in (401, 403):
                    raise SystemExit("coresignal: key rejected (HTTP %s)." % e.status)
                print("    coresignal: search failed (%s), skipping provider." % e.status)
                return []
            cache.put(self.name + ":ids", body, ids)
        if isinstance(ids, dict):
            ids = ids.get("ids") or ids.get("data") or []

        budget = int(self.config.get("max_collects", limit))
        leads: List[Dict[str, Any]] = []
        spent = 0
        for cid in ids[: max(0, budget)]:
            rec = cache.get(self.name + ":rec", cid, ttl or 30 * 86400)
            if rec is None:
                if spent >= budget:
                    break
                try:
                    rec = get_json(self._collect_url(cid), headers=self.headers())
                except HttpError as e:
                    if e.status == 402:
                        print("    coresignal: credits exhausted after %d collects." % spent)
                        break
                    continue
                cache.put(self.name + ":rec", cid, rec)
                spent += 1

            acct = canonical_account(
                rec.get("linkedin_shorthand_name") or rec.get("professional_network_url")
                or rec.get("linkedin_url") or ""
            )
            if not acct:
                continue
            exp = (rec.get("active_experience") or [{}])
            exp = exp[0] if isinstance(exp, list) and exp else {}
            leads.append(make_lead(
                source=self.name,
                account=acct,
                full_name=rec.get("full_name") or "",
                title=rec.get("active_experience_title") or exp.get("position_title") or "",
                company=rec.get("active_experience_company_name") or exp.get("company_name") or "",
                location=rec.get("location_full") or "",
                country=_cc(rec.get("location_country")),
                email=(rec.get("primary_professional_email") or ""),
                summary=rec.get("headline") or rec.get("summary") or "",
                # Vendor states 695M+ records refreshed monthly; many rows are
                # 3-4 months old at access. Reporting that honestly, not as live.
                freshness_days=int(self.config.get("assumed_freshness_days", 100)),
                raw_query="coresignal es_dsl",
                extra={"id": cid},
            ))
            if len(leads) >= limit:
                break
        if spent:
            print("    coresignal: %d collect credit(s) spent, %d served from cache."
                  % (spent, len(leads) - spent))
        return leads[:limit]


def _cc(name):
    if not name:
        return ""
    inv = {v.lower(): k for k, v in COUNTRY_NAMES.items()}
    return inv.get(str(name).lower(), "")
