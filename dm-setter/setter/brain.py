"""The setter brain: one structured Claude call per turn, laid out so almost every input token is a cache read.

Request layout (render order is system -> messages; a cache breakpoint covers everything before it):

  system[0]  SETTER_RULES            shared by every coach        breakpoint 1 (1h)
  system[1]  coach pack               voice, script, offer, examples breakpoint 2 (1h)
  messages   [summary of older turns] + the mirrored conversation
             ... last turn we sent                                breakpoint 3 (5m) <- next turn reads up to here
             the lead's new burst
  system     per-turn context note (time, stage, known facts)     never cached, never stored

Nothing that changes per turn sits before a breakpoint, so turn N+1 re-reads turn N's prefix at 10% of the input
price and pays full price only for the new burst and the short context note. History is trimmed by a rolling
summary every ~20 messages (one cheap-model call), which is the only time the conversation prefix changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db, llm
from .coach import Coach
from .config import settings
from .prompts import SETTER_RULES, SUMMARY_PROMPT
from .stages import MODEL_STAGES, required_known

WINDOW_S = 24 * 3600


@dataclass
class Reply:
    bubbles: list[str]
    stage: str
    slots: dict
    confidence: float
    handoff: bool
    reason: str
    send_link: bool
    heart: bool
    usage: dict = field(default_factory=dict)


def _cc(ttl: str | None = None) -> dict:
    return {"type": "ephemeral", "ttl": ttl} if ttl else {"type": "ephemeral"}


def system_blocks(coach: Coach) -> list[dict]:
    ttl = settings.system_cache_ttl if settings.system_cache_ttl in ("5m", "1h") else "1h"
    return [{"type": "text", "text": SETTER_RULES, "cache_control": _cc(ttl)},
            {"type": "text", "text": coach.block(), "cache_control": _cc(ttl)}]


def _turns(thread: dict) -> list[list[str]]:
    """The mirrored conversation as alternating [role, text] turns (consecutive messages merged)."""
    turns: list[list[str]] = []
    if thread.get("summary"):
        turns.append(["user", f"(earlier in this chat, summarised)\n{thread['summary']}"])
    for m in db.messages(thread["id"], after_id=thread.get("summary_upto") or 0):
        role = "user" if m["dir"] == "in" else "assistant"
        text = (m["text"] or "").strip() or "[message]"
        if turns and turns[-1][0] == role:
            turns[-1][1] += "\n" + text
        else:
            turns.append([role, text])
    if turns and turns[0][0] == "assistant":
        turns.insert(0, ["user", "(the chat was started from your side)"])
    return turns


def first_name(thread: dict) -> str:
    n = (thread.get("name") or "").strip().split()
    return n[0] if n and n[0].isalpha() and len(n[0]) > 1 else ""


def context_note(coach: Coach, thread: dict, at: float | None = None, extra: str = "") -> str:
    at = at or time.time()
    tz = coach.get("hours.timezone", "UTC")
    try:
        local = datetime.fromtimestamp(at, ZoneInfo(tz))
    except Exception:  # noqa: BLE001 (unknown zone name in yaml)
        local = datetime.fromtimestamp(at, timezone.utc)
    left = WINDOW_S - (at - (thread.get("last_in_at") or at))
    known = thread.get("slots") or {}
    missing = [s["slot"] for s in coach.slots if s.get("required") and not str(known.get(s["slot"], "")).strip()]
    who = " ".join(x for x in (first_name(thread), f"(@{thread['username']})" if thread.get("username") else "") if x)
    lines = [
        "context (from the system, not the lead):",
        f"now: {local:%a %d %b, %H:%M} coach time · reply window: {max(0, int(left // 3600))}h left",
        f"stage: {thread.get('stage') or 'OPENER'} · call link sent: {'yes' if thread.get('links_sent') else 'no'}"
        f" · lead wants to buy now: {'yes' if thread.get('hot') else 'no'}",
        "known: " + (" | ".join(f"{s}: {known.get(s) or '?'}" for s in coach.slot_names)),
        "still to learn before pitching: " + (", ".join(missing) if missing else "nothing, they qualify"),
    ]
    bits = [who or "name unknown"]
    if thread.get("follows") is not None:
        bits.append("follows you" if thread["follows"] else "doesn't follow you")
    if thread.get("source"):
        bits.append(f"came from: {thread['source']}")
    lines.append("lead: " + ", ".join(bits))
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def build_request(coach: Coach, thread: dict, *, note: str | None = None, extra: str = "",
                  at: float | None = None) -> tuple[list[dict], list[dict]]:
    """(system, messages) for one turn. `note` turns the request into a nudge after our own last message."""
    turns = _turns(thread)
    if note:
        turns.append(["user", "(no reply from the lead yet)"])
    msgs = [{"role": r, "content": [{"type": "text", "text": t}]} for r, t in turns]
    if len(msgs) >= 2:
        msgs[-2]["content"][-1]["cache_control"] = _cc()     # everything before the new burst is reusable
    ctx = context_note(coach, thread, at, extra="\n".join(x for x in (note, extra) if x))
    if llm.canonical(settings.reply_model) in llm.MID_SYSTEM_MODELS and msgs:
        msgs.append({"role": "system", "content": ctx})
    elif msgs:
        msgs[-1]["content"].append({"type": "text", "text": ctx})
    else:
        msgs = [{"role": "user", "content": [{"type": "text", "text": ctx}]}]
    return system_blocks(coach), msgs


def parse(raw: dict, coach: Coach) -> Reply:
    bubbles = [b.strip() for b in raw.get("bubbles") or [] if isinstance(b, str) and b.strip()]
    if len(bubbles) > 3:
        bubbles = bubbles[:2] + [" ".join(bubbles[2:])]
    slots = {}
    for s in raw.get("slots") or []:
        if isinstance(s, dict) and s.get("slot") in coach.slot_names and str(s.get("value", "")).strip():
            slots[s["slot"]] = str(s["value"]).strip()[:120]
    try:
        conf = max(0.0, min(1.0, float(raw.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    stage = raw.get("stage") if raw.get("stage") in MODEL_STAGES else "OPENER"
    return Reply(bubbles=bubbles, stage=stage, slots=slots, confidence=conf, handoff=bool(raw.get("handoff")),
                 reason=str(raw.get("reason") or "")[:200], send_link=bool(raw.get("send_link")),
                 heart=bool(raw.get("heart")))


async def reply(coach: Coach, thread: dict, *, note: str | None = None, extra: str = "",
                purpose: str = "reply") -> Reply:
    system, msgs = build_request(coach, thread, note=note, extra=extra)
    raw, usage = await llm.structured(
        model=settings.reply_model, system=system, messages=msgs, schema=coach.output_schema(),
        coach=coach.slug, thread_id=thread["id"], purpose=purpose, effort=settings.reply_effort,
        thinking=settings.reply_thinking)
    r = parse(raw, coach)
    r.usage = usage
    return r


async def maybe_summarize(coach: Coach, thread: dict) -> dict:
    """Fold the oldest messages into the rolling summary once the live history passes the threshold."""
    live = db.messages(thread["id"], after_id=thread.get("summary_upto") or 0)
    if len(live) <= settings.history_summarize_at:
        return thread
    old = live[: len(live) - settings.history_keep]
    transcript = "\n".join(f"{'LEAD' if m['dir'] == 'in' else 'YOU'}: {m['text']}" for m in old)
    if thread.get("summary"):
        transcript = f"(summary so far) {thread['summary']}\n{transcript}"
    try:
        summary, _ = await llm.text(model=settings.cheap_model, system=SUMMARY_PROMPT,
                                    messages=[{"role": "user", "content": transcript}], coach=coach.slug,
                                    thread_id=thread["id"], purpose="summary", max_tokens=400)
    except llm.LLMError:
        return thread                              # keep the full history this turn; try again next turn
    db.update_thread(thread["id"], summary=summary, summary_upto=old[-1]["id"])
    db.audit(thread["id"], coach.slug, "summarized", upto=old[-1]["id"], chars=len(summary))
    return db.thread(thread["id"])


def qualified(coach: Coach, thread: dict) -> bool:
    return required_known(coach.slots, thread.get("slots") or {})
