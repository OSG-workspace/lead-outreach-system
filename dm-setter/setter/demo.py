"""Demo mode: one Instagram account where any coach can try a setter built for them.

The demo account is connected to a hub pack (coach.yaml has `demo: {hub: true, ...}`). Every coach pack that can be
demoed carries a `demo_code` (e.g. JANE7). On the demo account:

  "DEMO JANE7"   the thread switches to Jane's pack and starts fresh: her voice, script, offer and booking link
  "RESET"        back to the hub, history hidden; the next DEMO <code> starts a new run
  anything else  while on the hub: a fixed menu reply, no model call

Demo threads always answer at once (no draft mode, active hours or nudges) and stop after
`demo.max_replies` bot turns. A guardrail that would hand a real chat to the coach sends a short note instead, so the
person trying it sees what the safety layer does rather than silence.
"""

from __future__ import annotations

import re

from . import coach as coaches
from . import db
from .coach import Coach

CODE = re.compile(r"^\s*demo\s+([a-z0-9][a-z0-9_-]{1,23})\b", re.I)
OPENED = "[opened the chat]"
MENU = ("hey! this account demos AI DM setters for coaches. reply DEMO + the code you were sent "
        "(like DEMO {example}) and I'll turn into that coach's setter. you play the lead")
UNKNOWN = "hmm I don't have a setter for \"{code}\". check the code and send DEMO + code again"
DONE = "that's the end of this demo. reply RESET to start over, or DEMO + a code to try another setter"
NOTE = "[demo note: in the live version this chat goes to {coach} now: {why}. reply RESET to start over]"


def hub_for(account_id: str) -> Coach | None:
    c = coaches.for_account(account_id)
    return c if c and c.get("demo.hub") else None


def is_demo(t: dict) -> bool:
    return bool(t and hub_for(t["account_id"]))


def persona(code: str) -> Coach | None:
    code = code.strip().lower()
    for slug in coaches.all_slugs():
        c = coaches.get(slug)
        if c and not c.get("demo.hub") and str(c.cfg.get("demo_code", "")).strip().lower() == code:
            return c
    return None


def command(hub: Coach, text: str) -> tuple[str, str]:
    """('start', code) | ('reset', '') | ('', '')."""
    if text.strip().upper() == str(hub.get("demo.reset_word", "RESET")).upper():
        return "reset", ""
    m = CODE.match(text)
    return ("start", m.group(1)) if m else ("", "")


def _fresh(tid: int, coach: str, upto: int) -> None:
    db.cancel_jobs(tid, ("reply", "nudge"))
    db.supersede_drafts(tid)
    db.update_thread(tid, coach=coach, stage="OPENER", slots={}, summary="", summary_upto=upto, links_sent=0,
                     handoff="", hot=0, paused_until=0, booked_at=None, nudged_for=0, opted_out=0, disclosed=1,
                     mode="autopilot")


def on_message(hub: Coach, t: dict, msg_id: int, text: str) -> None:
    """Webhook path (database only): apply DEMO <code> / RESET before the reply job is scheduled."""
    cmd, code = command(hub, text)
    if cmd == "reset":
        _fresh(t["id"], hub.slug, msg_id)
        db.audit(t["id"], hub.slug, "demo_reset")
    elif cmd == "start":
        p = persona(code)
        if p:
            db.run("UPDATE messages SET text=? WHERE id=?", (OPENED, msg_id))
            _fresh(t["id"], p.slug, msg_id - 1)          # the code message becomes the opening turn
            db.audit(t["id"], p.slug, "demo_start", code=code)
        elif t["coach"] != hub.slug:
            _fresh(t["id"], hub.slug, msg_id)            # unknown code mid-demo: back to the menu
        if not p:
            db.audit(t["id"], hub.slug, "demo_unknown_code", code=code)


def hub_reply(hub: Coach, burst_text: str) -> str:
    cmd, code = command(hub, burst_text.splitlines()[-1] if burst_text else "")
    if cmd == "start":
        return str(hub.get("demo.unknown", UNKNOWN)).format(code=code)
    example = hub.get("demo.example_code", "MARCUS")
    return str(hub.get("demo.menu", MENU)).format(example=example)


def over_limit(hub: Coach, t: dict) -> bool:
    n = db.one("SELECT COUNT(*) AS n FROM messages WHERE thread_id=? AND id>? AND dir='out' AND by='bot'",
               (t["id"], t.get("summary_upto") or 0))["n"]
    return n >= int(hub.get("demo.max_replies", 40))


def note(hub: Coach, c: Coach, why: str) -> str:
    return str(hub.get("demo.note", NOTE)).format(coach=c.first_name, why=why)
