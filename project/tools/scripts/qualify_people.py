#!/usr/bin/env python3
"""Stage 5.3-LI: qualify PEOPLE (the LinkedIn chain's verify equivalent).

THE INVERSION
The email chain enriches a company until it yields a contact point
(name -> domain -> info@ -> ZeroBounce verify). LinkedIn starts from a PERSON
and enriches backward into a company. So the lead record is no longer a place
with a phone number; it is a person linked to a company, and "usable" stops
meaning "the address bounced or it didn't".

Four gates replace ZeroBounce:

  1. TITLE TIER   normalized decision power. T1 Owner/GM/Chairman/CEO,
                  T2 Head-of/Director, T3 Manager. Market-specific: Lebanese
                  SMEs say "General Manager" where the West says CEO, so the
                  dictionary is per market, not global.
  2. ALIVENESS    posted / commented / reacted inside the window. Dormant
                  accounts are the single biggest source of wasted invites.
  3. REACHABILITY connection degree, open-profile flag, InMail-ability.
  4. GEO ANCHOR   THE DIASPORA TRAP. Never filter Lebanon on profile location:
                  "Beirut" profiles are full of people living in Dubai, Paris
                  and Montreal. Anchor on the CURRENT COMPANY's location
                  (company page, or the OSM address we already hold). Milder
                  but real in the Gulf, where people churn out of Dubai.

ALIVENESS ROUTES, IT DOES NOT DISCARD (explicit user directive)
A dormant person with the right title is not waste — we harvested their name
and exact title, which is a strict upgrade on info@. They are re-channelled to
the EMAIL chain with a person-level opener instead of being dropped:

    active   + right title -> route "linkedin"  (invite queue)
    dormant  + right title -> route "email"     (existing chain, named DM)
    wrong title / wrong geo -> route "drop"

INPUT   <run-dir>/people-raw.json      list of person records
OUTPUT  <run-dir>/people-qualified.json    route == "linkedin"
        <run-dir>/people-to-email.json     route == "email"  (re-channelled)
        <run-dir>/people-dropped.json      route == "drop"   (with reason)
Exit 7 = nothing qualified for EITHER channel (an expected mid-run state for a
target-leads loop; run_fire decides whether that is terminal).
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

# --- Title tiers -------------------------------------------------------------
# Ordered most-senior first; first match wins. Per-market overrides are merged
# on top from the fixture's linkedin.json "title_tiers".
DEFAULT_TIERS: dict[str, list[str]] = {
    "T1": [r"\bchairman\b", r"\bceo\b", r"\bchief executive\b", r"\bfounder\b",
           r"\bco-?founder\b", r"\bowner\b", r"\bproprietor\b", r"\bpartner\b",
           r"\bmanaging director\b", r"\bmanaging partner\b", r"\bpresident\b",
           # Levant/Gulf SMEs use GM where the West says CEO
           r"\bgeneral manager\b", r"\bdeputy general manager\b", r"\bg\.?m\.?\b"],
    "T2": [r"\bhead of\b", r"\bdirector\b", r"\bvp\b", r"\bvice president\b",
           r"\bcountry director\b", r"\bmedical director\b", r"\bproject director\b",
           r"\bmlro\b", r"\bcompliance officer\b", r"\baml\b"],
    "T3": [r"\bmanager\b", r"\bofficer\b", r"\blead\b", r"\bsupervisor\b",
           r"\bpractice manager\b", r"\bestimation\b", r"\btender\b", r"\bmeal\b"],
}
TIER_RANK = {"T1": 3, "T2": 2, "T3": 1}


def title_tier(title: str, tiers: dict) -> str | None:
    t = (title or "").lower()
    for tier in ("T1", "T2", "T3"):
        for pat in tiers.get(tier, []):
            if re.search(pat, t):
                return tier
    return None


def geo_ok(p: dict, wanted_cc: set[str], wanted_cities: set[str]) -> tuple[bool, str]:
    """Anchor on the COMPANY's location, never the person's profile location.

    A Beirut-headline profile living in Dubai is the single most common false
    positive in Lebanese sourcing; pitching a Beirut AI receptionist to an
    expat is pure waste. The person's own location is deliberately ignored."""
    if not wanted_cc and not wanted_cities:
        return True, "no-geo-filter"
    cc = (p.get("company_country") or "").upper()
    city = (p.get("company_city") or "").lower()
    if not cc and not city:
        return False, "no-company-location (profile location is NOT a substitute)"
    if wanted_cc and cc and cc not in wanted_cc:
        return False, f"company in {cc}, not {'/'.join(sorted(wanted_cc))}"
    if wanted_cities and city and city not in wanted_cities:
        return False, f"company city {city!r} outside the run's city list"
    return True, "company-anchored"


def reachable(p: dict) -> tuple[bool, str]:
    if p.get("degree") in (1, "1st"):
        return True, "1st-degree (message directly, no invite needed)"
    if p.get("open_profile"):
        return True, "open profile"
    if p.get("inmail_available"):
        return True, "InMail"
    if p.get("degree") in (2, "2nd", 3, "3rd"):
        return True, "invite"
    return False, "out of network and not open/InMail-able"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config", default="{}", help="fixture linkedin.json as JSON")
    ap.add_argument("--alive-days", type=int, default=90)
    a = ap.parse_args()
    run = Path(a.run_dir)
    cfg = json.loads(a.config or "{}")

    src = run / "people-raw.json"
    if not src.exists():
        sys.exit("ABORT: people-raw.json missing (Stage 2-LI produced no people)")
    people = json.loads(src.read_text())

    tiers = {k: list(DEFAULT_TIERS.get(k, [])) + list((cfg.get("title_tiers") or {}).get(k, []))
             for k in ("T1", "T2", "T3")}
    accept = set(cfg.get("accept_tiers") or ["T1", "T2"])
    alive_days = int(cfg.get("alive_days", a.alive_days))
    wanted_cc = {c.upper() for c in (cfg.get("countries") or [])}
    wanted_cities = {c.lower() for c in (cfg.get("cities") or [])}

    li, mail, dropped = [], [], []
    for p in people:
        tier = title_tier(p.get("title") or p.get("headline", ""), tiers)
        if tier not in accept:
            dropped.append({**p, "drop_reason": f"title tier {tier or 'none'} not in {sorted(accept)}"})
            continue
        ok, why = geo_ok(p, wanted_cc, wanted_cities)
        if not ok:
            dropped.append({**p, "drop_reason": f"geo: {why}"})
            continue
        last = p.get("last_activity_days")
        alive = last is not None and last <= alive_days
        rok, rwhy = reachable(p)
        rec = {**p, "title_tier": tier, "tier_rank": TIER_RANK[tier],
               "alive": alive, "last_activity_days": last,
               "reachable": rok, "reach_mode": rwhy, "geo_note": why}
        # Aliveness ROUTES, it does not discard.
        if alive and rok:
            li.append({**rec, "route": "linkedin"})
        else:
            reason = "dormant" if not alive else "unreachable"
            mail.append({**rec, "route": "email", "recycle_reason": reason})

    (run / "people-qualified.json").write_text(json.dumps(li, ensure_ascii=False, indent=2))
    (run / "people-to-email.json").write_text(json.dumps(mail, ensure_ascii=False, indent=2))
    (run / "people-dropped.json").write_text(json.dumps(dropped, ensure_ascii=False, indent=2))
    print(f"people: {len(people)} in -> {len(li)} LinkedIn-native, "
          f"{len(mail)} re-channelled to email, {len(dropped)} dropped")
    for r in ("title", "geo"):
        n = sum(1 for d in dropped if d["drop_reason"].startswith(r))
        if n:
            print(f"  dropped[{r}]: {n}")
    if not li and not mail:
        sys.exit(7)


if __name__ == "__main__":
    main()
