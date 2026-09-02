"""The stored target audience — this tool's memory.

You describe an audience once ("owners of boutique hotels in Bulgaria and
Romania"); it is normalised, given a slug, and written to audiences/<slug>.json.
From then on `li-search fire <slug>` reproduces that exact search. Nothing about
an audience is inferred at fire time, so a run six weeks later targets the same
population as the first one — which is the only way a lead ledger stays
meaningful.

An audience is deliberately provider-agnostic. Each adapter compiles it into its
own query language (Exa natural-language + category, PDL Elasticsearch,
Coresignal ES DSL), so swapping providers never means rewriting audiences.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
AUDIENCES = ROOT / "audiences"

# ISO-3166 alpha-2 for the countries this operator actually works, plus the
# common spellings. Adapters that want a country NAME get it from here rather
# than each inventing a mapping.
COUNTRY_NAMES = {
    "AE": "United Arab Emirates", "SA": "Saudi Arabia", "QA": "Qatar",
    "KW": "Kuwait", "BH": "Bahrain", "OM": "Oman", "LB": "Lebanon",
    "EG": "Egypt", "JO": "Jordan", "TR": "Turkey", "MA": "Morocco",
    "US": "United States", "GB": "United Kingdom", "CA": "Canada",
    "AU": "Australia", "DE": "Germany", "FR": "France", "ES": "Spain",
    "IT": "Italy", "NL": "Netherlands", "BG": "Bulgaria", "RO": "Romania",
    "GR": "Greece", "PT": "Portugal", "PL": "Poland", "CH": "Switzerland",
    "AT": "Austria", "BE": "Belgium", "SE": "Sweden", "IE": "Ireland",
}

# Owner-equivalent titles. Levant and Gulf SMEs say "General Manager" where the
# West says CEO, so the default set is broader than a US-only list would be.
DEFAULT_TITLES = [
    # Ordered by yield, because every web-index adapter spends its query cap on
    # the FIRST six of these. Owner/Founder/CEO/MD/GM are the forms that show up
    # in a Gulf SME headline; the long-form synonyms come last.
    "Owner", "Founder", "CEO", "Managing Director", "General Manager", "Co-Founder",
    "Managing Partner", "Chairman", "President", "Co-Owner",
    "Chief Executive Officer", "Proprietor", "Partner", "Principal",
]

# LinkedIn serves a member's public profile from a COUNTRY subdomain chosen by
# the member's own location (ae.linkedin.com/in/... for the UAE). In a web
# index that subdomain is a far more reliable geo filter than the word "Dubai"
# appearing in a headline, so every web-engine adapter builds its site:
# operator from this map. US profiles live on the bare www host.
LINKEDIN_SUBDOMAIN = {cc: cc.lower() for cc in COUNTRY_NAMES}
LINKEDIN_SUBDOMAIN["GB"] = "uk"
LINKEDIN_SUBDOMAIN["US"] = ""

SENIORITY_PRESETS = {
    "owner": DEFAULT_TITLES,
    "clevel": ["CEO", "COO", "CFO", "CTO", "CMO", "Chief Executive Officer",
               "Chief Operating Officer", "Founder", "President"],
    "director": ["Director", "Head of", "VP", "Vice President", "Country Director"],
    "manager": ["Manager", "General Manager", "Operations Manager", "Practice Manager"],
}


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:60] or "audience"


class Audience(dict):
    """A stored target-audience definition. Plain dict so it round-trips to JSON."""

    @property
    def slug(self) -> str:
        return self["slug"]

    def titles(self) -> List[str]:
        t = list(self.get("titles") or [])
        for preset in self.get("seniority") or []:
            t.extend(SENIORITY_PRESETS.get(preset, []))
        # Preserve order, drop case-duplicates.
        seen, out = set(), []
        for x in t:
            k = x.lower().strip()
            if k and k not in seen:
                seen.add(k)
                out.append(x.strip())
        return out or DEFAULT_TITLES

    def geo_terms(self) -> List[str]:
        """Cities first — they are far more selective than a country name in a
        neural index, and a country-only query returns that country's capital
        over and over."""
        terms = list(self.get("cities") or [])
        terms += [COUNTRY_NAMES.get(c.upper(), c) for c in (self.get("countries") or [])]
        return terms or ["Worldwide"]

    def site_filters(self) -> List[Tuple[str, str]]:
        """[(site operand, country code)] — one per country, e.g.
        ("ae.linkedin.com/in", "AE"). A country with no subdomain (US) or no
        country at all falls back to the bare host, so the query still restricts
        itself to profile pages."""
        out: List[Tuple[str, str]] = []
        for cc in (self.get("countries") or []):
            cc = cc.upper()
            sub = LINKEDIN_SUBDOMAIN.get(cc)
            out.append(("%s.linkedin.com/in" % sub if sub else "linkedin.com/in", cc))
        return out or [("linkedin.com/in", "")]

    def people_pairs(self) -> List[Tuple[str, str]]:
        """[(name, company)] — a people seed is written 'Name @ Company'. The
        company is what stops 'Hala Jaber' (dozens of them) from qualifying
        every namesake; a bare name still works but qualifies only when the
        row shows a title or industry match beside it."""
        out: List[Tuple[str, str]] = []
        for entry in self.get("people") or []:
            name, _, company = entry.partition("@")
            out.append((name.strip(), company.strip()))
        return out

    def subjects(self) -> List[str]:
        """Industry first, then each keyword — each a SEPARATE query subject.
        The first version glued industry and every keyword into one string, and
        a web engine reads 'real estate brokerage real estate, brokerage,
        property, realty' as noise, not as a filter."""
        seen, out = set(), []
        for x in [self.get("industry") or ""] + list(self.get("keywords") or []):
            k = x.strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(x.strip())
        return out

    def describe(self) -> str:
        parts = []
        if self.get("industry"):
            parts.append(self["industry"])
        if self.get("keywords"):
            parts.append(", ".join(self["keywords"]))
        return " ".join(parts) or self.get("description", "")


def new_audience(
    name: str,
    industry: str = "",
    titles: Optional[List[str]] = None,
    seniority: Optional[List[str]] = None,
    countries: Optional[List[str]] = None,
    cities: Optional[List[str]] = None,
    keywords: Optional[List[str]] = None,
    exclude_titles: Optional[List[str]] = None,
    exclude_keywords: Optional[List[str]] = None,
    headcount: Optional[List[int]] = None,
    description: str = "",
    notes: str = "",
    companies: Optional[List[str]] = None,
    people: Optional[List[str]] = None,
) -> Audience:
    a = Audience({
        "slug": slugify(name),
        "name": name,
        "description": description,
        "industry": industry,
        "titles": titles or [],
        "seniority": seniority or (["owner"] if not titles else []),
        "countries": [c.strip().upper() for c in (countries or []) if c.strip()],
        "cities": [c.strip() for c in (cities or []) if c.strip()],
        "keywords": keywords or [],
        "exclude_titles": exclude_titles or [],
        "exclude_keywords": exclude_keywords or [],
        "headcount": headcount or [],
        # Seeds. A research brief usually NAMES the firms and founders it wants;
        # a seed query per name is the most precise scope a web index offers,
        # so they are fired first and count as evidence at qualification.
        "companies": [c.strip() for c in (companies or []) if c.strip()],
        "people": [c.strip() for c in (people or []) if c.strip()],
        "notes": notes,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fired": [],
    })
    return a


def save(a: Audience) -> Path:
    AUDIENCES.mkdir(parents=True, exist_ok=True)
    a["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p = AUDIENCES / ("%s.json" % a["slug"])
    p.write_text(json.dumps(a, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def load(slug: str) -> Audience:
    p = AUDIENCES / ("%s.json" % slugify(slug))
    if not p.exists():
        have = ", ".join(x["slug"] for x in load_all()) or "(none stored yet)"
        raise SystemExit(
            "No stored audience %r.\nStored audiences: %s\n"
            "Define one with:  ./li-search define \"<name>\" --industry ... --countries ..."
            % (slug, have)
        )
    return Audience(json.loads(p.read_text(encoding="utf-8")))


def load_all() -> List[Audience]:
    if not AUDIENCES.exists():
        return []
    out = []
    for p in sorted(AUDIENCES.glob("*.json")):
        try:
            out.append(Audience(json.loads(p.read_text(encoding="utf-8"))))
        except Exception:
            continue
    return out


def record_fire(a: Audience, run_id: str, found: int) -> None:
    a.setdefault("fired", []).append({
        "run_id": run_id,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "leads": found,
    })
    save(a)
