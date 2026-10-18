"""The setter's stage machine. The model PROPOSES the next stage; this code decides what is allowed.

Funnel stages move forward one step at a time (the lead can pull it forward when they ask how to work with the
coach); the side exits (NURTURE, DISQUALIFIED) are open from anywhere. BOOKED is set only by the booking
webhook, HANDOFF only by the engine.
"""

from __future__ import annotations

FUNNEL = ["OPENER", "DISCOVERY", "GAP", "QUALIFY", "CALL_PITCH", "BOOKING"]
SIDE = ["NURTURE", "DISQUALIFIED"]
CODE_ONLY = ["BOOKED", "HANDOFF"]
STAGES = FUNNEL + SIDE + CODE_ONLY
MODEL_STAGES = FUNNEL + SIDE                    # what the reply model may answer with
NO_NUDGE = {"BOOKED", "DISQUALIFIED", "NURTURE", "HANDOFF"}


def required_known(coach_slots: list[dict], known: dict) -> bool:
    return all(str(known.get(s["slot"], "")).strip() for s in coach_slots if s.get("required"))


def next_stage(current: str, proposed: str, *, qualified: bool, hot: bool) -> str:
    """The stage after this turn. `qualified` = every required slot is known; `hot` = the lead wants in now."""
    if proposed not in MODEL_STAGES:
        return current
    if current in CODE_ONLY and current != "HANDOFF":
        return current                          # BOOKED stays booked until the booking webhook says otherwise
    if proposed in SIDE:
        return proposed
    cur = FUNNEL.index(current) if current in FUNNEL else 0
    new = FUNNEL.index(proposed)
    if new >= FUNNEL.index("CALL_PITCH") and not (qualified or hot):
        new = min(new, FUNNEL.index("QUALIFY"))  # no pitch before the required facts are known
    if new > cur + 1 and not hot and proposed not in ("CALL_PITCH", "BOOKING"):
        new = cur + 1                           # one step at a time
    return FUNNEL[max(0, new)]


def link_allowed(stage: str, *, qualified: bool, hot: bool, links_sent: int, max_links: int) -> bool:
    return stage in ("CALL_PITCH", "BOOKING") and (qualified or hot) and links_sent < max_links
