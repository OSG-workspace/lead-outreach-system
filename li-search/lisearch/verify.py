"""VERIFY — the delivery gate. The one place this tool spends money on a person.

WHAT THIS IS FOR, AND WHAT IT IS DELIBERATELY NOT FOR
Sourcing stays deterministic and free: ddgs and openweb find the profiles, and
every account this tool emits traces back to a real index hit. A search-grounded
model is never asked to PRODUCE leads — asked to list profiles it invents slugs,
and "every slug came from an index" is the whole guarantee. It is asked exactly
one question, about a row that already exists:

    is this person the OWNER of a business in the TARGET SECTOR?

That is the only question `qualify()` cannot always answer. Qualification reads
title / geo / industry off the row's own SERP text ([fire.py] qualify), and on
2026-09-03 that left 265 of 6,414 rows qualified (4.1%) across the four Lebanon
audiences. The rows it cannot settle are not badly written, they are opaque:
502 of the 574 "owner title + Lebanon, industry unknown" rows in the marketing
audience carry a company name no regex can classify — JADWA, IN ACTION, Flow
Beirut, Deep Management.

MEASURED, ON THIS TOOL'S OWN ROWS (2026-09-04, 20 rows, perplexity/sonar, $0.111)
  6 of 16 unqualified rows resolved to a genuine in-sector owner;
  every rejection was correct — a hotel GM, a travel agency, BUTEC (EPC
  contracting), Cortas (food manufacturing), a coding academy, and four
  agency EMPLOYEES (COO, comms manager, marketing director) who are not owners;
  and of 4 rows the deterministic qualifier had already PASSED, one was
  off-spec: "Said Mehanna, Owner & Managing Director, ENERGIA sarl" —
  renewables and water treatment, not an agency. It was already delivered.

WHY IT GATES DELIVERY AND NOT THE POOL
A full enrichment pass over the 2,906 named, in-geo, one-axis-short rows costs
~$16 and would add ~1,090 qualified accounts. But volume is not the constraint:
the LinkedIn channel drips 8→18 invites/day, 90/week, and the pool already holds
265 qualified plus 278 delivered. Buying leads that cannot be sent is spending on
the wrong axis. Buying certainty about the ~50 rows a batch actually hands over
costs cents. So the gate sits in `export()`, on the rows about to be delivered.

THE REJECTION RULE IS ASYMMETRIC, AND THAT IS THE POINT
The same lesson the email chain paid for with its SMTP ladder: a positive is a
good signal and a negative is a useless one. Silence here means "the index did
not settle it", not "this person is off-spec", so ONLY a confident, identified
negative rejects a row. Everything else is delivered, stamped `unverified`. A
gate that dropped rows on absence of evidence would quietly shrink the pool for
the diaspora-thin, Arabic-named half of every Lebanese audience.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import cache as cache_mod
from .compliance import assert_allowed_host

ROOT = Path(__file__).resolve().parent.parent
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("LI_VERIFY_MODEL", "perplexity/sonar")

# 30 days, matching the page cache. A role IS a change signal and the cache
# module is right that those are worth nothing once stale — but re-asking the
# same account on every export is paying twice for an answer that changes maybe
# once a year, and `export` only ever asks about rows it has never delivered.
# `--verify-ttl 0` forces a fresh answer when that matters.
DEFAULT_TTL = 30 * 86400

MEASURED_COST_PER_ROW = 0.0056     # 2026-09-04, 20 rows, $0.1111

SYSTEM_PROMPT = """You verify ONE person for a prospecting list. You are given
what a web index already showed about them. Search the web to settle the gaps.

ANSWER ONLY ABOUT THIS EXACT PERSON. A confident answer about a different person
of the same name is the worst possible outcome. If you cannot find this person,
set identified false — that is a normal and useful answer.

You report FACTS: who they are, what they do, what the business is. You do not
decide whether they belong on the list.

Never invent a LinkedIn URL, a company, or a role. Never guess a sector from a
company name alone; if the name tells you nothing and no source describes the
business, say so with confidence "low".

- company / company_sector: the business they hold that role at, and what that
  business actually does, in a few words.
- role: report it EXACTLY as the source words it, and do not soften or inflate
  it. This is the field that decides whether the person is senior enough — a
  ladder outside this conversation applies that test, so "Managing Director" is
  a complete and useful answer and you must not second-guess whether it counts.
- is_owner: your own read of whether they LEAD that business rather than work
  in it. It is recorded, but the role above is what the decision uses.
- in_sector: true ONLY if that business IS one of the kinds of business listed
  under SECTOR. Judge the BUSINESS, never the location: the pipeline has
  already confirmed where this person is, and the location line is given only
  so you can tell them apart from a namesake elsewhere. A company that is the
  right kind of business is in_sector true even if you are unsure it sits in
  that country.
- confidence: "high" when a source states the role and the business plainly;
  "medium" when you are inferring the sector; "low" when you are unsure of
  either. Downstream, only a high/medium answer can reject anyone, so an honest
  "low" costs nothing and a confident guess costs a real lead.

Reply with ONE JSON object, no markdown fence, no commentary:
{"row_id":"<echo exactly>","identified":true|false,"role":"","company":"",
"company_sector":"","is_owner":true|false,"in_sector":true|false,
"confidence":"high"|"medium"|"low","source_url":"","reason":""}"""

CONFIRMED = "confirmed"
REJECTED = "rejected"
UNVERIFIED = "unverified"


def api_key() -> str:
    """OPEN_ROUTER_API_KEY. cli.load_config() has already folded li-search/.env
    and project/.env into the environment by the time this runs."""
    for name in ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(name):
            return os.environ[name].strip()
    return ""


def available() -> bool:
    return bool(api_key())


def target_sentence(audiences: List[Dict[str, Any]]) -> str:
    """The specification the gate judges against, built from the stored
    audiences rather than typed by hand — the gate must judge against the same
    brief `qualify()` does, or it becomes a second, divergent definition of it.

    LOCATION IS CONTEXT, NOT A TEST. It appears so the model can tell the
    target apart from a namesake abroad, and is labelled as already settled.
    Geography is the one axis the deterministic path is genuinely good at (the
    profile's own LinkedIn country subdomain, plus city text), and it fails on
    almost nothing: of 6,414 rows on 2026-09-03 only 83 missed on geo. Letting
    the model re-judge it cost two false rejections on the very first live run
    — a creative studio and a branding agency, both refused with "not a
    Lebanon/Batroun-based agency" in the reason.
    """
    # Never truncated: a sector silently dropped off the end of this list is a
    # confident, wrong rejection of a real lead.
    sectors: List[str] = []
    countries: List[str] = []
    cities: List[str] = []
    from .audience import COUNTRY_NAMES
    for a in audiences:
        for s in (a.subjects() if hasattr(a, "subjects") else []):
            # Arabic and other non-latin subject spellings are query fodder for
            # the index adapters, not prose; the model is prompted in English.
            if s not in sectors and re.search(r"[a-z]", s, re.I):
                sectors.append(s)
        for cc in (a.get("countries") or []):
            n = COUNTRY_NAMES.get(cc.upper(), cc)
            if n not in countries:
                countries.append(n)
        for c in (a.get("cities") or []):
            if c not in cities and re.search(r"[a-z]", c, re.I):
                cities.append(c)
    where = " / ".join(countries) or ", ".join(cities[:6]) or "(not specified)"
    if countries and cities:
        where += " (%s...)" % ", ".join(cities[:4])
    return ("SECTOR — the person must OWN or LEAD one of these kinds of business:\n"
            "  %s\n"
            "LOCATION — already confirmed by the pipeline, do NOT judge it, it is here\n"
            "only to tell this person apart from a namesake elsewhere: %s"
            % ("; ".join(sectors) or "the business described in the brief", where))


_LADDER: List[Any] = []


def _default_ladder():
    """An audience carrying only this tool's owner-equivalent title ladder."""
    if not _LADDER:
        from .audience import new_audience
        _LADDER.append(new_audience("owner-ladder", industry="", countries=[], cities=[]))
    return _LADDER[0]


class Spec:
    """Everything needed to judge ONE row: the prose the model is asked, and the
    audience whose title ladder decides seniority.

    THE MODEL SUPPLIES FACTS, THIS TOOL APPLIES THE CONTRACT. Asked to decide
    `is_owner` itself, perplexity/sonar returned false for two Managing
    Directors on the first live run — both correctly identified, both in
    sector, both refused — because it reads "owner" as equity rather than as
    this operator's ladder, where a Levant SME's Managing Director IS the
    decider (see audience.DEFAULT_TITLES). So the model reports the role
    verbatim, which it does accurately, and `fire.title_hit` decides whether
    that role qualifies — the SAME function that qualified the row in the first
    place. One definition of "owner" in the tool, not two."""

    def __init__(self, text: str, audience: Optional[Dict[str, Any]] = None):
        self.text = text
        self.audience = audience

    def role_ok(self, role: str, model_said: bool) -> bool:
        """Whole-word owner ladder + the DEMOTERS ("Vice President", "Assistant
        to the CEO", "Former Owner"). With no audience to judge against — a row
        whose audience was renamed away — the model's own read is all there is.

        Tested against the audience's own titles AND against
        audience.DEFAULT_TITLES, because a stored audience narrows the SEARCH
        (its title list is what the adapters spend their query budget on), not
        the definition of a decider. All four Lebanon audiences dropped "Chief
        Executive Officer" from their custom list in favour of "CEO" and four
        Arabic titles, so the gate refused Impact BBDO's "Chief Executive
        Officer Levant" and Quantum's "Chief Executive" — both plainly the
        boss. `qualify()` is deliberately left alone: widening it there would
        change what every past fire counted as qualified."""
        if self.audience is None:
            return bool(model_said)
        if not (role or "").strip():
            return False
        from .fire import title_hit
        lead = {"title": role}
        return bool(title_hit(lead, self.audience) or title_hit(lead, _default_ladder()))


def as_spec(target: Any, row: Dict[str, Any]) -> Spec:
    t = target(row) if callable(target) else target
    return t if isinstance(t, Spec) else Spec(str(t))


def specs(audiences: List[Dict[str, Any]]) -> Dict[str, Spec]:
    """{slug: specification}. A row is judged against ITS OWN audience, never
    against the union of the brief's four.

    shughol-lebanon is four audiences wide — agencies, PR/production,
    consultancies, dev shops — and a row found by one of them is regularly a
    business belonging to another. What every row shares is the BRIEF, so that
    is what the sector question asks. What differs is the title ladder, which
    is per audience, so that is what the Spec carries."""
    # ONE sector list — the brief's — with each row's OWN audience supplying the
    # title ladder. Sector is per BRIEF because delivery is per brief: an
    # advertising-agency owner the PR audience happened to surface is still a
    # lead the operator asked for, and judging that row against PR subjects
    # alone refused four good rows on the second live run (Impact BBDO,
    # InfoPro, TSP, Quantum Communications).
    text = target_sentence(audiences)
    return {a["slug"]: Spec(text, a) for a in audiences if a.get("slug")}


def spec_for(row: Dict[str, Any], by_slug: Dict[str, Spec], fallback: Spec) -> Spec:
    return by_slug.get((row.get("audience") or "").strip(), fallback)


def build_prompt(row: Dict[str, Any], target: str) -> str:
    return ("%s\n\nPerson (from a web index):\n"
            "Name: %s\nLinkedIn: %s\nHeadline/title text: %s\nCompany text: %s\n"
            "Location text: %s\nIndex snippet: %s\nrow_id: %s\n\n"
            "Verify their current role and their company's sector. Return the JSON."
            % (target,
               row.get("full_name") or "(unknown)",
               row.get("linkedin_url") or "",
               row.get("title") or "(none)",
               row.get("company") or "(none)",
               row.get("location") or "(none)",
               (row.get("summary") or "(none)")[:400],
               row.get("linkedin_account") or ""))


_JSON = re.compile(r"\{.*\}", re.S)


def _post(payload: dict, key: str, timeout: int) -> dict:
    # The tool's own rule, enforced on this path too: the gate reaches a model
    # API, never linkedin.com. Nothing here fetches a profile.
    assert_allowed_host(ENDPOINT)
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ask_model(row: Dict[str, Any], target: Any, *, key: str, model: str = DEFAULT_MODEL,
              timeout: int = 120, retries: int = 3) -> Tuple[Dict[str, Any], float]:
    """One row -> (raw model answer, cost in USD). The only network call here."""
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": build_prompt(row, as_spec(target, row).text)}]}
    last = ""
    for attempt in range(retries):
        try:
            d = _post(payload, key, timeout)
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code not in (408, 409, 429, 500, 502, 503, 504):
                break                      # auth/quota: retrying cannot help
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:             # network / timeout
            last = str(e)[:120]
            time.sleep(2 * (attempt + 1))
            continue
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        cost = float((d.get("usage") or {}).get("cost") or 0.0)
        m = _JSON.search(re.sub(r"```(?:json)?", "", content))
        if not m:
            last = "no JSON in reply"
            continue
        try:
            return json.loads(m.group(0)), cost
        except json.JSONDecodeError:
            last = "malformed JSON"
            continue
    return {"identified": False, "confidence": "low",
            "reason": "verification call failed after %d attempt(s): %s" % (retries, last)}, 0.0


def decide(ans: Dict[str, Any], spec: Optional[Spec] = None) -> str:
    """The asymmetric rule, in one function so it can be tested without a key.

    REJECT only a confident, identified negative. Everything else — not found,
    low confidence, a failed call — is UNVERIFIED and still gets delivered. The
    measured basis: on the probe every `identified` high/medium negative was
    genuinely off-spec (hotel, travel, EPC, food, coding academy, four
    employees), while the `identified:false` rows were simply thin index
    coverage, which is not evidence of anything.

    Seniority is decided by `spec.role_ok`, this tool's own ladder, not by the
    model's `is_owner` — see Spec.

    Takes either a fresh model answer or a cached record: both carry the same
    five fields this reads, which is what lets a cached row be re-judged under
    a changed policy for free."""
    if not ans.get("identified"):
        return UNVERIFIED
    if (ans.get("confidence") or "low").lower() not in ("high", "medium"):
        return UNVERIFIED
    ok_role = (spec or Spec("")).role_ok(ans.get("role") or "", ans.get("is_owner"))
    if ok_role and ans.get("in_sector"):
        return CONFIRMED
    return REJECTED


def verify_row(row: Dict[str, Any], target: Any, *, key: str = "", model: str = DEFAULT_MODEL,
               ttl: int = DEFAULT_TTL, timeout: int = 120,
               ask: Optional[Callable[..., Tuple[Dict[str, Any], float]]] = None) -> Dict[str, Any]:
    """-> {verdict, role, sector, source, confidence, reason, cost, cached}.

    Cached on (account, target, model): the same person judged against the same
    brief is the same answer, and `export` must be re-runnable without paying
    again for rows it has already seen."""
    acct = (row.get("linkedin_account") or "").strip().lower()
    spec = as_spec(target, row)
    # The spec is part of the key: the same person judged against a different
    # brief is a different question and must be asked again.
    ckey = {"account": acct, "target": spec.text, "model": model}
    hit = cache_mod.get("verify", ckey, ttl=ttl) if acct else None
    if hit is not None:
        # RE-DECIDE from the stored facts rather than trusting the stored
        # verdict. What was cached is the model's answer — identified, role,
        # is_owner, in_sector, confidence — and the verdict is policy applied to
        # it. When the policy changes, as it did the day the ladder learned that
        # "Chief Technology Officer" and "CTO" are one title, every affected row
        # must move without being paid for a second time.
        return dict(hit, verdict=decide(hit, spec), cost=0.0, cached=True)
    ans, cost = (ask or ask_model)(row, target, key=key, model=model, timeout=timeout)
    out = {"verdict": decide(ans, spec),
           # The raw flags are stored, not just the verdict: a refusal is a paid
           # judgement about a real person, and "which half failed — the role or
           # the sector?" is the first question the operator asks of it.
           "identified": bool(ans.get("identified")),
           "is_owner": bool(ans.get("is_owner")),
           "role_qualifies": spec.role_ok(ans.get("role") or "", ans.get("is_owner")),
           "in_sector": bool(ans.get("in_sector")),
           "role": (ans.get("role") or "").strip(),
           "company": (ans.get("company") or "").strip(),
           "sector": (ans.get("company_sector") or "").strip(),
           "source": (ans.get("source_url") or "").strip(),
           "confidence": (ans.get("confidence") or "").strip().lower(),
           "reason": (ans.get("reason") or "").strip()[:300],
           "checked_at": time.strftime("%Y-%m-%d")}
    if acct and ttl > 0:
        cache_mod.put("verify", ckey, out)
    return dict(out, cost=cost, cached=False)


def verify_rows(rows: List[Dict[str, Any]], target: Any, *, key: str = "",
                model: str = DEFAULT_MODEL, ttl: int = DEFAULT_TTL, workers: int = 8,
                timeout: int = 120, ask: Optional[Callable[..., Tuple[Dict[str, Any], float]]] = None,
                on_done: Optional[Callable[[int, int, Dict[str, Any], Dict[str, Any]], None]] = None,
                ) -> Dict[str, Dict[str, Any]]:
    """Verify a block of rows in parallel -> {account: result}. Order-independent:
    the caller decides what to do with each verdict."""
    out: Dict[str, Dict[str, Any]] = {}
    if not rows:
        return out
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(verify_row, r, target, key=key, model=model, ttl=ttl,
                          timeout=timeout, ask=ask): r for r in rows}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            r = futs[fut]
            res = fut.result()
            out[(r.get("linkedin_account") or "").strip().lower()] = res
            if on_done:
                on_done(i, len(rows), r, res)
    return out
