"""The setter loop. Code owns the control flow; Claude writes; Jev and the patterns decide at the branch points.

  webhook -> ingest()        mirror the message, debounce a reply job (ack in milliseconds)
  worker  -> reply_job()     hours check -> media -> cheap exits -> budget -> inbound gates -> summary -> Claude
                              -> stage/slot validation -> style pass -> outbound gates -> link gating -> decide()
  decide()                   DRY_RUN -> 'dry' draft · draft mode / any hold / low confidence / window closed ->
                              pending draft for a person · otherwise deliver() with human timing
  deliver()                  seen, heart, read pause, typing per bubble; stops if the lead writes again mid-send
  echo of a message we did not send -> the coach typed in the app -> the bot steps back on that thread
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import brain, db, demo, gates, humanize, llm, media, notify
from . import coach as coaches
from .coach import Coach
from .config import settings
from .instagram import Account, IGError
from .prompts import NUDGE_NOTE, STYLE_RETRY_NOTE
from .questions import ACK_ONLY, BOOKING_TOKEN
from .stages import NO_NUDGE, link_allowed, next_stage, required_known
from .webhook import Event

WINDOW_S = 24 * 3600
WINDOW_MARGIN_S = 120
LINK_PLACEHOLDER = re.compile(r"\[(booking |call )?link\]|\{link\}|<link>", re.I)


# --- senders -----------------------------------------------------------------------------------------------
class Sender(Protocol):
    async def seen(self, t: dict) -> None: ...
    async def typing(self, t: dict) -> None: ...
    async def react(self, t: dict, mid: str) -> None: ...
    async def send(self, t: dict, text: str, human_agent: bool = False) -> str: ...


class IGSender:
    def _acct(self, t: dict) -> Account:
        a = db.account(t["account_id"])
        if not a or not a.get("access_token"):
            raise IGError(401, {"error": {"message": "account not connected: open /connect/<coach>"}})
        return Account(a["ig_user_id"], a["access_token"])

    async def seen(self, t: dict) -> None:
        await self._acct(t).action(t["igsid"], "mark_seen")

    async def typing(self, t: dict) -> None:
        await self._acct(t).action(t["igsid"], "typing_on")

    async def react(self, t: dict, mid: str) -> None:
        if mid and not mid.startswith(("c_", "local_")):
            await self._acct(t).react(t["igsid"], mid)

    async def send(self, t: dict, text: str, human_agent: bool = False) -> str:
        return await self._acct(t).send_text(t["igsid"], text, human_agent=human_agent)


class CaptureSender:
    """Records instead of sending: the terminal chat, evals and tests use it."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.hearts: list[str] = []

    async def seen(self, t: dict) -> None:
        pass

    async def typing(self, t: dict) -> None:
        pass

    async def react(self, t: dict, mid: str) -> None:
        self.hearts.append(mid)

    async def send(self, t: dict, text: str, human_agent: bool = False) -> str:
        self.sent.append((t["id"], text))
        return f"local_{uuid.uuid4().hex[:12]}"


SENDER: Sender = IGSender()


def window_open(t: dict, at: float | None = None) -> bool:
    return bool(t.get("last_in_at")) and (at or time.time()) - t["last_in_at"] < WINDOW_S - WINDOW_MARGIN_S


def day_start() -> float:
    n = time.time()
    return n - n % 86400


def tracked_link(c: Coach, t: dict) -> str:
    """The booking link with the thread token, so the booking webhook can find the conversation."""
    u = urlparse(c.call_link)
    q = dict(parse_qsl(u.query))
    q.update(utm_source="instagram", utm_medium="dm", utm_content=t["token"])
    if "cal.com" in u.netloc:
        q["metadata[setter]"] = t["token"]
    return urlunparse(u._replace(query=urlencode(q)))


# --- ingest (webhook path: fast, no network) --------------------------------------------------------------
async def ingest(ev: Event) -> None:
    c = coaches.for_account(ev.account_id)
    if not c:
        db.audit(None, "", "unknown_account", account=ev.account_id, kind=ev.kind)
        return
    if ev.kind == "comment":
        await comment_event(c, ev)
        return
    if not ev.igsid or ev.igsid == ev.account_id or ev.kind == "read":
        return
    t, new = db.get_or_create_thread(c.slug, ev.account_id, ev.igsid)
    if new:
        db.schedule("profile", t["id"], time.time())
    if ev.kind == "message":
        at = ev.ts or time.time()
        msg_id = db.add_message(t["id"], "in", "lead", ev.text, mid=ev.mid or None, kind=ev.msg_kind,
                                media_url=ev.media_url, at=at)
        if msg_id is None:
            return                                           # webhook retry: already have it
        if ev.text.strip().upper() == "DELETE":              # the data-deletion path promised on /privacy
            erase_thread(c, t)
            return
        hub = demo.hub_for(ev.account_id)
        if hub:                                              # demo account: DEMO <code> / RESET switch the pack
            demo.on_message(hub, t, msg_id, ev.text)
            t = db.thread(t["id"])
        fields: dict = {"last_in_at": at}
        if ev.source and not t["source"]:
            fields["source"] = ev.source
        if t["opted_out"]:
            fields["opted_out"] = 0                          # they came back on their own
        db.update_thread(t["id"], **fields)
        db.run("UPDATE jobs SET status='cancelled' WHERE kind='nudge' AND thread_id=? AND status='queued'",
               (t["id"],))
        pc = coaches.get(t["coach"]) or c
        wait = float(hub.get("demo.debounce_seconds", 6)) if hub else \
            float(pc.get("humanize.debounce_seconds", settings.debounce_s))
        db.schedule_reply(t["id"], wait, settings.debounce_max_s)
    elif ev.kind == "echo":
        await on_echo(c, t, ev)
    elif ev.kind == "deleted" and ev.mid:
        db.run("UPDATE messages SET kind='deleted' WHERE mid=?", (ev.mid,))
    elif ev.kind == "reaction":
        db.audit(t["id"], c.slug, "lead_reaction", what=ev.text, mid=ev.mid)
    elif ev.kind == "referral" and ev.source and not t["source"]:
        db.update_thread(t["id"], source=ev.source)


def erase_thread(c: Coach, t: dict) -> None:
    """Forget the lead: messages, drafts, profile and facts go; the bare thread row stays opted out so the bot
    never messages them again (and a webhook retry cannot resurrect the conversation)."""
    tid = t["id"]
    db.cancel_jobs(tid, ("reply", "nudge", "profile"))
    db.run("DELETE FROM messages WHERE thread_id=?", (tid,))
    db.run("DELETE FROM drafts WHERE thread_id=?", (tid,))
    db.run("DELETE FROM audit WHERE thread_id=?", (tid,))
    db.update_thread(tid, username="", name="", slots={}, summary="", summary_upto=0, opted_out=1, source="")
    db.audit(tid, c.slug, "erased_on_request")


async def on_echo(c: Coach, t: dict, ev: Event) -> None:
    if ev.mid and db.one("SELECT id FROM messages WHERE mid=?", (ev.mid,)):
        return                                               # our own send, already recorded
    mine = db.one("SELECT id FROM messages WHERE thread_id=? AND dir='out' AND mid IS NULL AND text=?"
                  " AND created_at>=? ORDER BY id DESC LIMIT 1", (t["id"], ev.text, time.time() - 180))
    if mine:                                                 # echo raced ahead of the send response
        db.run("UPDATE messages SET mid=? WHERE id=?", (ev.mid or None, mine["id"]))
        return
    # The coach typed this in the Instagram app: step back on this thread.
    db.add_message(t["id"], "out", "coach", ev.text, mid=ev.mid or None, kind=ev.msg_kind)
    hours = float(c.get("humanize.takeover_hours", 12))
    db.update_thread(t["id"], paused_until=time.time() + hours * 3600, last_out_at=time.time())
    db.cancel_jobs(t["id"])
    db.supersede_drafts(t["id"])
    db.audit(t["id"], c.slug, "coach_took_over", hours=hours)


# --- jobs --------------------------------------------------------------------------------------------------
async def run_job(job: dict) -> None:
    kind, tid = job["kind"], job["thread_id"]
    payload = json.loads(job.get("payload") or "{}")
    if kind == "reply":
        await reply_job(tid)
    elif kind == "nudge":
        await nudge_job(tid)
    elif kind == "profile":
        await profile_job(tid)
    elif kind == "comment":
        await comment_reply_job(payload)


async def profile_job(tid: int) -> None:
    t = db.thread(tid)
    if not t or not isinstance(SENDER, IGSender):
        return
    a = db.account(t["account_id"])
    if not a or not a.get("access_token"):
        return
    try:
        p = await Account(a["ig_user_id"], a["access_token"]).profile(t["igsid"])
    except IGError:
        return
    db.update_thread(tid, name=p.get("name") or "", username=p.get("username") or t["username"],
                     follows=None if p.get("is_user_follow_business") is None else int(p["is_user_follow_business"]))


async def reply_job(tid: int) -> None:
    t = db.thread(tid)
    c = coaches.get(t["coach"]) if t else None
    if not t or not c:
        return
    now = time.time()
    if not settings.enabled:
        db.audit(tid, c.slug, "skipped", why="SETTER_ENABLED is off")
        return
    if t["opted_out"] or t["paused_until"] > now:
        return
    burst = db.unanswered(tid)
    if not burst:
        return
    hub = demo.hub_for(t["account_id"])
    if hub and (c.slug == hub.slug or demo.over_limit(hub, t)):
        text = demo.hub_reply(hub, burst[-1]["text"]) if c.slug == hub.slug else demo.DONE
        await decide(hub, t, [text], confidence=1.0, holds=[], heart_mid="", purpose="demo_menu",
                     meta={"answered_upto": burst[-1]["id"]})
        return
    later = None if hub else humanize.next_active(c.get("hours"), now)
    if later and t["last_in_at"] and later < t["last_in_at"] + WINDOW_S - 3600:
        db.schedule("reply", tid, later)
        db.audit(tid, c.slug, "deferred_to_active_hours", until=later)
        return
    if t["handoff"]:
        await notify.send(c, t, f"new message in a handed-off thread ({t['handoff']})")
        return

    problems = await media.enrich(c.slug, tid, burst)
    burst = db.unanswered(tid)
    text = "\n".join(m["text"] for m in burst)

    if t["stage"] == "BOOKED" and ACK_ONLY.match(text):     # "thanks!" after booking: heart it, no model call
        await SENDER.react(t, burst[-1]["mid"] or "")
        db.audit(tid, c.slug, "acked_with_heart")
        return

    cap = float(c.get("limits.daily_usd", 5) or 0)
    if cap and db.spend_since(c.slug, day_start()) >= cap:
        if not db.one("SELECT 1 FROM audit WHERE coach=? AND event='budget_stop' AND created_at>=?",
                      (c.slug, day_start())):
            await notify.send(c, None, f"daily model budget (${cap:.2f}) reached: replies are held until tomorrow")
        db.audit(tid, c.slug, "budget_stop", cap=cap)
        db.add_draft(tid, [], f"daily budget ${cap:.2f} reached: reply yourself", None, {})
        return

    g = await gates.inbound(c, tid, text)
    db.audit(tid, c.slug, "inbound_gates", hits=g.hits, p=g.p)
    if g.opt_out:
        db.update_thread(tid, opted_out=1)
        db.cancel_jobs(tid)
        db.audit(tid, c.slug, "opted_out")
        await notify.send(c, t, "asked to stop messaging: the bot has stopped on this thread")
        return
    if g.hot and not t["hot"]:
        db.update_thread(tid, hot=1)
        await notify.send(c, t, "HOT: says they want to pay / sign up now")
        t = db.thread(tid)
    if g.handoff:
        await handoff(c, t, g.handoff, source=g.hits.get(g.handoff, ""))
        return
    if problems:
        await handoff(c, t, problems[0])
        return

    t = await brain.maybe_summarize(c, t)
    try:
        r = await brain.reply(c, t)
    except llm.Refused as e:
        await handoff(c, t, f"model declined ({e})")
        return
    except llm.LLMError as e:
        if e.retryable:
            raise                                            # the worker retries with backoff, then hold()s
        await hold(c, t, f"model unavailable: {e}")         # a service problem, not this lead: no handoff flag
        return
    await act_on_reply(c, t, r, burst)


async def hold(c: Coach, t: dict, reason: str) -> None:
    """Park the thread for a person without handing it off: the next message gets a normal attempt again."""
    db.add_draft(t["id"], [], reason, None, {})
    db.audit(t["id"], c.slug, "held", reasons=[reason])
    await notify.send(c, t, f"reply yourself: {reason}")


async def act_on_reply(c: Coach, t: dict, r: brain.Reply, burst: list[dict], purpose: str = "reply") -> None:
    tid = t["id"]
    if r.handoff:
        await handoff(c, t, r.reason or "the model asked for a person", source="model")
        return
    slots = {**(t["slots"] or {}), **r.slots}
    qualified = required_known(c.slots, slots)
    stage = next_stage(t["stage"], r.stage, qualified=qualified, hot=bool(t["hot"]))
    db.update_thread(tid, slots=slots, stage=stage)
    t = db.thread(tid)

    bubbles = humanize.style(r.bubbles, c.style)
    if any(LINK_PLACEHOLDER.search(b) for b in bubbles):      # the examples show "[booking link]": never send it
        r.send_link = True
        bubbles = [x for x in (LINK_PLACEHOLDER.sub("", b).strip(" :") for b in bubbles) if x]
    holds: list[str] = []
    found = humanize.tells(bubbles)
    if found:
        try:
            r2 = await brain.reply(c, t, extra=STYLE_RETRY_NOTE.format(tells=", ".join(found)), purpose="restyle")
            b2 = humanize.style(r2.bubbles, c.style)
            if b2 and not humanize.tells(b2) and not r2.handoff:
                bubbles, r = b2, r2
            else:
                holds.append("sounds like a bot: " + ", ".join(found))
        except llm.LLMError:
            holds.append("sounds like a bot: " + ", ".join(found))

    flags = await gates.outbound(c, tid, bubbles)
    holds += [f"{k} ({v})" for k, v in flags.items()]
    link = ""
    if r.send_link:
        if c.call_link and link_allowed(stage, qualified=qualified, hot=bool(t["hot"]), links_sent=t["links_sent"],
                                        max_links=int(c.get("limits.max_links_per_thread", 2))):
            link = tracked_link(c, t)
        else:
            holds.append("asked to send the link before the lead qualified")
    heart = (burst[-1].get("mid") or "") if (r.heart and burst) else ""
    if not bubbles and not link:
        if heart:
            await SENDER.react(t, heart)
        db.audit(tid, c.slug, "no_reply_needed", stage=stage)
        return
    out = bubbles + ([link] if link else [])
    disclose = c.get("disclosure.mode", "profile") == "first_message" and not t["disclosed"]
    if disclose and c.get("disclosure.first_message_line"):
        out.append(humanize.clean(c.get("disclosure.first_message_line"), c.style) or c.get("disclosure.first_message_line"))
    await decide(c, t, out, confidence=r.confidence, holds=holds, heart_mid=heart, purpose=purpose,
                 meta={"link": bool(link), "disclose": disclose, "stage": stage, "usage": r.usage,
                       "answered_upto": burst[-1]["id"] if burst else 0})


async def decide(c: Coach, t: dict, bubbles: list[str], *, confidence: float, holds: list[str], heart_mid: str,
                 purpose: str, meta: dict) -> str:
    tid = t["id"]
    meta = {**meta, "heart": heart_mid, "purpose": purpose}
    reasons = list(holds)
    hub = demo.hub_for(t["account_id"])
    mode = t["mode"] or c.mode
    if mode != "autopilot" and not hub:
        reasons.append("draft mode")
    bar = float(c.get("autopilot.min_confidence", 0.75))
    if confidence < bar and not hub:
        reasons.append(f"confidence {confidence:.2f} < {bar:.2f}")
    if mode == "autopilot" and not hub and c.get("disclosure.mode", "profile") == "profile" \
            and not c.get("disclosure.profile_confirmed"):
        reasons.append("autopilot needs disclosure.profile_confirmed (or disclosure.mode: first_message)")
    if hub and holds and purpose != "demo_note":            # demo: never send flagged text; show the guardrail
        db.audit(tid, c.slug, "demo_guardrail", reasons=holds, bubbles=bubbles)
        bubbles, reasons = [demo.note(hub, c, "; ".join(holds))], []
    if hub and not settings.dry_run and window_open(t):    # the demo bio is the disclosure; no drafts here
        reasons = []
    if not window_open(t):
        reasons.append("24h reply window closed")
    if settings.dry_run:
        db.add_draft(tid, bubbles, "DRY_RUN; would hold: " + "; ".join(reasons) if reasons else "DRY_RUN: would send",
                     confidence, meta, status="dry")
        db.audit(tid, c.slug, "dry_run", bubbles=bubbles, holds=reasons)
        return "dry"
    if reasons:
        db.add_draft(tid, bubbles, "; ".join(reasons), confidence, meta)
        db.audit(tid, c.slug, "held", reasons=reasons, bubbles=bubbles)
        real = [x for x in reasons if x != "draft mode"]
        await notify.send(c, t, ("held: " + "; ".join(real)) if real else "draft waiting for your OK")
        return "held"
    sent = await deliver(c, t, bubbles, heart_mid=heart_mid, meta=meta)
    return "sent" if sent else "interrupted"


async def handoff(c: Coach, t: dict, reason: str, source: str = "") -> None:
    tid = t["id"]
    if reason == "bot_question" and c.get("disclosure.bot_question", "handoff") == "disclose" \
            and c.get("disclosure.bot_answer"):
        burst = db.unanswered(tid)
        await decide(c, t, [c.get("disclosure.bot_answer")], confidence=1.0, holds=[], heart_mid="",
                     purpose="bot_answer", meta={"disclose": True, "answered_upto": burst[-1]["id"] if burst else 0})
        return
    hub = demo.hub_for(t["account_id"])
    if hub:                                                  # demo: explain the handoff instead of going quiet
        burst = db.unanswered(tid)
        db.audit(tid, c.slug, "demo_handoff", reason=reason, source=source)
        await decide(c, t, [demo.note(hub, c, reason.replace("_", " "))], confidence=1.0, holds=[], heart_mid="",
                     purpose="demo_note", meta={"answered_upto": burst[-1]["id"] if burst else 0})
        return
    db.update_thread(tid, handoff=reason)
    db.cancel_jobs(tid, ("nudge",))
    db.add_draft(tid, [], f"handoff: {reason}", None, {"source": source})
    db.audit(tid, c.slug, "handoff", reason=reason, source=source)
    urgent = "URGENT " if reason == "self_harm" else ""
    await notify.send(c, t, f"{urgent}needs you ({reason}): the bot has stepped back on this thread")


# --- sending -----------------------------------------------------------------------------------------------
def _paused(tid: int) -> bool:
    t = db.thread(tid)
    return bool(t and (t["paused_until"] > time.time() or t["opted_out"]))


async def deliver(c: Coach, t: dict, bubbles: list[str], *, by: str = "bot", heart_mid: str = "",
                  human_agent: bool = False, meta: dict | None = None) -> int:
    """Send bubbles like a person types them. Stops early if the lead writes again (the new reply job answers
    everything) or the coach takes over. Returns how many bubbles went out."""
    meta = meta or {}
    tid = t["id"]
    upto = int(meta.get("answered_upto") or db.last_inbound_id(tid))
    timing = settings.humanize_timing and by == "bot"
    await SENDER.seen(t)
    if heart_mid:
        await SENDER.react(t, heart_mid)
    if timing:
        last_in = db.one("SELECT text FROM messages WHERE id=?", (upto,)) or {"text": ""}
        await asyncio.sleep(humanize.read_seconds(last_in["text"] or ""))
    sent = 0
    for i, b in enumerate(bubbles):
        if by == "bot" and (db.last_inbound_id(tid) > upto or _paused(tid)):
            db.audit(tid, c.slug, "send_interrupted", sent=sent, of=len(bubbles))
            break
        if timing:
            await SENDER.typing(t)
            await asyncio.sleep(humanize.typing_seconds(b))
        row = db.add_message(tid, "out", by, b, mid=None)       # recorded first so the echo can find it
        try:
            mid = await SENDER.send(t, b, human_agent=human_agent)
        except IGError as e:
            db.run("DELETE FROM messages WHERE id=?", (row,))
            db.audit(tid, c.slug, "send_failed", error=str(e), sent=sent)
            if e.outside_window:
                db.add_draft(tid, bubbles[i:], "Instagram says the 24h window is closed", None, meta)
            elif sent == 0 and e.retryable:
                raise
            break
        db.run("UPDATE messages SET mid=COALESCE(mid, ?) WHERE id=?", (mid or None, row))
        sent += 1
        if timing and i < len(bubbles) - 1:
            await asyncio.sleep(humanize.gap_seconds())
    if sent:
        fields: dict = {"last_out_at": time.time()}
        if meta.get("link"):
            fields["links_sent"] = (db.thread(tid)["links_sent"] or 0) + 1
        if meta.get("disclose"):
            fields["disclosed"] = 1
        db.update_thread(tid, **fields)
        db.audit(tid, c.slug, "sent", by=by, bubbles=bubbles[:sent], purpose=meta.get("purpose", ""))
        schedule_nudge(c, db.thread(tid))
    return sent


def schedule_nudge(c: Coach, t: dict) -> None:
    if not c.get("nudge.enabled", True) or t["stage"] in NO_NUDGE or not t["last_in_at"] or demo.is_demo(t):
        return
    if t["nudged_for"] == t["last_in_at"]:
        return                                                # one nudge per window, never more
    due = max(t["last_in_at"] + float(c.get("nudge.after_hours", 20)) * 3600, time.time() + 3 * 3600)
    if due < t["last_in_at"] + WINDOW_S - 1200:
        db.schedule("nudge", t["id"], due)


async def nudge_job(tid: int) -> None:
    t = db.thread(tid)
    c = coaches.get(t["coach"]) if t else None
    if not t or not c or not settings.enabled or t["opted_out"] or t["handoff"] or t["stage"] in NO_NUDGE:
        return
    if t["paused_until"] > time.time() or t["nudged_for"] == t["last_in_at"]:
        return
    last = db.one("SELECT dir FROM messages WHERE thread_id=? AND kind!='deleted' ORDER BY id DESC LIMIT 1", (tid,))
    if not last or last["dir"] != "out" or not window_open(t, time.time() + 900):
        return
    db.update_thread(tid, nudged_for=t["last_in_at"])
    if c.get("nudge.mode", "llm") == "template" and c.get("nudge.templates"):
        bubbles, conf = [random.choice(c.get("nudge.templates"))], 1.0
    else:
        hours = int((time.time() - (t["last_out_at"] or t["last_in_at"])) // 3600)
        try:
            r = await brain.reply(c, t, note=NUDGE_NOTE.format(hours=max(1, hours)), purpose="nudge")
        except llm.LLMError:
            return
        if r.handoff or not r.bubbles:
            return
        bubbles, conf = humanize.style(r.bubbles, c.style)[:1], r.confidence
    flags = await gates.outbound(c, tid, bubbles)
    await decide(c, t, bubbles, confidence=conf, holds=[f"{k} ({v})" for k, v in flags.items()], heart_mid="",
                 purpose="nudge", meta={"answered_upto": db.last_inbound_id(tid)})


# --- people acting from the dashboard ------------------------------------------------------------------------
async def approve(draft_id: int, edited: list[str] | None = None) -> str:
    d = db.draft(draft_id)
    if not d or d["status"] not in ("pending", "dry"):
        return "that draft is no longer pending"
    t = db.thread(d["thread_id"])
    c = coaches.get(t["coach"]) if t else None
    if not t or not c:
        return "thread or coach missing"
    if settings.dry_run:
        return "DRY_RUN is on: nothing was sent (set DRY_RUN=0 to send for real)"
    bubbles = [b for b in (edited if edited is not None else d["bubbles"]) if b.strip()]
    if not bubbles:
        return "nothing to send"
    changed = edited is not None and bubbles != d["bubbles"]
    human_agent = False
    if not window_open(t):
        if not (settings.human_agent_enabled and changed):
            return ("the 24h window is closed: Instagram only allows a human-written reply now (HUMAN_AGENT, "
                    "7 days, after App Review). Edit the text and enable HUMAN_AGENT_ENABLED, or reply in the app.")
        human_agent = True
    meta = {**d["meta"], "answered_upto": db.last_inbound_id(t["id"])}
    sent = await deliver(c, t, bubbles, by="human" if changed else "bot", heart_mid=d["meta"].get("heart", ""),
                         human_agent=human_agent, meta=meta)
    db.run("UPDATE drafts SET status=?, decided_at=? WHERE id=?",
           ("sent" if sent else "failed", time.time(), draft_id))
    return f"sent {sent} message(s)"


async def manual_send(tid: int, text: str) -> str:
    t = db.thread(tid)
    c = coaches.get(t["coach"]) if t else None
    if not t or not c:
        return "thread or coach missing"
    if settings.dry_run:
        return "DRY_RUN is on: nothing was sent"
    bubbles = [b.strip() for b in text.split("\n\n") if b.strip()]
    human_agent = not window_open(t)
    if human_agent and not settings.human_agent_enabled:
        return "the 24h window is closed and HUMAN_AGENT_ENABLED is off: reply in the Instagram app"
    sent = await deliver(c, t, bubbles, by="human", human_agent=human_agent,
                         meta={"answered_upto": db.last_inbound_id(tid)})
    hours = float(c.get("humanize.takeover_hours", 12))
    db.update_thread(tid, paused_until=time.time() + hours * 3600)
    db.supersede_drafts(tid)
    return f"sent {sent} message(s); the bot is paused here for {hours:g}h"


# --- bookings ----------------------------------------------------------------------------------------------
async def booking(payload: dict) -> list[int]:
    blob = json.dumps(payload)
    event = " ".join(str(payload.get(k, "")) for k in ("event", "triggerEvent", "type")).lower()
    cancelled = "cancel" in event
    done = []
    for tok in set(BOOKING_TOKEN.findall(blob)):
        row = db.one("SELECT id FROM threads WHERE token=?", (tok,))
        if not row:
            continue
        t = db.thread(row["id"])
        c = coaches.get(t["coach"])
        if cancelled:
            db.update_thread(t["id"], stage="BOOKING", booked_at=None)
            msg = "cancelled their call"
        else:
            db.update_thread(t["id"], stage="BOOKED", booked_at=time.time())
            db.cancel_jobs(t["id"], ("nudge",))
            msg = "BOOKED a call"
        db.audit(t["id"], t["coach"], "booking", cancelled=cancelled)
        await notify.send(c, t, msg)
        done.append(t["id"])
    return done


# --- comment -> DM -----------------------------------------------------------------------------------------
def match_trigger(c: Coach, text: str) -> dict | None:
    low = f" {text.lower()} "
    for trig in c.cfg.get("comment_triggers") or []:
        for kw in trig.get("keywords") or []:
            k = str(kw).lower().strip()
            if k and (f" {k} " in low or f" {k}!" in low or f" {k}." in low or low.strip() == k):
                return trig
    return None


async def comment_event(c: Coach, ev: Event) -> None:
    if not ev.comment_id or not ev.igsid or ev.igsid == ev.account_id:
        return
    if db.one("SELECT 1 FROM comments WHERE comment_id=?", (ev.comment_id,)):
        return
    trig = match_trigger(c, ev.text)
    db.run("INSERT INTO comments VALUES (?,?,?,?,?,?,?)", (ev.comment_id, ev.account_id, ev.media_id, ev.igsid,
                                                           ev.text, "trigger" if trig else "none", time.time()))
    if trig:
        db.schedule("comment", None, time.time() + random.uniform(4, 25), {
            "coach": c.slug, "account_id": ev.account_id, "igsid": ev.igsid, "username": ev.username,
            "comment_id": ev.comment_id, "text": ev.text}, replace=False)


async def comment_reply_job(p: dict) -> None:
    c = coaches.get(p["coach"])
    trig = match_trigger(c, p["text"]) if c else None
    if not c or not trig:
        return
    sent_last_hour = db.one("SELECT COUNT(*) AS n FROM comments WHERE account_id=? AND action='sent' AND created_at>=?",
                            (p["account_id"], time.time() - 3600))["n"]
    if sent_last_hour >= 700:                                 # Meta caps private replies at 750/hour
        db.schedule("comment", None, time.time() + 600, p, replace=False)
        return
    t, _ = db.get_or_create_thread(c.slug, p["account_id"], p["igsid"])
    if p.get("username") and not t["username"]:
        db.update_thread(t["id"], username=p["username"], source="comment")
    dm = str(trig.get("dm", "")).format(username=p.get("username", ""), url=trig.get("url", "")).strip()
    db.add_message(t["id"], "in", "lead", f"[commented on your post] {p['text']}", mid=f"c_{p['comment_id']}",
                   kind="comment")
    if settings.dry_run or not dm:
        db.add_draft(t["id"], [dm], "DRY_RUN: comment private reply", 1.0, {}, status="dry")
        return
    a = db.account(p["account_id"])
    if not a or not a.get("access_token"):
        return
    acct = Account(a["ig_user_id"], a["access_token"])
    try:
        mid = await acct.private_reply(p["comment_id"], dm)
    except IGError as e:
        db.audit(t["id"], c.slug, "private_reply_failed", error=str(e))
        return
    db.add_message(t["id"], "out", "bot", dm, mid=mid or None, kind="comment_reply")
    db.run("UPDATE comments SET action='sent' WHERE comment_id=?", (p["comment_id"],))
    db.update_thread(t["id"], last_out_at=time.time())
    replies = trig.get("public_replies") or []
    if replies:
        try:
            await acct.public_reply(p["comment_id"], random.choice(replies))
        except IGError:
            pass
