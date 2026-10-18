"""Guardrail gates around the reply model. Code patterns always run; Jev adds a second, independent read.

Harness rules (from the Jev playbook): code owns the thresholds; Jev may only make a turn stricter; any Jev failure
falls back to the patterns plus the reply model's own handoff flag; every answer is cached and trailed.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from .coach import Coach
from .config import ROOT
from .jev_client import Jev, mode, trail
from .questions import (HANDOFF_ORDER, INBOUND_BARS, INBOUND_QUESTIONS, OUTBOUND_BARS, OUTBOUND_QUESTIONS,
                        RX_IN, RX_OUT)

TRAIL = ROOT / "data" / "jev-trail.jsonl"
URL = re.compile(r"https?://\S+|www\.\S+", re.I)


@dataclass
class Inbound:
    hits: dict = field(default_factory=dict)       # question -> "rule" | "jev" | "rule+jev"
    p: dict = field(default_factory=dict)          # Jev probabilities (empty when off or failed)
    handoff: str = ""                              # first hit in HANDOFF_ORDER
    opt_out: bool = False
    hot: bool = False


def _merge(hits: dict, name: str, src: str) -> None:
    hits[name] = f"{hits[name]}+{src}" if name in hits and src not in hits[name] else hits.get(name, src)


async def _ask(decision: str, state: dict, questions: dict, subject: str):
    return await asyncio.to_thread(Jev(decision, timeout=8).try_ask, state, questions, subject)


async def inbound(coach: Coach, thread_id: int, text: str) -> Inbound:
    res = Inbound()
    for name, rx in RX_IN.items():
        if rx.search(text):
            res.hits[name] = "rule"
    m = mode("inbound")
    if m != "off" and text.strip():
        a = await _ask("dm_inbound", {"lead_messages": text}, INBOUND_QUESTIONS, f"{coach.slug}/t{thread_id}")
        if a is not None:
            res.p = {k: a.p(k) for k in INBOUND_QUESTIONS}
            jev_hits = [k for k, v in res.p.items() if v >= INBOUND_BARS[k]]
            trail(TRAIL, "dm_inbound", {"coach": coach.slug, "thread": thread_id, "mode": m, "p": res.p,
                                        "rule_hits": sorted(res.hits), "jev_hits": jev_hits, "model": a.model})
            if m == "on":
                for k in jev_hits:
                    _merge(res.hits, k, "jev")
    res.opt_out = "opt_out" in res.hits
    res.hot = "ready_to_buy" in res.hits
    res.handoff = next((k for k in HANDOFF_ORDER if k in res.hits), "")
    return res


def _allowed_urls(coach: Coach) -> set[str]:
    blob = " ".join(str(coach.cfg.get(k, "")) for k in ("downsell", "allowed_links"))
    return {u.rstrip(".,)") for u in URL.findall(blob)}


async def outbound(coach: Coach, thread_id: int, bubbles: list[str]) -> dict:
    """{flag: source} for a drafted reply; empty means nothing tripped."""
    text = "\n".join(bubbles)
    flags: dict = {}
    for name, rx in RX_OUT.items():
        if name == "url":
            if any(u.rstrip(".,)") not in _allowed_urls(coach) for u in URL.findall(text)):
                flags["url"] = "rule"
        elif rx.search(text):
            flags[name] = "rule"
    m = mode("outbound")
    if m != "off" and text.strip():
        a = await _ask("dm_outbound", {"reply": text}, OUTBOUND_QUESTIONS, f"{coach.slug}/t{thread_id}")
        if a is not None:
            p = {k: a.p(k) for k in OUTBOUND_QUESTIONS}
            jev_hits = [k for k, v in p.items() if v >= OUTBOUND_BARS[k]]
            trail(TRAIL, "dm_outbound", {"coach": coach.slug, "thread": thread_id, "mode": m, "p": p,
                                         "rule_hits": sorted(flags), "jev_hits": jev_hits, "model": a.model})
            if m == "on":
                for k in jev_hits:
                    _merge(flags, k, "jev")
    return flags
