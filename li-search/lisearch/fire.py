"""FIRE — run a stored audience across every enabled provider and rank the result.

The shape is deliberately the opposite of the email chain's: no ledger, no
consumption, no send. A fire here is READ-ONLY and repeatable, and it is
CUMULATIVE: every prior run of the same audience is folded into this one, so
firing on a day one engine is sulking never shrinks the pool — it only ever
adds. The run folder is therefore always the best list this audience has ever
produced, not just what today's engines felt like returning.
"""
from __future__ import annotations

import csv
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import audience as aud
from .adapters import build
from .compliance import apply_suppression
from .lead import dedupe
from .adapters.base import looks_like_role

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def status(run_dir: Path, line: str) -> None:
    """Overwrite runs/<id>/status.txt with the current stage — the same live
    heartbeat the email chain keeps, so a session can `cat` one short line
    instead of scrolling a log to learn where a fire is."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "status.txt").write_text(
            "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), line), encoding="utf-8")
    except Exception:
        pass


def done_line(res: Dict[str, Any]) -> str:
    """One line that carries everything a session needs to relay. The playbook
    relays THIS, plus file paths — never the lead rows themselves."""
    prov = ",".join("%s:%s" % (k, ("STOPPED" if k in res.get("stopped", {}) else
                                   "skip" if k in res.get("skipped", []) else v))
                    for k, v in res["providers"].items())
    return ("DONE: %s — kept=%d qualified=%d strong=%d new=%d unique=%d excluded=%d providers=%s"
            % (res["audience"], len(res["leads"]), res["qualified"], res.get("strong", 0), res["new"],
               res["unique"], res["excluded"], prov))

SOURCE_WEIGHT = {"exa": 1.0, "coresignal": 1.0, "pdl": 0.95, "ddgs": 0.7, "delivered": 0.7, "openweb": 0.55}

# A title containing one of these is NOT the decider even when it contains an
# owner-equivalent word: "Vice President", "Assistant to the CEO", "Former Owner".
DEMOTERS = ("vice president", "vp ", "assistant", "deputy", "to the ", "office of",
            "former", "ex-", "secretary", "intern", "associate")


def _word(t: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(t.lower()))


def title_hit(lead: Dict[str, Any], a: aud.Audience) -> Optional[Tuple[int, str]]:
    """(rank, matched title) when the lead's title carries an owner-equivalent
    title as a whole word — 'Partner' must not match 'Partnerships Manager',
    and 'President' must not match 'Vice President'."""
    title = (lead.get("title") or "").lower()
    if not title:
        return None
    if any(d in title for d in DEMOTERS):
        return None
    for i, t in enumerate(a.titles()):
        if _word(t).search(title):
            return i, t
    return None


def geo_hit(lead: Dict[str, Any], a: aud.Audience) -> str:
    """'city' > 'country' > ''. City beats country because the diaspora problem
    is real: a 'Beirut' headline on someone living in Paris is the classic false
    match. The country comes from the profile's LinkedIn subdomain when the
    provider gave us a URL, which is the cheapest reliable geo signal there is."""
    loc = " ".join([lead.get("location") or "", lead.get("summary") or "",
                    lead.get("company") or "", lead.get("title") or ""]).lower()
    if any(_word(c).search(loc) for c in (a.get("cities") or [])):
        return "city"
    countries = [c.upper() for c in (a.get("countries") or [])]
    if lead.get("country") and lead["country"].upper() in countries:
        return "country"
    if any(aud.COUNTRY_NAMES.get(c, c).lower() in loc for c in countries):
        return "country"
    return ""


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _company_in(company: str, folded_blob: str) -> bool:
    """Whole-word match of a seeded firm name. 'tar' must not hit 'Qatar' or
    'startup', and anything under four characters is too short to trust."""
    fc = _fold(company)
    if len(fc) < 4:
        return False
    return bool(re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(fc), folded_blob))


def industry_hit(lead: Dict[str, Any], a: aud.Audience) -> List[str]:
    blob = " ".join(str(lead.get(k) or "") for k in
                    ("title", "company", "summary", "location")).lower()
    hits = [kw for kw in a.subjects() if kw.lower() in blob]
    # A seeded company name anywhere in the row is industry evidence of the
    # strongest kind: the brief named that firm.
    fb = _fold(blob)
    hits += ["company:%s" % c for c in (a.get("companies") or []) if _company_in(c, fb)]
    return hits


def person_hit(lead: Dict[str, Any], a: aud.Audience) -> Tuple[str, str]:
    """(name, how) for a seeded person whose name matches the row's name.
    `how` is what corroborates it: 'company' when the seed's firm appears in
    the row, 'title'/'industry' when the row itself shows the spec, or '' for
    a bare namesake — which is reported as a candidate, never qualified.
    Whole-name match on the folded form, so 'Hala Jaber' matches 'Hala Jaber'
    but not 'Hala Jaberi'."""
    name = _fold(lead.get("full_name") or "")
    if not name:
        return "", ""
    blob = _fold(" ".join(str(lead.get(k) or "") for k in ("title", "company", "summary")))
    for person, company in a.people_pairs():
        fp = _fold(person)
        if not fp or not re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(fp), name):
            continue
        if company and _company_in(company, blob):
            return person, "company"
        if title_hit(lead, a):
            return person, "title"
        if industry_hit(lead, a):
            return person, "industry"
        return person, ""
    return "", ""


_STOP = {"and", "the", "of", "for", "with", "firm", "company", "house", "shop", "shops",
         "services", "service", "solutions", "group", "management"}


def industry_words(a: aud.Audience) -> List[str]:
    """The individual words of the industry + keywords, minus glue words —
    what a headline actually carries ('Founder, Lemonade Digital' never says
    'digital marketing agency', but it does say 'digital')."""
    out: List[str] = []
    for s in a.subjects():
        for w in _fold(s).split():
            if len(w) >= 4 and w not in _STOP and w not in out:
                out.append(w)
    return out


def strength(lead: Dict[str, Any], a: aud.Audience) -> str:
    """'strong' when the industry is visible in the title, company or name —
    or the row is brief-seeded; 'weak' when the only industry evidence is the
    person's posts (the snippet), which is where a restaurant owner who wrote
    about an event comes from."""
    m = lead.get("match") or ""
    if m.startswith("person=") or "company:" in m:
        return "strong"
    head = _fold(" ".join([lead.get("title") or "", lead.get("company") or "",
                           lead.get("full_name") or ""]))
    for w in industry_words(a):
        if re.search(r"(?<![a-z0-9])%s" % re.escape(w), head):
            return "strong"
    return "weak"


def qualify(lead: Dict[str, Any], a: aud.Audience) -> None:
    """Stamp `qualified` and `match` on the lead. Qualified means the row shows,
    in its own text, all three parts of the stored specification: an
    owner-equivalent title, the target geography, and the target industry. A
    row that lacks one is kept and ranked, never dropped — but it is not
    counted as qualified, and `match` says exactly which part is missing."""
    t = title_hit(lead, a)
    g = geo_hit(lead, a)
    kws = industry_hit(lead, a)
    parts = []
    person, how = person_hit(lead, a)
    if person and how:
        # The brief named this person AND the row corroborates it.
        lead["qualified"] = True
        lead["match"] = "person=%s;via=%s" % (person, how) + ("" if not t else ";title=%s" % t[1])
        lead["strength"] = "strong"
        return
    if person:
        parts.append("person?=%s" % person)      # namesake: candidate only
    if t:
        parts.append("title=%s" % t[1])
    if g:
        parts.append("geo=%s" % g)
    if kws:
        parts.append("industry=%s" % kws[0])
    need_geo = bool(a.get("cities") or a.get("countries"))
    need_ind = bool(a.subjects())
    lead["qualified"] = bool(t) and (bool(g) or not need_geo) and (bool(kws) or not need_ind)
    lead["match"] = ";".join(parts) or "-"
    lead["strength"] = strength(lead, a) if lead["qualified"] else ""


def score(lead: Dict[str, Any], a: aud.Audience) -> float:
    """Rank, never filter — the operator asked for as many leads as possible, so
    a weak row is demoted rather than dropped. The one exception is an explicit
    exclusion, which is a stated instruction and is honoured as a filter."""
    s = 0.0

    t = title_hit(lead, a)
    if t:
        s += 3.0 - min(t[0], 8) * 0.1
    ph = person_hit(lead, a)
    if ph[0]:
        s += 4.0 if ph[1] else 1.0

    g = geo_hit(lead, a)
    s += {"city": 2.0, "country": 1.5}.get(g, 0.0)

    s += 0.75 * len(industry_hit(lead, a))

    # Completeness — a row you can act on beats a bare slug.
    for f, w in (("full_name", 1.0), ("company", 0.75), ("title", 0.5),
                 ("location", 0.4), ("email", 1.5)):
        if lead.get(f):
            s += w

    # Corroboration: two independent providers agreeing is the strongest signal
    # available without opening the profile.
    s += 1.5 * (len(lead.get("sources") or []) - 1)
    s *= max(SOURCE_WEIGHT.get(src, 0.6) for src in (lead.get("sources") or ["openweb"]))

    # Freshness. 'unknown' is not punished as hard as 'stale' — a monthly-batch
    # provider that does not date its rows is not the same as a known-old row.
    s += {"live": 1.0, "fresh": 0.75, "aging": 0.25, "unknown": 0.0, "stale": -0.5}.get(
        lead.get("freshness", "unknown"), 0.0)
    return round(s, 3)


def excluded(lead: Dict[str, Any], a: aud.Audience) -> bool:
    """Explicit exclusions from the stored spec. Titles are matched as whole
    phrases against the TITLE only; keywords as whole words against title and
    company only — never the summary, which is the person's posts, where a
    sector word like 'food' or 'hotel' appears in anyone's feed. Matching the
    summary knocked out 77 of 170 delivered rows on 2026-09-02, brief-named
    founders included."""
    title = _fold(lead.get("title") or "")
    # full_name is included because a company page indexed as a person
    # ("Nalbandian Carpets Beirut — CEO") carries its sector in the name.
    head = _fold(" ".join([lead.get("title") or "", lead.get("company") or "",
                           lead.get("full_name") or ""]))
    for t in (a.get("exclude_titles") or []):
        ft = _fold(t)
        if ft and re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(ft), title):
            return True
    for k in (a.get("exclude_keywords") or []):
        fk = _fold(k)
        # Word-START match, so 'architect' also catches 'architects' and
        # 'architecture', 'elevator' catches 'elevators'. Multi-word keywords
        # keep their internal spacing.
        if fk and re.search(r"(?<![a-z0-9])%s" % re.escape(fk), head):
            return True
    return False


def prior_leads(slug: str) -> Tuple[List[Dict[str, Any]], int]:
    """Every lead any earlier run of this audience wrote — kept AND excluded
    rows, so a spec change can never erase merged history — plus every row
    ever DELIVERED for this audience from results/*/owners.csv. Scores and
    flags are recomputed at merge time, so only the record matters."""
    out: List[Dict[str, Any]] = []
    runs = 0
    if RUNS.exists():
        for d in sorted(RUNS.glob("*-%s" % slug)):
            f = d / "leads.json"
            if not f.exists():
                continue
            try:
                blob = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            runs += 1
            for l in (blob.get("leads") or []) + (blob.get("excluded_rows") or []):
                l = dict(l)
                for k in ("score", "qualified", "match", "excluded"):
                    l.pop(k, None)
                l.setdefault("sources", ["prior"])
                out.append(normalise(l))
    results = ROOT / "results"
    if results.exists():
        import csv
        for f in results.glob("*/owners.csv"):
            try:
                rows = list(csv.DictReader(f.open(encoding="utf-8")))
            except Exception:
                continue
            for r in rows:
                if r.get("audience") != slug or not r.get("linkedin_account"):
                    continue
                from .lead import make_lead
                out.append(make_lead("delivered", r["linkedin_account"], full_name=r.get("full_name", ""),
                                     title=r.get("title", ""), company=r.get("company", ""),
                                     location=r.get("location", ""), freshness_days=None))
    return out, runs


def normalise(l: Dict[str, Any]) -> Dict[str, Any]:
    """Repair the one historical parse defect on load, so a prior run never
    re-infects a fresh one through the merge: a 'title' that carries no role
    word and no company beside it was the 'Name - Company' page-title shape
    filed under the wrong field. Move it, and leave the title honestly empty."""
    # Same ellipsis cut the headline parser applies now; older run files
    # predate it, and a longer stale title would otherwise win the merge.
    for k in ("title", "company", "full_name"):
        if l.get(k):
            l[k] = re.split(r"\.\.\.|…", l[k])[0].strip()
    t, c = l.get("title") or "", l.get("company") or ""
    if t and not looks_like_role(t) and (not c or c.strip().lower() == t.strip().lower()):
        l["company"], l["title"] = t, ""
    elif t and c and t.strip().lower() == c.strip().lower():
        l["title"] = ""
    return l


def fire(
    a: aud.Audience,
    providers: List[str],
    config: Dict[str, Any],
    limit: int = 500,
    ttl: int = 30 * 86400,
    dry_run: bool = False,
    quiet: bool = False,
) -> Dict[str, Any]:
    say = (lambda *a, **k: None) if quiet else print
    run_id = "%s-%s" % (time.strftime("%Y-%m-%d"), a["slug"])
    run_dir = RUNS / run_id
    per_source: Dict[str, int] = {}
    stopped: Dict[str, str] = {}
    skipped: List[str] = []
    fresh: List[Dict[str, Any]] = []

    say("FIRE  %s  limit=%d  titles=%s  geo=%s  site=%s" % (
        a["slug"], limit, "/".join(a.titles()[:4]) + ("…" if len(a.titles()) > 4 else ""),
        "/".join(a.geo_terms()[:3]), ",".join(s for s, _ in a.site_filters())))
    if not dry_run:
        status(run_dir, "FIRE start providers=%s" % ",".join(providers))

    adapters = build(providers, config)
    for ad in adapters:
        if not ad.available():
            say("  - %-11s SKIPPED: %s" % (ad.name, ad.why_unavailable()))
            per_source[ad.name] = 0
            skipped.append(ad.name)
            continue
        if dry_run:
            say("  - %-11s would run (%s)" % (ad.name, ad.kind))
            per_source[ad.name] = 0
            continue
        t0 = time.time()
        status(run_dir, "provider %s running" % ad.name)
        try:
            got = ad.search(a, limit=limit, ttl=ttl)
        except SystemExit:
            raise
        except Exception as e:
            # A provider dying mid-run keeps whatever it already returned. Half a
            # result set is worth more than none, and the reason is printed so it
            # is never mistaken for an empty market.
            partial = getattr(e, "partial", None) or []
            per_source[ad.name] = len(partial)
            fresh.extend(partial)
            stopped[ad.name] = str(e)
            say("  - %-11s STOPPED after %d: %s" % (ad.name, len(partial), str(e).split(". ")[0]))
            continue
        per_source[ad.name] = len(got)
        fresh.extend(got)
        say("  - %-11s %4d profile(s) in %.1fs" % (ad.name, len(got), time.time() - t0))

    # Fold in every earlier run of this audience. New accounts are the ones no
    # prior run had; everything else is corroborated or refreshed, never lost.
    prior, prior_runs = ([], 0) if dry_run else prior_leads(a["slug"])
    prior_accounts = {l.get("linkedin_account") for l in prior}

    merged = [normalise(l) for l in dedupe(fresh + prior)]   # fresh values win, prior fills gaps
    kept = [l for l in merged if not excluded(l, a)]
    excluded_rows = [l for l in merged if excluded(l, a)]
    dropped_excl = len(merged) - len(kept)
    kept = apply_suppression(kept)
    dropped_sup = len(merged) - dropped_excl - len(kept)

    new_accounts = {l["linkedin_account"] for l in kept} - prior_accounts

    for l in kept:
        qualify(l, a)
        l["score"] = score(l, a)
    kept.sort(key=lambda l: (not l["qualified"], l.get("strength") != "strong", -l["score"],
                             l.get("full_name") or l["linkedin_account"]))
    kept = kept[:limit]

    result = {
        "run_id": run_id,
        "audience": a["slug"],
        "fired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "providers": per_source,
        "raw": len(fresh),
        "prior_runs": prior_runs,
        "carried_over": len(prior_accounts),
        "new": len(new_accounts),
        "unique": len(merged),
        "excluded": dropped_excl,
        "suppressed": dropped_sup,
        "qualified": sum(1 for l in kept if l["qualified"]),
        "strong": sum(1 for l in kept if l.get("strength") == "strong"),
        "leads": kept,
        # Excluded rows travel with the run so a later spec change can bring
        # them back with their merged history intact. Not in csv/accounts.
        "excluded_rows": excluded_rows,
        "eu_records": sum(1 for l in kept if l.get("eu_flag")),
        "stopped": stopped,
        "skipped": skipped,
    }

    # A fire that fetched NOTHING (every provider skipped or blocked before its
    # first row) must never rewrite a run folder that holds real output. With
    # the carry-over above it would write the same rows back, but the honest
    # thing is to leave the earlier file — and its timestamp — alone.
    prior_file = run_dir / "leads.json"
    if not dry_run and not fresh and prior_file.exists():
        try:
            had = len(json.loads(prior_file.read_text(encoding="utf-8")).get("leads") or [])
        except Exception:
            had = 0
        if had:
            print("\n  REFUSING to overwrite %s: it holds %d lead(s) and this fire "
                  "fetched nothing.\n  Nothing was written. Fix the provider and re-fire."
                  % (run_dir.name, had))
            result["run_dir"] = None
            result["preserved_prior"] = had
            return result

    if not dry_run:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "leads.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        (run_dir / "audience.json").write_text(
            json.dumps(a, indent=2, ensure_ascii=False), encoding="utf-8")
        write_csv(run_dir / "leads.csv", kept)
        (run_dir / "accounts.txt").write_text(
            "\n".join(l["linkedin_account"] for l in kept) + "\n", encoding="utf-8")
        (run_dir / "accounts-qualified.txt").write_text(
            "\n".join(l["linkedin_account"] for l in kept if l["qualified"]) + "\n",
            encoding="utf-8")
        result["run_dir"] = str(run_dir)
        # summary.json is the small file a session reads instead of leads.json
        # (which is ~1 KB per lead). Everything except the rows.
        summary = {k: v for k, v in result.items() if k not in ("leads", "excluded_rows")}
        summary["top"] = [{"account": l["linkedin_account"], "name": l.get("full_name"),
                           "title": l.get("title"), "company": l.get("company"),
                           "match": l.get("match")} for l in kept[:10]]
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
        status(run_dir, done_line(result))
    return result


CSV_FIELDS = ["linkedin_account", "qualified", "strength", "match", "full_name", "title", "company",
              "location", "country", "email", "linkedin_url", "score", "sources",
              "freshness", "collected_at", "eu_flag"]


def write_csv(path: Path, leads: List[Dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for l in leads:
            row = dict(l)
            row["sources"] = "+".join(l.get("sources") or [])
            row["qualified"] = "yes" if l.get("qualified") else "no"
            w.writerow(row)
