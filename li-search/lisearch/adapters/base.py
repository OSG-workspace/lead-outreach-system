"""The one interface every provider implements.

WHY AN ADAPTER LAYER IS THE WHOLE POINT
Proxycurl went from ~$10M ARR to shut down in six months. In this category a
vendor disappearing is the base rate, not the tail. So no provider name appears
anywhere outside its own adapter file: `fire` talks to this interface, and
replacing a dead vendor is a new file plus a config line, never a rewrite.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterable, List, Optional

from ..audience import Audience, COUNTRY_NAMES
from ..compliance import assert_no_session_auth


class Adapter(object):
    name = "base"
    kind = "unknown"          # licensed | index | scraper | fallback
    env_key = None            # env var holding the API key, or None if keyless
    cost_note = ""
    doc = ""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.key = os.environ.get(self.env_key or "", "") or self.config.get("api_key", "")
        # fire() sets this to a status-file writer so a long fetch is visible
        # from outside ("ddgs 340/1400 queries"); harmless when unset.
        self.progress = None

    def report(self, msg: str) -> None:
        if self.progress:
            try:
                self.progress(msg)
            except Exception:
                pass

    # -- availability ---------------------------------------------------
    def available(self) -> bool:
        """A provider with no key is skipped, never fatal. That is what lets the
        whole tool run on free tiers, or on nothing at all."""
        return True if not self.env_key else bool(self.key)

    def why_unavailable(self) -> str:
        return "no API key (set %s in the environment or li-search/config.json)" % self.env_key

    # -- the contract ---------------------------------------------------
    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------
    def headers(self) -> Dict[str, str]:
        h = self._headers()
        assert_no_session_auth(self.name, h)
        return h

    def _headers(self) -> Dict[str, str]:
        return {}


# Words that make a headline segment a ROLE rather than a company name.
ROLE_WORDS = re.compile(
    r"(?<![a-z])(owner|founder|ceo|coo|cfo|cto|cmo|director|manager|head|chief|president|"
    r"partner|officer|executive|consultant|agent|advisor|adviser|broker|specialist|realtor|"
    r"strategist|associate|vp|chairman|chairwoman|proprietor|principal|investor|entrepreneur|"
    r"lead|engineer|analyst|coordinator|assistant|secretary|intern|expert|professional|"
    r"leader|supervisor|representative|sales|marketing|recruiter|developer|architect|"
    r"accountant|lawyer|attorney|doctor|dr\.?|md|gm|ceo/founder)(?![a-z])", re.I)


def looks_like_role(segment: str) -> bool:
    return bool(ROLE_WORDS.search(segment or ""))


def split_headline(headline: str, full_name: str = "") -> Dict[str, str]:
    """Turn an index title like
       'Sami Haddad - Managing Director - Cedar Hotels | LinkedIn'
    into name/title/company. Index providers return exactly this shape, and it
    is the difference between a usable row and a bare URL.

    LinkedIn emits THREE shapes: 'Name - Title - Company', 'Name - Title at
    Company', and — for anyone whose headline is just where they work — 'Name -
    Company'. The first version filed that last shape's company under title,
    which put "Rocky Real Estate Brokerage LLC" in the TITLE column of 142 of
    167 rows on the first real fire. A lone segment with no role word is a
    company, and the title is honestly left empty.
    """
    s = (headline or "").strip()
    s = s.split("|")[0].strip()                      # drop the trailing 'LinkedIn'
    # An engine truncates long titles with an ellipsis, and one parser then
    # glues the NEXT result's title onto it ("...Mohammed Ilyas"). Nothing
    # after the ellipsis belongs to this person.
    s = re.split(r"\.\.\.|…", s)[0].strip()
    s = s.replace(" – ", " - ").replace(" — ", " - ")
    parts = [p.strip() for p in s.split(" - ") if p.strip()]
    name, title, company = full_name, "", ""
    if parts:
        if not name:
            name = parts[0]
        rest = parts[1:] if (not full_name or parts[0].lower() == full_name.lower()) else parts
        if len(rest) >= 2:
            title, company = rest[0], rest[-1]
        elif len(rest) == 1:
            blob = rest[0]
            # "Owner at Cedar Hotels" is the other common shape.
            if " at " in blob:
                title, company = [x.strip() for x in blob.split(" at ", 1)]
            elif " @ " in blob:
                title, company = [x.strip() for x in blob.split(" @ ", 1)]
            elif looks_like_role(blob):
                title = blob
            else:
                company = blob
    return {"full_name": name, "title": title, "company": company}


_LOCATION_RE = re.compile(r"location:\s*([^·•|\n]{3,80})", re.I)


def location_from_snippet(snippet: str) -> str:
    """Some engines render LinkedIn's 'Location: Dubai, United Arab Emirates'
    line into the snippet. Cheap to lift, and it is the city evidence the
    qualification gate wants."""
    m = _LOCATION_RE.search(snippet or "")
    return m.group(1).strip(" .") if m else ""


SEED_TITLE_OR = '(Founder OR Owner OR CEO OR "Managing Director" OR "Managing Partner" OR "General Manager")'


def web_queries(a: Audience, cap: int, max_titles: int = 8) -> List[str]:
    """The query matrix every web-index adapter (openweb, ddgs, ...) fires.

    Shape:  site:ae.linkedin.com/in "Owner" "real estate" Dubai

    - the country subdomain does the geo filtering an index can actually honour,
      so a "Dubai" query stops returning Karachi profiles that mention Dubai;
    - ONE subject per query — industry first, then each keyword as its own
      variant — never the whole keyword list glued into one string;
    - tiers, so a small cap still covers every city x every top title before it
      spends queries on synonyms:  tier 0 = one query per seeded PERSON and per
      seeded COMPANY (the brief named them; nothing is more precise), tier 1 =
      top-6 titles x geos x industry, tier 2 = same x each keyword, tier 3 =
      remaining titles x geos x industry. Geos are the cities plus the country
      name, because a Lebanese profile says "Beirut" or just "Lebanon".
    """
    titles = a.titles()[:max_titles]
    subjects = a.subjects() or [""]
    cities = list(a.get("cities") or [])
    top, rest = titles[:6], titles[6:]
    t0: List[str] = []
    t1: List[tuple] = []
    t2: List[tuple] = []
    t3: List[tuple] = []
    sites = a.site_filters()
    country_names = [COUNTRY_NAMES.get(cc, cc) for _, cc in sites if cc]
    # People: the person may be listed on ANY regional subdomain (a founder
    # who moved to Dubai keeps the firm), so no site prefix — just the name
    # and the country as a disambiguator.
    for name, company in a.people_pairs():
        if company:
            t0.append('site:linkedin.com/in "%s" "%s"' % (name, company))
        t0.append('site:linkedin.com/in "%s" %s' % (name, " ".join(country_names[:1])))
    for site, cc in sites:
        for company in a.get("companies") or []:
            # Strict (title OR-group) first, then loose: the OR syntax is not
            # honoured by every engine, and the loose form still surfaces the
            # founder when the page title carries the role.
            t0.append('site:%s "%s" %s' % (site, company, SEED_TITLE_OR))
            t0.append('site:%s "%s"' % (site, company))
        geos = cities + [COUNTRY_NAMES.get(cc, cc)] if cc else (cities or [""])
        t1 += [(site, t, subjects[0], g) for t in top for g in geos]
        t2 += [(site, t, s, g) for s in subjects[1:] for t in top for g in geos]
        t3 += [(site, t, subjects[0], g) for t in rest for g in geos]
    out: List[str] = []
    seen = set()
    for q in t0:
        q = q.strip()
        if q not in seen:
            seen.add(q)
            out.append(q)
    for site, t, s, g in t1 + t2 + t3:
        q = 'site:%s "%s"' % (site, t)
        if s:
            # Quote short subjects (they are phrases); leave long ones loose so
            # "real estate brokerage" still matches "real estate broker".
            q += (' "%s"' % s) if len(s.split()) <= 2 else (" " + s)
        if g:
            q += " " + g
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:cap]
