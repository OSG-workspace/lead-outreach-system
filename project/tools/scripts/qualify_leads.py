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

    dropped_freemail = dropped_score = dropped_signal = dropped_small_hotel = 0
    kept: list[dict] = []
    for l in leads:
        if l.get("email_class") == "personal":
            dropped_freemail += 1
            continue
        if l.get("signal") in DEAD_SIGNALS:
            dropped_signal += 1
            continue
        if int(l.get("score", 0)) < args.min_score:
            dropped_score += 1
            continue
        # Big / high-call-volume hotel gate: drop properties with no scale signal.
        if require_hotel_volume and l.get("vertical", "").lower() in HOTEL_VERTICALS:
            tier = l.get("hotel_volume", "low")
            if VOLUME_RANK.get(tier, 0) < min_volume:
                dropped_small_hotel += 1
                continue
        kept.append(l)

    # Rank by fit: bigger/high-volume hotels first, then score, then
    # person-before-role, then bigger chains first.
    kept.sort(key=lambda l: (
        -VOLUME_RANK.get(l.get("hotel_volume", "na"), -1) if l.get("hotel_volume", "na") != "na" else 0,
        -int(l.get("score", 0)),
        CLASS_RANK.get(l.get("email_class"), 9),
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
