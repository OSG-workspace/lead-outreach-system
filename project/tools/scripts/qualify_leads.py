#!/usr/bin/env python3
"""Stage 5.3 — qualify + cap, BEFORE the expensive per-lead fan-outs.

Why this stage exists
---------------------
The pipeline used to dispatch one web-research Haiku agent (name-finder, then
gap-writer) for EVERY extracted lead, then drop 40-80% of them at the contact /
gap gate. We paid for the most expensive work on leads we were about to throw
away. One real run extracted 464 leads and dispatched 464 name-finder agents
for 0 sends.

This gate runs with ZERO agents (pure data already on each lead from extract):
  1. Disqualifies weak leads (freemail-only, below score floor, dead signals).
  2. Ranks the rest by fit (score, then person>role email, then chain size).
  3. Caps to the top-N so a pathological extraction can never fan out into
     thousands of agents. N = ENRICH_MAX_LEADS env (default 250 — a SAFETY
     CEILING, not a throttle: it is set high enough to enrich every genuinely
     qualified lead in a normal run, so we outreach as many as qualify. Raise it
     (env or `<run>/enrich_cap.txt`) for very large sweeps; it exists only to
     stop a runaway 1000-lead extraction from dispatching 1000 agents).

Output: leads-qualified.json (same JSONL schema as leads-extracted.json).
Stage 5.5 (enrich) and Stage 6 (draft) read THIS file, so the cap propagates
through every downstream fan-out.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--run-dir", required=True)
p.add_argument("--cap", type=int, default=int(os.environ.get("ENRICH_MAX_LEADS", "250")),
               help="max qualified leads to carry into enrichment (top-N by fit). "
                    "Safety ceiling, default 250 — high enough to enrich every "
                    "qualified lead in a normal run; raise for huge sweeps.")
p.add_argument("--min-score", type=int, default=int(os.environ.get("QUALIFY_MIN_SCORE", "82")),
               help="drop leads below this extract score")
# On a LinkedIn run the outreach surface is a person's profile, so every gate in
# this file that judges a MAILBOX is measuring the wrong thing. Two consequences,
# both off by default so email campaigns are untouched:
#   1. freemail-only / no-email leads are kept (LinkedIn never uses the address).
#   2. NOTHING is written to the permanent disqualified ledger for an email
#      reason. Without this a LinkedIn run would blacklist a company from every
#      FUTURE email campaign purely for not publishing an address — a permanent
#      loss caused by a channel that never needed the address.
p.add_argument("--linkedin-run", action="store_true",
               help="exempt email-quality gates and never blacklist for email reasons")
args = p.parse_args()

ROOT = Path(args.run_dir).resolve()
EXTRACTED = ROOT / "leads-extracted.json"
OUT = ROOT / "leads-qualified.json"
QUALIFY_CFG = ROOT / "qualify.json"

# Signals that mean the gap is already solved or not worth a custom email.
DEAD_SIGNALS = {"modern_booking"}
# Email-class fit: a named person's mailbox is worth enriching; a freemail
# (gmail/yahoo) almost never resolves to a direct decision-maker address.
CLASS_RANK = {"person": 0, "role": 1, "personal": 2}

# Optional per-run gate: require a BIG / high-call-volume hotel. Activated by a
# run-local qualify.json (so only runs that opt in are affected):
#   {"require_hotel_size_volume": true, "min_hotel_volume": "medium"}
HOTEL_VERTICALS = {"hotel", "hotels", "resort", "resorts", "bnb", "guesthouse", "guesthouses"}
VOLUME_RANK = {"high": 2, "medium": 1, "low": 0}

# Traffic / demand tier, produced for EVERY vertical by extract_leads.py.
# `unknown` sits ABOVE `low` deliberately: it means "we could not measure this
# site", which is our fetch failing, not the business being quiet. A lead we
# failed to measure must not be punished as though we had measured it.
TRAFFIC_RANK = {"high": 3, "medium": 2, "unknown": 1, "low": 0}


def _traffic_tier(lead: dict) -> str:
    """Missing field (lead from an older extract) reads as `unknown`, never `low`."""
    return str(lead.get("traffic_tier") or "unknown").lower()


def load(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def main() -> None:
    if not EXTRACTED.exists():
        raise SystemExit("ABORT: leads-extracted.json missing (run Stage 5 first).")
    leads = load(EXTRACTED)
    total = len(leads)

    # Optional hotel size/volume gate.
    cfg = {}
    if QUALIFY_CFG.exists():
        try:
            cfg = json.loads(QUALIFY_CFG.read_text())
        except Exception:
            cfg = {}
    require_hotel_volume = bool(cfg.get("require_hotel_size_volume"))
    min_volume = VOLUME_RANK.get(str(cfg.get("min_hotel_volume", "medium")).lower(), 1)

    # Optional per-campaign traffic floor. ABSENT KEY = NO FLOOR, so the 20
    # fixtures with no qualify.json are untouched. An INVALID value also means
    # no floor: a typo must never silently start dropping leads.
    raw_floor = str(cfg.get("min_traffic_tier", "")).lower().strip()
    min_traffic = TRAFFIC_RANK.get(raw_floor) if raw_floor in TRAFFIC_RANK else None
    if raw_floor and min_traffic is None:
        print(f"  WARNING: ignoring invalid min_traffic_tier={raw_floor!r} "
              f"(expected one of high/medium/low) — no traffic floor applied")
    if min_traffic is not None and raw_floor == "unknown":
        print("  WARNING: min_traffic_tier='unknown' is meaningless — no floor applied")
        min_traffic = None

    dropped_freemail = dropped_score = dropped_signal = dropped_small_hotel = 0
    dropped_traffic = 0
    kept: list[dict] = []
    disqualified: list[tuple[str, str]] = []   # (domain, reason) -> permanent block

    def _domain(lead: dict) -> str:
        d = (lead.get("domain") or "").strip().lower()
        if d:
            return d
        site = (lead.get("website") or "").strip().lower()
        return site.split("//")[-1].split("/")[0].removeprefix("www.")

    for l in leads:
        # A LinkedIn lead is judged by the person gates (qualify_people.py), not
        # by its mailbox. Skipping the email-quality gates here is what keeps the
        # audience alive; the LinkedIn person gates are strictly harder, not softer.
        email_exempt = args.linkedin_run and l.get("email_class") in ("personal", "none")
        if l.get("email_class") == "personal" and not email_exempt:
            dropped_freemail += 1
            disqualified.append((_domain(l), "freemail-only"))
            continue
        if l.get("signal") in DEAD_SIGNALS:
            dropped_signal += 1
            disqualified.append((_domain(l), f"dead-signal:{l.get('signal')}"))
            continue
        if int(l.get("score", 0)) < args.min_score and not email_exempt:
            dropped_score += 1
            disqualified.append((_domain(l), f"score<{args.min_score}"))
            continue
        # Big / high-call-volume hotel gate: drop properties with no scale signal.
        if require_hotel_volume and l.get("vertical", "").lower() in HOTEL_VERTICALS:
            tier = l.get("hotel_volume", "low")
            if VOLUME_RANK.get(tier, 0) < min_volume:
                dropped_small_hotel += 1
                disqualified.append((_domain(l), "hotel-volume-too-low"))
                continue
        # Traffic floor. Deliberately does NOT append to `disqualified`: that
        # ledger blocks a domain permanently on every future run and every
        # channel, and a traffic verdict depends on how well we crawled the site
        # TODAY. `unknown` is exempt — we never punish a site we failed to measure.
        if min_traffic is not None:
            t = _traffic_tier(l)
            if t != "unknown" and TRAFFIC_RANK.get(t, 0) < min_traffic:
                dropped_traffic += 1
                continue
        kept.append(l)

    # PROVEN-UNQUALIFIED ledger (user directive 2026-07-27). Only a lead this gate
    # actually judged and rejected is blocked from future runs. Merely having been
    # *sourced* is not proof of anything, and a lead that QUALIFIES must stay
    # reachable until it is genuinely contacted — so qualified-but-not-yet-contacted
    # domains are deliberately NOT written here.
    # On a LinkedIn run, an email-quality verdict is not evidence about the
    # business, so it must never reach the PERMANENT ledger — that ledger blocks
    # a domain from every future run on ANY channel. Only judgements about the
    # business itself (dead signal, too small) are proof and survive.
    if args.linkedin_run:
        kept_reasons = [(d, r) for d, r in disqualified
                        if r.startswith("dead-signal") or r.startswith("hotel-volume")]
        if len(kept_reasons) != len(disqualified):
            print(f"  linkedin-run: {len(disqualified) - len(kept_reasons)} email-quality "
                  f"verdicts NOT written to the permanent disqualified ledger")
        disqualified = kept_reasons

    if disqualified:
        ledger = Path("vault/lead-outreach/disqualified-log.txt")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        from datetime import date
        today = date.today().isoformat()
        run_slug = ROOT.name
        seen_before = set()
        if ledger.exists():
            for line in ledger.read_text().splitlines():
                part = line.split("|", 1)[0].strip().lower()
                if part:
                    seen_before.add(part)
        with ledger.open("a") as f:
            for dom, reason in disqualified:
                if dom and dom not in seen_before:
                    seen_before.add(dom)
                    f.write(f"{dom}|{today}|{run_slug}|{reason}\n")

    # Rank by fit. Traffic is a TIE-BREAK, deliberately placed BELOW the two
    # email-quality keys (score, then person>role class).
    #
    # Calibrated 2026-08-10 over 990 domains: the tier itself discriminates well
    # as a busyness measure (21.1% high / 34.6% medium / 44.2% low). What could
    # NOT be validated is whether busy businesses are easier or HARDER to reach:
    # only one run on disk carries both raw_html/ and Stage 5.5 outcomes
    # (2026-07-27-lb-ngos-1, n=37, NGOs only, high tier n=1), which proves
    # nothing either way. The plausible adverse mechanism is that larger orgs
    # publish only role inboxes (info@, reception@), and email-availability
    # failures are ALREADY 77% of all enrichment drops. Leading the sort with
    # traffic could therefore lower send yield while picking better prospects.
    # So traffic only reorders leads of EQUAL email quality: upside where it is
    # free, no ability to push a hard-to-reach lead ahead of a reachable one.
    # Revisit once a non-NGO run exists with both artifacts.
    kept.sort(key=lambda l: (
        -VOLUME_RANK.get(l.get("hotel_volume", "na"), -1) if l.get("hotel_volume", "na") != "na" else 0,
        -int(l.get("score", 0)),
        CLASS_RANK.get(l.get("email_class"), 9),
        -TRAFFIC_RANK.get(_traffic_tier(l), 1),
        -int(l.get("traffic_score", 0)),
        -int(l.get("branches_estimate", 0)),
    ))

    capped = kept[: args.cap]
    over_cap = len(kept) - len(capped)

    OUT.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in capped) + "\n")

    print(f"Qualify + cap: {total} extracted -> {len(capped)} carried into enrichment")
    print(f"  dropped freemail-only:   {dropped_freemail}")
    print(f"  dropped below score {args.min_score}: {dropped_score}")
    print(f"  dropped dead signal:     {dropped_signal}")
    if require_hotel_volume:
        print(f"  dropped small/low-volume hotel: {dropped_small_hotel} "
              f"(need >= {cfg.get('min_hotel_volume','medium')} size/volume signal)")
    if min_traffic is not None:
        print(f"  dropped low-traffic:     {dropped_traffic} "
              f"(need >= {raw_floor} traffic tier; not ledgered — this run only)")
    print(f"  qualified before cap:    {len(kept)}")
    if over_cap > 0:
        print(f"  CAPPED OFF (top-{args.cap} kept): {over_cap} qualified leads held back "
              f"this run (raise ENRICH_MAX_LEADS to widen).")
    print(f"Wrote {OUT}")
    if not capped:
        print("ABORT: 0 leads survived the qualify gate "
              "(lower QUALIFY_MIN_SCORE or check extraction).")
        raise SystemExit(7)


if __name__ == "__main__":
    main()
