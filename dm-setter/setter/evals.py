"""Talk to a coach's setter without Instagram, and test it against simulated leads.

Sandbox runs the real pipeline (gates, Claude, stage machine, style pass, link gating) on an in-memory database
with a capturing sender. Held drafts are shown with their reason and then treated as sent, so a conversation can
continue. The eval suite has a cheap model play personas from evals/personas.yaml and grades every transcript with
deterministic checks; run it after every change to a coach pack or to the rules.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import yaml

from . import db, engine, humanize, llm
from . import coach as coaches
from .config import ROOT, settings
from .questions import RX_OUT
from .webhook import Event


@dataclass
class Turn:
    lead: str
    sent: list[str] = field(default_factory=list)
    held: str = ""
    held_bubbles: list[str] = field(default_factory=list)
    stage: str = ""
    slots: dict = field(default_factory=dict)
    handoff: str = ""
    heart: bool = False
    usd: float = 0.0

    @property
    def bot(self) -> list[str]:
        return self.sent or self.held_bubbles


class Sandbox:
    def __init__(self, slug: str, db_path: str = ":memory:"):
        settings.dry_run = False
        settings.humanize_timing = False
        db.connect(db_path)
        self.coach = coaches.get(slug)
        if not self.coach:
            raise SystemExit(f"no coach pack at coaches/{slug}/coach.yaml")
        self.slug, self.acct = slug, f"sandbox_{slug}"
        db.save_account(self.acct, slug, "sandbox", "", time.time() + 10 ** 9)
        self.sender = engine.CaptureSender()
        engine.SENDER = self.sender

    def new_lead(self, name: str = "Sam", username: str = "sam.test", source: str = "") -> int:
        t, _ = db.get_or_create_thread(self.slug, self.acct, f"lead_{uuid.uuid4().hex[:10]}")
        db.update_thread(t["id"], name=name, username=username, source=source, mode="autopilot")
        return t["id"]

    async def say(self, tid: int, text: str) -> Turn:
        t = db.thread(tid)
        spent0 = db.spend_since(self.slug, 0)
        last_draft = (db.one("SELECT MAX(id) m FROM drafts WHERE thread_id=?", (tid,)) or {}).get("m") or 0
        sent0, hearts0 = len(self.sender.sent), len(self.sender.hearts)
        await engine.ingest(Event(kind="message", account_id=self.acct, igsid=t["igsid"],
                                  mid=f"m_{uuid.uuid4().hex[:12]}", text=text, ts=time.time()))
        db.cancel_jobs(tid, ("reply", "profile", "nudge"))
        await engine.reply_job(tid)                      # now, instead of after the debounce
        turn = Turn(lead=text, sent=[s for _, s in self.sender.sent[sent0:]], heart=len(self.sender.hearts) > hearts0)
        d = db.one("SELECT * FROM drafts WHERE thread_id=? AND id>? ORDER BY id DESC LIMIT 1", (tid, last_draft))
        t = db.thread(tid)
        if d and d["status"] == "pending":
            turn.held, turn.held_bubbles = d["reason"], json.loads(d["bubbles"])
            if turn.held_bubbles and not t["handoff"]:   # pretend a person approved it, to keep talking
                for b in turn.held_bubbles:
                    db.add_message(tid, "out", "bot", b)
                meta = json.loads(d["meta"] or "{}")
                db.update_thread(tid, last_out_at=time.time(),
                                 links_sent=t["links_sent"] + (1 if meta.get("link") else 0))
                db.run("UPDATE drafts SET status='sandbox' WHERE id=?", (d["id"],))
        t = db.thread(tid)
        turn.stage, turn.slots, turn.handoff = t["stage"], t["slots"], t["handoff"]
        turn.usd = db.spend_since(self.slug, 0) - spent0
        return turn


# --- simulated leads -----------------------------------------------------------------------------------------
LEAD_PROMPT = """You are role-playing a real person DMing an online coach's Instagram account. Stay in character.
Persona: {persona}
Write ONLY your next DM: short and casual like real Instagram DMs (lowercase is fine, the odd typo is fine, no \
emojis unless it fits). Never mention you are role-playing. When the chat is naturally over (you booked, said bye, \
or refused), reply exactly [END]."""


async def lead_reply(persona: str, transcript: list[tuple[str, str]], slug: str) -> str:
    msgs: list[dict] = []
    for who, text in transcript:                   # the lead model sees the coach as "user"
        role = "assistant" if who == "LEAD" else "user"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + text
        else:
            msgs.append({"role": role, "content": text})
    if not msgs or msgs[0]["role"] == "assistant":
        msgs.insert(0, {"role": "user", "content": "(you messaged the coach first)"})
    if msgs[-1]["role"] == "assistant":
        msgs.append({"role": "user", "content": "(no reply yet; send your next message)"})
    out, _ = await llm.text(model=settings.cheap_model, system=LEAD_PROMPT.format(persona=persona), messages=msgs,
                            coach=slug, thread_id=None, purpose="eval_lead", max_tokens=200)
    return out.strip().strip('"')


def grade(p: dict, turns: list[Turn], coach: coaches.Coach) -> dict:
    bot_text = [b for t in turns for b in t.bot]
    joined = "\n".join(bot_text)
    link_turn = next((i + 1 for i, t in enumerate(turns) if any(coach.call_link.split("?")[0] in b for b in t.bot)),
                     None) if coach.call_link else None
    handoff = next((t.handoff for t in turns if t.handoff), "")
    lens = [len(b) for b in bot_text]
    checks = {
        "no_price": not RX_OUT["price"].search(joined),
        "no_guarantee": not RX_OUT["guarantee"].search(joined),
        "no_human_claim": not RX_OUT["human_claim"].search(joined),
        "no_bot_tells": not humanize.tells(bot_text),
        "one_question_per_turn": all(sum(b.count("?") for b in t.bot) <= 1 for t in turns),
    }
    exp = p.get("expect") or {}
    if "link_sent" in exp:
        checks["link_sent" if exp["link_sent"] else "no_link"] = bool(link_turn) == bool(exp["link_sent"])
    if exp.get("max_turns_to_link"):
        checks["link_in_time"] = bool(link_turn) and link_turn <= exp["max_turns_to_link"]
    if "handoff" in exp:
        want = exp["handoff"]
        checks[f"handoff_{want or 'none'}"] = (handoff.startswith(want) if want else not handoff)
    if exp.get("end_stage_in"):
        checks["end_stage"] = turns[-1].stage in exp["end_stage_in"] if turns else False
    return {
        "persona": p["id"], "passed": all(checks.values()), "checks": checks, "turns": len(turns),
        "link_turn": link_turn, "handoff": handoff, "final_stage": turns[-1].stage if turns else "",
        "avg_chars": round(sum(lens) / len(lens)) if lens else 0, "coach_median_chars": coach.style["median_chars"],
        "usd": round(sum(t.usd for t in turns), 4),
        "transcript": [{"lead": t.lead, "bot": t.bot, "held": t.held, "stage": t.stage} for t in turns],
    }


async def run_persona(sb: Sandbox, p: dict, max_turns: int) -> dict:
    tid = sb.new_lead(name=p.get("name", "Sam"), username=p["id"], source=p.get("source", ""))
    transcript: list[tuple[str, str]] = []
    turns: list[Turn] = []
    msg = p.get("opener") or await lead_reply(p["persona"], transcript, sb.slug)
    for _ in range(max_turns):
        if msg == "[END]" or not msg:
            break
        turn = await sb.say(tid, msg)
        turns.append(turn)
        transcript.append(("LEAD", msg))
        transcript += [("YOU", b) for b in turn.bot]
        if turn.handoff:
            break
        msg = await lead_reply(p["persona"], transcript, sb.slug)
    return grade(p, turns, sb.coach)


async def run(slug: str, only: str = "", max_turns: int = 10) -> list[dict]:
    personas = yaml.safe_load((ROOT / "evals" / "personas.yaml").read_text())
    custom = settings.coaches_dir / slug / "personas.yaml"
    if custom.is_file():
        personas += yaml.safe_load(custom.read_text()) or []
    if only:
        personas = [p for p in personas if p["id"] in only.split(",")]
    sb = Sandbox(slug)
    results = []
    for p in personas:
        r = await run_persona(sb, p, max_turns)
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        bad = [k for k, v in r["checks"].items() if not v]
        print(f"{mark}  {p['id']:<16} turns={r['turns']:<2} link@{r['link_turn'] or '-':<3} "
              f"handoff={r['handoff'] or '-':<14} stage={r['final_stage']:<12} ${r['usd']:.4f}"
              + (f"  failed: {', '.join(bad)}" if bad else ""))
    out = ROOT / "data" / "evals" / f"{time.strftime('%Y-%m-%d-%H%M')}-{slug}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    total = sum(r["usd"] for r in results)
    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed · ${total:.4f} · transcripts: {out}")
    return results

