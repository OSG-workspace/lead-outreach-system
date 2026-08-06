#!/usr/bin/env python3
"""Stage 5.5-LI: rank qualified people and hand them to project/linkedin.

WHY A RANKED LIST AND NOT A SEND LIST
Email sends everything that verifies. LinkedIn spends scarcity, so order
matters more than volume: a run that qualifies 400 people will send perhaps 18
invites that day, and which 18 is the entire game.

THIS SCRIPT DOES NOT SEND, AND DOES NOT CAP.
`project/linkedin` is the action layer and owns every limit: profile-URL dedup,
cross-channel person suppression, the daily ramp (8 -> 18/day, absolute max 20,
weeklyMax 90 in config/limits.json), pacing, the near-duplicate-note check, and
the circuit breakers. Re-implementing any of that here would create a second,
conflicting source of truth. This script adds only what that module does NOT
do — the person gates' score, hook strength, COMPANY-level blocking, and the
small-firm coordination window — then writes its documented input contract.

NOTE the real ceiling is ~90 invites/week, not the ~150 a generic LinkedIn
playbook assumes; the ramp starts at 8/day for a new sending account.

SCORE = title tier x aliveness x reachability x hook strength
  tier          T1 3 / T2 2 / T3 1
  aliveness     fresher activity scores higher (<=14d best, decays to the gate)
  reachability  1st-degree > open profile > invite > InMail
  hook strength number of harvested personalization hooks (shared school,
                mutuals, recent post, company timing signal). In Lebanon the
                AUB/LAU/USJ match is the strongest accept-rate lever there is,
                so it is weighted hardest.

SUPPRESSION (applied BEFORE budget allocation)
  * company-level negative: the company already replied "not interested" or is
    in the disqualified ledger -> never invite its people.
  * already-contacted company: honours the email chain's sent-log so the two
    channels do not double-touch the same business.
  * SMALL-FIRM COORDINATION: at or below `small_firm_headcount` (default 6),
    at most ONE person per company per `coordination_window_days`. Two invites
    into a 6-person firm in the same week reads as coordinated automation,
    which is exactly how a domain gets burned.
  * invite ledger: never re-invite someone already invited.

OUTPUT  <run-dir>/linkedin-leads.json   input contract for linkedin/queue/generate.js
        <run-dir>/invite-carry.json    held back (small-firm window / pre-rank cap)
        vault/lead-outreach/linkedin-invites.txt   append-only invite ledger
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
# Same override the Node side honours (linkedin/lib/paths.js), so a test can run
# the real ranking without writing into the live backlog the drip is draining.
LI_STATE_DIR = Path(os.environ.get("LINKEDIN_STATE_DIR")
                    or PROJECT / "linkedin" / "state")
VAULT = PROJECT / "vault" / "lead-outreach"
INVITE_LEDGER = VAULT / "linkedin-invites.txt"     # profile_url|company|iso|YYYY-MM-DD|run
SENT_LOG = VAULT / "sent-log.md"
DISQUALIFIED = VAULT / "disqualified-log.txt"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _company_keys(*values: str) -> set[str]:
    """Every normalised handle one company answers to (domain and/or name).

    Two sides of this system name the same company differently — the ranker has
    the OSM domain, the sender's state.json only kept the display name — so a
    single "preferred" key silently never matched across runs.
    """
    out: set[str] = set()
    for v in values:
        if isinstance(v, dict):                       # a person record
            v = [v.get("company_domain"), v.get("company")]
        for one in (v if isinstance(v, list) else [v]):
            k = _norm(one)
            if k:
                out.add(k)
    return out


LI_STATE = LI_STATE_DIR / "state.json"

# Statuses that mean "an invite was actually spent on this person". `attempting`
# and `unknown` count deliberately: the click may have landed, and the whole
# point of the coordination window is to avoid a SECOND touch at the same firm.
SPENT = {"attempting", "sent", "accepted", "expired", "withdrawn", "unknown"}


def load_ledger() -> tuple[set[str], dict[str, date]]:
    """-> (profile urls already invited, company -> most recent invite date)

    SOURCED FROM linkedin/state/state.json, NOT from a text ledger.

    `vault/lead-outreach/linkedin-invites.txt` is only ever appended by this
    script's `--commit` flag, and nothing passes it — run_fire deliberately does
    not, because a fire QUEUES and the invite may not be sent for weeks, so
    committing at fire time would record invites that never happened. The result
    was a ledger that was read on every run and written on none: it always came
    back empty, and the small-firm coordination window — "at most one person per
    small company per 14 days" — silently did nothing across runs. Two fires a
    week apart would both queue a person at the same six-person brokerage, which
    the module's own docs call exactly how a domain gets burned.

    state.json is the honest source: it records what the sender actually spent,
    at the moment it spent it. The text ledger is still read if present, so a
    hand-maintained one keeps working.
    """
    seen: set[str] = set()
    per_company: dict[str, date] = {}

    def note(url: str, *companies: str, when: date | None = None) -> None:
        if url:
            seen.add(url.lower().rstrip("/"))
            seen.add(url.lower().rstrip("/") + "/")
        if not when:
            return
        for key in _company_keys(list(companies)):
            if key not in per_company or when > per_company[key]:
                per_company[key] = when

    if LI_STATE.exists():
        try:
            leads = (json.loads(LI_STATE.read_text()) or {}).get("leads") or {}
        except Exception:
            leads = {}
            print("  WARN: linkedin/state/state.json unreadable — cross-run invite "
                  "dedup and the small-firm window are running blind this fire.")
        for url, l in leads.items():
            if (l.get("status") or "") not in SPENT:
                continue
            stamp = l.get("sentAt") or l.get("attemptStartedAt") or l.get("updatedAt") or ""
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
            except ValueError:
                when = None
            note(l.get("profileUrl") or url,
                 l.get("company") or "", l.get("companyDomain") or "", when=when)

    if INVITE_LEDGER.exists():
        for line in INVITE_LEDGER.read_text().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                continue
            try:
                d = datetime.strptime(parts[3], "%Y-%m-%d").date()
            except ValueError:
                d = None
            note(parts[0], parts[1], when=d)

    return seen, per_company


def blocked_companies() -> set[str]:
    """Companies the email chain has already burned or ruled out."""
    out = set()
    if DISQUALIFIED.exists():
        for line in DISQUALIFIED.read_text().splitlines():
            d = line.split("|", 1)[0].strip().lower()
            if d:
                out.add(_norm(d))
    if SENT_LOG.exists():
        txt = SENT_LOG.read_text()
        for m in re.finditer(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", txt):
            out.add(_norm(m.group(1)))
    return out


def hook_strength(p: dict, cfg: dict) -> tuple[float, list[str]]:
    hooks, score = [], 0.0
    schools = {s.lower() for s in (cfg.get("school_boost") or [])}
    if schools and any(s in (p.get("school") or "").lower() for s in schools):
        score += 2.0; hooks.append("shared-school")      # strongest LB lever
    if (p.get("mutual_connections") or 0) >= 3:
        score += 1.0; hooks.append("mutuals")
    if p.get("recent_posts"):
        score += 1.0; hooks.append("recent-post")
    if p.get("company_signal"):
        score += 1.5; hooks.append(f"timing:{p['company_signal']}")
    return score, hooks


def alive_score(days: int | None, gate: int) -> float:
    if days is None:
        return 0.0
    if days <= 14:
        return 3.0
    if days <= 30:
        return 2.0
    return 1.0 if days <= gate else 0.0


REACH_SCORE = {"1st-degree (message directly, no invite needed)": 3.0,
               "open profile": 2.0, "invite": 1.5, "InMail": 1.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config", default="{}")
    ap.add_argument("--budget", type=int, default=0,
                    help="optional pre-trim of the ranked list. The REAL cap is the "
                         "daily ramp in linkedin/config/limits.json, enforced by "
                         "sender.js; leave at 0 to hand over the full ranked list.")
    ap.add_argument("--commit", action="store_true", help="write the invite ledger")
    a = ap.parse_args()
    run = Path(a.run_dir)
    cfg = json.loads(a.config or "{}")
    budget = a.budget or int(cfg.get("prerank_cap", 0)) or 10**9
    small_hc = int(cfg.get("small_firm_headcount", 6))
    window = int(cfg.get("coordination_window_days", 14))
    gate = int(cfg.get("alive_days", 90))

    # Read the people who HAVE a composed message. people-qualified.json is the
    # pre-writer list and carries no message at all, so reading it here silently
    # dropped every DM li-writer produced and handed the queue a list the sender
    # could only reject. people-with-notes.json is the same records plus the
    # `message` field, written by draft_linkedin.py --phase merge.
    src = run / "people-with-notes.json"
    if not src.exists():
        src = run / "people-qualified.json"
        if not src.exists():
            sys.exit("ABORT: people-with-notes.json missing (run draft_linkedin.py --phase merge first)")
        print("  WARN: people-with-notes.json missing; falling back to the pre-writer "
              "list. No messages will be attached and the queue will reject these.")
    people = json.loads(src.read_text())

    invited, company_last = load_ledger()
    blocked = blocked_companies()
    today = date.today()

    ranked, suppressed = [], []
    for p in people:
        # A company is identified by its domain AND its name, not one or the
        # other. The ranking side prefers the domain; the sender's state.json
        # only ever recorded the NAME, so keying on a single preferred value made
        # the two sides look up different strings and the cross-run small-firm
        # window never matched anything. Carry both, match on either.
        ckeys = _company_keys(p)
        if (p.get("profile_url") or "").lower() in invited:
            suppressed.append({**p, "suppress": "already invited"}); continue
        if ckeys & blocked:
            suppressed.append({**p, "suppress": "company already contacted or disqualified"}); continue
        hs, hooks = hook_strength(p, cfg)
        score = (p.get("tier_rank", 1)
                 + alive_score(p.get("last_activity_days"), gate)
                 + REACH_SCORE.get(p.get("reach_mode", ""), 1.0)
                 + hs)
        ranked.append({**p, "score": round(score, 2), "hooks": hooks,
                       "_ckeys": sorted(ckeys)})

    ranked.sort(key=lambda x: -x["score"])

    # Small-firm coordination: one person per small company per window.
    queue, carry, used_company = [], [], set()
    for p in ranked:
        cks, hc = set(p["_ckeys"]), p.get("company_headcount")
        small = hc is not None and hc <= small_hc
        if small and cks:
            last = max((company_last[k] for k in cks if k in company_last),
                       default=None)
            if (cks & used_company) or (last and (today - last).days < window):
                carry.append({**p, "carry_reason":
                              f"small firm (hc={hc}): one invite per {window}d"
                              + (f" (last invited {last})" if last else " (this run)")})
                continue
        if len(queue) >= budget:
            carry.append({**p, "carry_reason": "over pre-rank cap"})
            continue
        if small and cks:
            used_company |= cks
        queue.append(p)

    for x in queue + carry:
        x.pop("_ckeys", None)

    # HAND OFF to project/linkedin — do NOT re-implement what it already owns.
    # That module is the action layer and enforces every limit itself: profile-URL
    # dedup, cross-channel person suppression, the daily ramp cap, pacing, the
    # near-duplicate-note check, and the circuit breakers. This script only adds
    # what it does NOT do — the person gates' score, hook strength, COMPANY-level
    # blocking, and the small-firm coordination window — then writes its input
    # contract:  node linkedin/queue/generate.js --leads-file <this file>
    leads = []
    for p in queue:
        leads.append({
            "leadId": p.get("lead_id") or p.get("profile_url"),
            "name": p.get("full_name"),
            "company": p.get("company"),
            # Carried so the SENDER can persist it: the cross-run small-firm
            # window matches on either handle, and state.json used to keep
            # only the display name.
            "companyDomain": p.get("company_domain"),
            "title": p.get("title"),
            "email": p.get("email"),
            "score": p["score"],
            "gap": p.get("gap") or (p.get("hooks") or [None])[0],
            "linkedinUrl": p.get("profile_url"),
            # THE DM li-writer composed for this person. It is carried on the
            # INVITE so the module can store it and send it if and when the
            # invite is accepted, days or weeks later — see queue/generate.js.
            **({"message": p["message"]} if p.get("message") else {}),
            "degree": p.get("degree"),
            "openProfile": bool(p.get("open_profile")),
            # An Open Profile member or an existing connection needs no invite at
            # all, so the two-phase wait does not apply to them.
            "directMessageable": bool(p.get("open_profile")) or p.get("degree") == 1,
            # generate.js REJECTS a lead whose note is missing or templated, so
            # compose must have run first. Left absent on purpose if it has not:
            # a loud rejection there beats a templated invite going out.
            **({"note": p["note"]} if p.get("note") else {}),
        })
    (run / "linkedin-leads.json").write_text(json.dumps(leads, ensure_ascii=False, indent=2))
    (run / "invite-carry.json").write_text(json.dumps(carry, ensure_ascii=False, indent=2))

    # THE BACKLOG, and why it lives in the module rather than the run folder.
    # A fire ranks perhaps 60 people; the invite ramp spends 8-18 a day, so the
    # list takes weeks to drain and outlives the run that produced it. Keeping
    # the drip's input inside project/linkedin means the daily command is always
    # the same one and never has to name a run folder — and a second fire simply
    # merges into the same backlog instead of starting a competing queue.
    backlog_file = LI_STATE_DIR / "backlog.json"
    backlog_file.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if backlog_file.exists():
        try:
            existing = json.loads(backlog_file.read_text())
        except Exception:
            print("  WARN: backlog.json unreadable; starting a fresh backlog")
    # Prune people the sender has already spent an invite on. generate.js skips
    # them at queue time anyway, so leaving them here changes no behaviour — but
    # "N total waiting" is the number the operator reads to judge how many weeks
    # of supply is left, and an unpruned backlog inflates it forever.
    def spent(l: dict) -> bool:
        u = (l.get("linkedinUrl") or "").lower().rstrip("/")
        return bool(u) and (u in invited or u + "/" in invited)

    by_url = {l["linkedinUrl"]: l for l in existing
              if l.get("linkedinUrl") and not spent(l)}
    pruned = len(existing) - len(by_url)
    added = 0
    for l in leads:
        if l.get("linkedinUrl") and l["linkedinUrl"] not in by_url:
            by_url[l["linkedinUrl"]] = l
            added += 1
    merged = sorted(by_url.values(), key=lambda l: -float(l.get("score") or 0))
    backlog_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    print(f"  backlog: +{added} new, {pruned} already invited and dropped, "
          f"{len(merged)} total waiting -> {backlog_file}")

    if a.commit and queue:
        VAULT.mkdir(parents=True, exist_ok=True)
        with INVITE_LEDGER.open("a") as f:
            for p in queue:
                f.write(f"{p.get('profile_url','')}|{p.get('company','')}|"
                        f"{p.get('company_country','')}|{today.isoformat()}|{run.name}\n")

    missing_msg = sum(1 for l in leads if "message" not in l)
    direct = sum(1 for l in leads if l.get("directMessageable"))
    print(f"linkedin-leads.json: {len(leads)} ranked | carried {len(carry)} | "
          f"suppressed {len(suppressed)}")
    if queue:
        print(f"  top score {queue[0]['score']} — {queue[0].get('full_name','?')} "
              f"({queue[0].get('title','?')}) hooks={queue[0]['hooks']}")
    print(f"  {direct} directly messageable now; {len(leads) - direct} need an "
          f"invite accepted first")
    if missing_msg:
        print(f"  NOTE: {missing_msg} leads have no composed message — "
              f"the queue will reject those until li-writer runs.")
    print("  next: node linkedin/queue/generate.js --leads-file "
          f"{(run / 'linkedin-leads.json')}")


if __name__ == "__main__":
    main()
