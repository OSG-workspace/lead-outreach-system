#!/usr/bin/env python3
"""Stage 0-LS: hand a li-search audience to the LinkedIn outreach channel.

WHY THIS IS A FILE HANDOFF AND NOT A SHARED MODULE
`li-search/` researches people and is forbidden (lisearch/compliance.py) from
ever touching linkedin.com or a logged-in session. `project/linkedin/` sends
invites and DMs by driving the operator's own logged-in Chrome. Those two
postures are different on purpose, so the bridge between them is one-way and
lives on the SENDING side: this script READS li-search's delivered pool and
writes a normal run folder that the rest of the LinkedIn arm already knows how
to finish (draft_linkedin.py batch -> li-writer -> merge -> linkedin_queue.py
-> backlog.json -> the daily drip). li-search itself is not modified and does
not learn that outreach exists.

WHAT COMES IN
  li-search/results/<brief>/owners.csv     every account ever delivered for a
                                           brief, numbered, never repeated
  li-search/runs/<date>-<audience>/leads.json  the search snippet, subdomain
                                           country and provenance per account
  li-search/suppression.txt                erasure requests, honoured here too

WHAT GOES OUT   <run-dir>/people-qualified.json   the writer's + ranker's input
                <run-dir>/people-dropped.json     with a reason per person
                <run-dir>/pitch.json, channels.json, linkedin.json
                <run-dir>/handoff.json            what was taken and why

WHO IS SKIPPED, AND WHY EACH SKIP IS HERE RATHER THAN DOWNSTREAM
  * already in linkedin/state/state.json or state/backlog.json — the channel
    would reject them anyway, but only AFTER li-writer had written (and paid
    for) a message for each. Skipping first is what makes a second handoff of
    the same brief pick up the NEXT people instead of re-drafting the old.
  * `status=withdrawn` in owners.csv — the operator found the row off-spec.
  * li-search's suppression list — an erasure request must hold on both sides.
  * title outside the accepted tiers — the same T1/T2 gate qualify_people.py
    applies to the walk route, so a handoff cannot smuggle a Coordinator past
    the person gates.

WHAT THIS SCRIPT DOES NOT KNOW
Nothing that needs linkedin.com: connection degree, open-profile flag, last
activity, company headcount. Every person is therefore an `invite` with no
aliveness score, and the small-firm coordination window cannot fire because
headcount is unknown. That is honest — it is what the index gave us.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from li_url import canonical_profile_url  # noqa: E402
from qualify_people import DEFAULT_TIERS, TIER_RANK, title_tier  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
REPO = PROJECT.parent
LI_SEARCH_DEFAULT = REPO / "li-search"
PITCH_DIR = PROJECT / "templates" / "li-handoff"
FALLBACK_PITCH = PROJECT / "templates" / "gcc-outreach-li" / "pitch.json"


def _state_dir() -> Path:
    return Path(os.environ.get("LINKEDIN_STATE_DIR") or PROJECT / "linkedin" / "state")


def _norm_url(u: str | None) -> str:
    return (canonical_profile_url(u) or "").lower()


def already_in_channel(state_dir: Path) -> dict[str, str]:
    """profile url -> why the channel already holds it (status, or 'backlog')."""
    held: dict[str, str] = {}
    st = state_dir / "state.json"
    if st.exists():
        try:
            leads = (json.loads(st.read_text()) or {}).get("leads") or {}
        except Exception:
            leads = {}
            print("  WARN: state.json unreadable — cross-handoff dedup is running blind")
        for url, l in leads.items():
            k = _norm_url(l.get("profileUrl") or url)
            if k:
                held[k] = f"already {l.get('status') or 'known'} on LinkedIn"
    bl = state_dir / "backlog.json"
    if bl.exists():
        try:
            for l in json.loads(bl.read_text()) or []:
                k = _norm_url(l.get("linkedinUrl"))
                if k and k not in held:
                    held[k] = "already waiting in the backlog"
        except Exception:
            print("  WARN: backlog.json unreadable — may re-draft people already queued")
    return held


def load_suppression(li_root: Path) -> set[str]:
    f = li_root / "suppression.txt"
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        s = line.strip().lower()
        if s and not s.startswith("#"):
            out.add(s)
    return out


def latest_run_rows(li_root: Path, slug: str) -> dict[str, dict]:
    """account -> the richest row li-search holds for it (snippet, country…)."""
    runs = sorted((li_root / "runs").glob(f"*-{slug}"))
    if not runs:
        return {}
    f = runs[-1] / "leads.json"
    if not f.exists():
        return {}
    try:
        rows = (json.loads(f.read_text(encoding="utf-8")) or {}).get("leads") or []
    except Exception:
        return {}
    return {r.get("linkedin_account"): r for r in rows if r.get("linkedin_account")}


def audience_countries(li_root: Path, slug: str) -> list[str]:
    f = li_root / "audiences" / f"{slug}.json"
    if not f.exists():
        return []
    try:
        return [c.upper() for c in (json.loads(f.read_text(encoding="utf-8")).get("countries") or [])]
    except Exception:
        return []


def pool_from_brief(li_root: Path, brief: str) -> list[dict]:
    f = li_root / "results" / brief / "owners.csv"
    if not f.exists():
        sys.exit(f"ABORT: {f} missing — deliver a batch first: "
                 f"cd li-search && ./li-search export {brief} --audiences <slugs> --take 50")
    with f.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    rows.sort(key=lambda r: int(r.get("n") or 0))
    return rows


def pool_from_audience(li_root: Path, slug: str) -> list[dict]:
    rows = latest_run_rows(li_root, slug)
    out = [dict(r, audience=slug, status="") for r in rows.values() if r.get("qualified")]
    out.sort(key=lambda r: -(r.get("score") or 0))
    return out


def _num(*values) -> float:
    """First value that parses as a number, else 0.

    owners.csv is written by li-search and its column set has changed once
    already (a `strength` column landed between `location` and `match` on
    2026-09-03), which shifted every field after it in rows appended against an
    older header. A crash here reported as a stack trace what is really "that
    CSV is one column out of step" — and it aborted a handoff that had already
    done its work. A score is a ranking hint, so an unreadable one is a 0, not
    an abort."""
    for v in values:
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def title_for_tiering(row: dict) -> str:
    """The row's own title, or the title li-search matched on (`title=Founder`
    inside `match`) when the index gave a headline with no role in it."""
    t = (row.get("title") or "").strip()
    m = row.get("match") or ""
    matched = ""
    for part in m.split(";"):
        if part.startswith("title="):
            matched = part[len("title="):]
    return f"{t} {matched}".strip()


def choose_pitch(brief: str | None, explicit: str | None) -> tuple[dict, str]:
    cands = []
    if explicit:
        cands.append(Path(explicit))
    if brief:
        cands.append(PITCH_DIR / f"{brief}.pitch.json")
    cands.append(FALLBACK_PITCH)
    for c in cands:
        if c.exists():
            try:
                return json.loads(c.read_text(encoding="utf-8")), str(c)
            except Exception as e:  # noqa: BLE001
                sys.exit(f"ABORT: pitch file {c} is not valid JSON: {e}")
    return {}, "(none)"


def build(pool: list[dict], *, li_root: Path, held: dict[str, str], suppressed: set[str],
          tiers: dict, accept: set[str], take: int, brief: str | None) -> tuple[list[dict], list[dict]]:
    by_slug_rows: dict[str, dict[str, dict]] = {}
    by_slug_cc: dict[str, list[str]] = {}
    taken, dropped, seen = [], [], set()
    for row in pool:
        acct = (row.get("linkedin_account") or "").strip().lower()
        slug = row.get("audience") or ""
        if slug not in by_slug_rows:
            by_slug_rows[slug] = latest_run_rows(li_root, slug)
            by_slug_cc[slug] = audience_countries(li_root, slug)
        rich = by_slug_rows[slug].get(acct) or {}
        url = canonical_profile_url(row.get("linkedin_url") or rich.get("linkedin_url"))

        def drop(reason: str) -> None:
            dropped.append({"linkedin_account": acct, "full_name": row.get("full_name"),
                            "audience": slug, "drop_reason": reason})

        if not acct or not url:
            drop("no usable profile URL"); continue
        if url.lower() in seen:
            drop("duplicate account in the pool"); continue
        seen.add(url.lower())
        if (row.get("status") or "").strip().lower() == "withdrawn":
            drop("withdrawn by the operator in owners.csv"); continue
        if acct in suppressed or url.lower().rstrip("/") in suppressed:
            drop("on li-search's suppression list"); continue
        if url.lower() in held:
            drop(held[url.lower()]); continue
        tier = title_tier(title_for_tiering({**rich, **{k: v for k, v in row.items() if v}}), tiers)
        if tier not in accept:
            drop(f"title tier {tier or 'none'} not in {sorted(accept)}"); continue
        if len(taken) >= take:
            drop("over --take for this handoff (next handoff picks it up)"); continue

        cc_audience = by_slug_cc[slug]
        cc_row = (rich.get("country") or "").upper()
        company_country = cc_row if cc_row and (not cc_audience or cc_row in cc_audience) \
            else (cc_audience[0] if cc_audience else cc_row)
        taken.append({
            "lead_id": acct,
            "full_name": row.get("full_name") or rich.get("full_name"),
            "title": row.get("title") or rich.get("title"),
            "company": row.get("company") or rich.get("company"),
            "profile_url": url,
            "email": rich.get("email") or None,
            # Geo is the AUDIENCE's, by construction of the search (country
            # subdomain in the query). The profile's own location is carried for
            # the writer, never used as a gate — the diaspora trap.
            "company_country": company_country or None,
            "profile_country": cc_row or None,
            "profile_location": row.get("location") or rich.get("location") or None,
            "company_city": None, "company_domain": None, "company_headcount": None,
            "title_tier": tier, "tier_rank": TIER_RANK[tier],
            "reachable": True, "reach_mode": "invite", "degree": None, "open_profile": False,
            "alive": None, "last_activity_days": None,
            # The one hook the index gives us: the search-result snippet.
            "snippet": (rich.get("summary") or "").strip() or None,
            "match": row.get("match") or rich.get("match"),
            "score_lisearch": _num(row.get("score"), rich.get("score")),
            "sources": rich.get("sources") or [s for s in (row.get("sources") or "").split("+") if s],
            "eu_flag": bool(rich.get("eu_flag")),
            "audience": slug, "brief": brief, "origin": "li-search", "route": "linkedin",
        })
    return taken, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--brief", help="li-search/results/<brief>/owners.csv is the pool")
    src.add_argument("--audience", help="li-search/runs/<latest>-<slug>/leads.json is the pool")
    ap.add_argument("--li-search-root", default=str(LI_SEARCH_DEFAULT))
    ap.add_argument("--run-dir", help="default: project/runs/<today>-li-<brief|audience>")
    ap.add_argument("--take", type=int, default=60,
                    help="people per handoff. ~60 is three to four weeks of invite supply "
                         "at the 8->18/day ramp; taking more only makes li-writer draft "
                         "messages that will sit unsent for months.")
    ap.add_argument("--pitch", help="pitch.json giving li-writer the angle; default "
                                    "templates/li-handoff/<brief>.pitch.json, then gcc-outreach-li's")
    ap.add_argument("--config", default="{}", help="linkedin.json-style overrides "
                                                   "(accept_tiers, title_tiers, countries)")
    a = ap.parse_args()

    li_root = Path(a.li_search_root)
    if not li_root.is_dir():
        sys.exit(f"ABORT: li-search root {li_root} not found")
    cfg = json.loads(a.config or "{}")
    tiers = {k: list(DEFAULT_TIERS.get(k, [])) + list((cfg.get("title_tiers") or {}).get(k, []))
             for k in ("T1", "T2", "T3")}
    accept = set(cfg.get("accept_tiers") or ["T1", "T2"])

    label = a.brief or a.audience
    run = Path(a.run_dir) if a.run_dir else PROJECT / "runs" / f"{date.today().isoformat()}-li-{label}"
    if not a.run_dir:
        k, base = 1, run
        while run.exists():
            run = base.with_name(f"{base.name}-{k}"); k += 1
    run.mkdir(parents=True, exist_ok=True)

    pool = pool_from_brief(li_root, a.brief) if a.brief else pool_from_audience(li_root, a.audience)
    held = already_in_channel(_state_dir())
    suppressed = load_suppression(li_root)
    taken, dropped = build(pool, li_root=li_root, held=held, suppressed=suppressed, tiers=tiers,
                           accept=accept, take=a.take, brief=a.brief)

    pitch, pitch_src = choose_pitch(a.brief, a.pitch)
    countries = sorted({p["company_country"] for p in taken if p.get("company_country")})
    (run / "people-qualified.json").write_text(json.dumps(taken, ensure_ascii=False, indent=2))
    (run / "people-dropped.json").write_text(json.dumps(dropped, ensure_ascii=False, indent=2))
    (run / "pitch.json").write_text(json.dumps(pitch, ensure_ascii=False, indent=2))
    (run / "channels.json").write_text(json.dumps(["linkedin"]))
    (run / "linkedin.json").write_text(json.dumps({**cfg, "countries": cfg.get("countries") or countries},
                                                  ensure_ascii=False, indent=2))
    reasons: dict[str, int] = {}
    for d in dropped:
        r = d["drop_reason"].split(" (")[0]
        reasons[r] = reasons.get(r, 0) + 1
    (run / "handoff.json").write_text(json.dumps({
        "brief": a.brief, "audience": a.audience, "li_search_root": str(li_root),
        "pool": len(pool), "taken": len(taken), "dropped": reasons, "pitch": pitch_src,
        "accounts": [p["lead_id"] for p in taken], "at": date.today().isoformat(),
    }, ensure_ascii=False, indent=2))

    print(f"li-search handoff: {len(pool)} in pool -> {len(taken)} people -> {run}")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  skipped[{r}]: {n}")
    print(f"  pitch: {pitch_src}")
    if not taken:
        print("  nothing new to hand over. Fire or export more from li-search, or raise --take.")
        sys.exit(7)
    print(f"  next: draft_linkedin.py --phase batch --run-dir {run}  (then li-writer, merge, linkedin_queue)")


if __name__ == "__main__":
    main()
