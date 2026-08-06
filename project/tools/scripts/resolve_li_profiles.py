#!/usr/bin/env python3
"""Stage 8.6b: company -> the owner's LinkedIn PROFILE, via ordinary web search.

WHY THIS EXISTS (user directive 2026-07-31)
"If not able to source from LinkedIn you can source them normally like the other
ones. The important part is the outreach should be done on LinkedIn and the
LinkedIn account should be found."

The walker resolves people by opening a company's LinkedIn page and reading its
People tab. In the Gulf that page frequently does not exist: a real brokerage
with thirty agents may have no LinkedIn company presence at all while its owner
personally does. Those companies were simply lost — sourced, qualified, and then
silently unreachable.

So this is the SECOND route to the same destination, and it changes nothing
about the chain's shape: the company is still sourced normally (OSM), the
outreach still happens on LinkedIn, and the person still passes exactly the same
gates in qualify_people.py. Only the way we learn the profile URL differs.

It is deliberately NOT a fallback that lowers the bar. li-finder must SEE a real
linkedin.com/in/ profile tied to this company by a source; a constructed or
guessed slug is refused, because a wrong profile pitches a stranger about
someone else's business and burns them in the ledger permanently.

    phase prep    li-companies.json + people-raw.json -> lif-batch-NNN.txt
                  (only companies the walk produced NO person for)
    phase merge   lif-out/*.json -> appended to people-needing-harvest.json

Between the two, run_fire dispatches one li-finder agent per batch file. After
merge, walk_companies.js --profiles harvests the gate fields for the found
profiles, exactly as it does for people found the normal way.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from li_url import canonical_profile_url as normalize_profile_url  # noqa: E402,F401


def _norm_company(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def prep(run: Path, cap: int) -> int:
    companies = json.loads((run / "li-companies.json").read_text())
    people = []
    praw = run / "people-raw.json"
    if praw.exists():
        try:
            people = json.loads(praw.read_text())
        except Exception:
            people = []

    covered = {_norm_company(p.get("company")) for p in people if p.get("company")}
    gaps = [c for c in companies if _norm_company(c.get("name")) not in covered]

    outdir = run / "lif-out"
    outdir.mkdir(exist_ok=True)
    for f in run.glob("lif-batch-*.txt"):
        f.unlink()

    for i, c in enumerate(gaps[:cap], 1):
        (run / f"lif-batch-{i:03d}.txt").write_text(
            f"OutputFile: {(outdir / f'{i:03d}.json').resolve()}\n"
            f"Company:    {c.get('name','')}\n"
            f"City:       {c.get('city') or ''}\n"
            f"Country:    {c.get('country') or ''}\n"
            f"Vertical:   {c.get('vertical') or ''}\n"
            f"Website:    {c.get('domain') or ''}\n"
        )
    print(f"Stage 8.6b prep: {len(companies)} companies, {len(people)} already have a person, "
          f"{len(gaps)} gaps -> {min(len(gaps), cap)} li-finder batches")
    return min(len(gaps), cap)


def merge(run: Path) -> int:
    companies = {_norm_company(c.get("name")): c
                 for c in json.loads((run / "li-companies.json").read_text())}
    outdir = run / "lif-out"
    found, rejected = [], 0
    seen: set[str] = set()

    for f in sorted(outdir.glob("*.json")) if outdir.exists() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            print(f"  WARN: {f.name} is not valid JSON — skipped")
            continue
        if not d.get("found"):
            continue
        url = normalize_profile_url(d.get("profile_url", ""))
        if not url:
            rejected += 1
            continue
        if url in seen:
            continue
        # An agent with no evidence must not be able to contribute a lead: the
        # whole point of this route is that it is as strict as the walk, not
        # looser. No source tying person to company -> not usable.
        if not (d.get("evidence_url") or "").strip():
            rejected += 1
            continue
        co = companies.get(_norm_company(d.get("company")))
        if not co:
            rejected += 1
            continue
        seen.add(url)
        found.append({
            "full_name": d.get("full_name") or None,
            "title": d.get("title") or "",
            "profile_url": url,
            "profile_source": "li-finder",
            "profile_evidence": d.get("evidence_url"),
            "company": co.get("name"),
            "company_domain": co.get("domain"),
            # Geo anchor stays OUR sourcing's, never the profile's own location.
            "company_country": co.get("country"),
            "company_city": co.get("city"),
            "company_headcount": co.get("headcount"),
            "vertical": co.get("vertical"),
        })

    out = run / "people-found.json"
    out.write_text(json.dumps(found, ensure_ascii=False, indent=2))
    print(f"Stage 8.6b merge: {len(found)} profiles found by search, {rejected} rejected "
          f"(not a real /in/ profile, no evidence, or unknown company)")
    return len(found)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["prep", "merge"])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--cap", type=int, default=120,
                    help="max companies to resolve in one run (one agent each)")
    a = ap.parse_args()
    run = Path(a.run_dir)
    if not (run / "li-companies.json").exists():
        sys.exit("ABORT: li-companies.json missing (run draft_linkedin.py --phase prep first)")
    n = prep(run, a.cap) if a.phase == "prep" else merge(run)
    if a.phase == "merge" and n == 0:
        sys.exit(7)     # nothing found — run_fire decides whether that is terminal


if __name__ == "__main__":
    main()
