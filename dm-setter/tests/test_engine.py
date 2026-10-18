import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from setter import brain, db, engine
from setter.app import app
from setter.config import settings
from setter.webhook import Event

from conftest import ACCOUNT, lead_says


def sent_texts(env):
    return [t for _, t in env["sender"].sent]


async def test_autopilot_reply_goes_out_and_is_mirrored(env):
    tid = await lead_says("[replied to your story] this is me every night 😂")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    assert sent_texts(env) == ["haha appreciate that man", "what's the main thing you wanna change?"]
    out = db.rows("SELECT * FROM messages WHERE thread_id=? AND dir='out'", (tid,))
    assert [m["by"] for m in out] == ["bot", "bot"] and all(m["mid"] for m in out)
    t = db.thread(tid)
    assert t["stage"] == "DISCOVERY" and t["last_out_at"]
    assert db.one("SELECT 1 FROM jobs WHERE kind='nudge' AND thread_id=? AND status='queued'", (tid,))


async def test_debounce_merges_a_burst_into_one_job(env):
    tid = await lead_says("hey")
    await lead_says("quick question")
    await lead_says("how does it work")
    jobs = db.rows("SELECT * FROM jobs WHERE kind='reply' AND thread_id=? AND status='queued'", (tid,))
    assert len(jobs) == 1
    assert len(db.unanswered(tid)) == 3


async def test_draft_mode_holds_then_approve_sends(env):
    tid = await lead_says("hey man")
    await engine.reply_job(tid)                                  # demo coach is in draft mode
    assert sent_texts(env) == []
    d = db.pending_draft(tid)
    assert d and "draft mode" in d["reason"]
    result = await engine.approve(d["id"])
    assert result.startswith("sent 2")
    assert len(sent_texts(env)) == 2


async def test_bot_question_hands_off_without_calling_claude(env):
    tid = await lead_says("wait are you a bot?")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    assert env["fake"].calls == [] and sent_texts(env) == []
    assert db.thread(tid)["handoff"] == "bot_question"
    assert "handoff" in db.pending_draft(tid)["reason"]


async def test_bot_question_disclose_mode_sends_coach_answer(env):
    env["coach"].cfg["disclosure"]["bot_question"] = "disclose"
    tid = await lead_says("is this chatgpt?")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    assert sent_texts(env) == [env["coach"].cfg["disclosure"]["bot_answer"]]
    assert db.thread(tid)["disclosed"] == 1


async def test_coach_typing_in_app_pauses_the_bot(env):
    tid = await lead_says("hey")
    db.update_thread(tid, mode="autopilot")
    await engine.ingest(Event(kind="echo", account_id=ACCOUNT, igsid="lead1", mid="coachmid", text="yo it's me"))
    t = db.thread(tid)
    assert t["paused_until"] > time.time() + 3600
    await lead_says("oh nice")
    await engine.reply_job(tid)
    assert sent_texts(env) == [] and env["fake"].calls == []


async def test_echo_of_our_own_send_does_not_pause(env):
    tid = await lead_says("hey")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    mine = db.one("SELECT mid, text FROM messages WHERE thread_id=? AND dir='out' ORDER BY id LIMIT 1", (tid,))
    await engine.ingest(Event(kind="echo", account_id=ACCOUNT, igsid="lead1", mid=mine["mid"], text=mine["text"]))
    assert db.thread(tid)["paused_until"] == 0


async def test_link_is_gated_until_qualified_then_tracked_and_booked(env):
    tid = await lead_says("how do I sign up for a call")
    db.update_thread(tid, mode="autopilot")
    env["fake"].queue(bubbles=["here you go"], stage="BOOKING", send_link=True)
    await engine.reply_job(tid)
    assert sent_texts(env) == []
    assert "before the lead qualified" in db.pending_draft(tid)["reason"]

    db.update_thread(tid, stage="CALL_PITCH", slots={"goal": "lose 10kg", "current": "96kg, apps", "obstacle": "time",
                                                       "commitment": "9/10"})
    await lead_says("yeah let's do the call")
    env["fake"].queue(bubbles=["here you go"], stage="BOOKING", send_link=True)
    await engine.reply_job(tid)
    texts = sent_texts(env)
    token = db.thread(tid)["token"]
    assert texts[0] == "here you go" and "calendly.com" in texts[1] and token in texts[1]
    assert db.thread(tid)["links_sent"] == 1

    done = await engine.booking({"event": "invitee.created", "payload": {"tracking": {"utm_content": token}}})
    assert done == [tid] and db.thread(tid)["stage"] == "BOOKED"
    assert not db.one("SELECT 1 FROM jobs WHERE kind='nudge' AND thread_id=? AND status='queued'", (tid,))


async def test_link_placeholder_is_never_sent(env):
    tid = await lead_says("ok send it")
    db.update_thread(tid, mode="autopilot", stage="CALL_PITCH",
                     slots={"goal": "g", "current": "c", "obstacle": "o", "commitment": "yes"})
    env["fake"].queue(bubbles=["here you go", "[booking link]"], stage="BOOKING")
    await engine.reply_job(tid)
    texts = sent_texts(env)
    assert "[booking link]" not in " ".join(texts) and any("calendly.com" in t for t in texts)


async def test_price_in_reply_is_held(env):
    tid = await lead_says("how much")
    db.update_thread(tid, mode="autopilot")
    env["fake"].queue(bubbles=["it's $2,500 for 12 weeks"])
    await engine.reply_job(tid)
    assert sent_texts(env) == []
    assert "price" in db.pending_draft(tid)["reason"]


async def test_closed_window_holds(env):
    tid = await lead_says("hey")
    db.update_thread(tid, mode="autopilot", last_in_at=time.time() - 25 * 3600)
    await engine.reply_job(tid)
    assert sent_texts(env) == [] and "window closed" in db.pending_draft(tid)["reason"]


async def test_low_confidence_holds(env):
    tid = await lead_says("hmm")
    db.update_thread(tid, mode="autopilot")
    env["fake"].queue(confidence=0.4)
    await engine.reply_job(tid)
    assert sent_texts(env) == [] and "confidence" in db.pending_draft(tid)["reason"]


async def test_dry_run_sends_nothing(env):
    settings.dry_run = True
    tid = await lead_says("hey")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    assert sent_texts(env) == []
    assert db.one("SELECT status FROM drafts WHERE thread_id=?", (tid,))["status"] == "dry"


async def test_opt_out_stops_everything(env):
    tid = await lead_says("stop")
    await engine.reply_job(tid)
    assert db.thread(tid)["opted_out"] == 1 and env["fake"].calls == []


async def test_delete_erases_the_conversation(env):
    tid = await lead_says("hey")
    await lead_says("DELETE")
    assert db.messages(tid) == [] and db.thread(tid)["opted_out"] == 1


async def test_slots_merge_and_stage_validation(env):
    tid = await lead_says("i want to lose 15kg")
    db.update_thread(tid, mode="autopilot")
    env["fake"].queue(stage="CALL_PITCH", slots=[{"slot": "goal", "value": "lose 15kg"}])
    await engine.reply_job(tid)
    t = db.thread(tid)
    assert t["slots"] == {"goal": "lose 15kg"}
    assert t["stage"] == "QUALIFY"                               # can't pitch with required slots missing


async def test_comment_trigger_queues_private_reply(env):
    await engine.ingest(Event(kind="comment", account_id=ACCOUNT, igsid="L9", username="dan", comment_id="c1",
                              text="PLAN pls", source="comment"))
    job = db.one("SELECT * FROM jobs WHERE kind='comment'")
    assert job and json.loads(job["payload"])["comment_id"] == "c1"
    await engine.ingest(Event(kind="comment", account_id=ACCOUNT, igsid="L9", comment_id="c1", text="PLAN pls"))
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE kind='comment'")["n"] == 1      # deduped


# --- the request layout that makes caching work --------------------------------------------------------------
def _strip(msgs):
    out = []
    for m in msgs:
        content = m["content"] if isinstance(m["content"], str) else [
            {k: v for k, v in b.items() if k != "cache_control"} for b in m["content"]]
        out.append({"role": m["role"], "content": content})
    return out


async def test_prefix_is_stable_between_turns(env):
    c = env["coach"]
    tid = await lead_says("hey")
    db.add_message(tid, "out", "bot", "yo what's up")
    await lead_says("want to lose weight")
    sys1, m1 = brain.build_request(c, db.thread(tid))
    db.add_message(tid, "out", "bot", "nice, how much?")
    await lead_says("like 10kg")
    sys2, m2 = brain.build_request(c, db.thread(tid))

    assert sys1 == sys2 and all("cache_control" in b for b in sys1)
    marks = [i for i, m in enumerate(m1) if isinstance(m["content"], list)
             and any("cache_control" in b for b in m["content"])]
    assert len(marks) == 1 and m1[marks[0]]["role"] == "assistant"
    assert _strip(m2)[: marks[0] + 1] == _strip(m1)[: marks[0] + 1]      # turn 2 re-reads turn 1's prefix
    assert m1[-1]["role"] == "system" and "context" in m1[-1]["content"]  # volatile note is last, uncached
    total_marks = len(sys1) + len(marks)
    assert total_marks <= 4


def test_cost_math():
    from setter import llm
    u = {"input": 1000, "cache_write": 0, "cache_write_1h": 0, "cache_read": 8000, "output": 200}
    assert abs(llm.cost("claude-sonnet-5-5", u) - (1000 * 2 + 8000 * 0.2 + 200 * 10) / 1e6) < 1e-12


# --- HTTP ----------------------------------------------------------------------------------------------------
def test_webhook_verify_and_signed_post(env):
    client = TestClient(app)
    r = client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                  "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"
    assert client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403

    body = json.dumps({"object": "instagram", "entry": [{"id": ACCOUNT, "messaging": [
        {"sender": {"id": "L5"}, "recipient": {"id": ACCOUNT}, "timestamp": int(time.time() * 1000),
         "message": {"mid": "w1", "text": "hello"}}]}]}).encode()
    assert client.post("/webhooks/instagram", content=body).status_code == 401
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/instagram", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200
    assert db.one("SELECT text FROM messages WHERE mid='w1'")["text"] == "hello"


def test_admin_requires_password(env):
    settings.admin_password = "pw"
    client = TestClient(app)
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "pw")).status_code == 200


async def test_service_failure_holds_without_handoff(env, monkeypatch):
    from setter import llm

    async def broken(**kw):
        raise llm.LLMError("OpenRouter credit balance too low (openrouter.ai/credits)", retryable=False)
    monkeypatch.setattr(llm, "structured", broken)
    tid = await lead_says("hey")
    db.update_thread(tid, mode="autopilot")
    await engine.reply_job(tid)
    assert db.thread(tid)["handoff"] == ""                       # the next message gets a normal attempt
    assert "credit balance" in db.pending_draft(tid)["reason"]


async def test_retryable_failure_surfaces_after_last_attempt(env, monkeypatch):
    from setter import llm, worker

    async def flaky(**kw):
        raise llm.LLMError("API 529", retryable=True)
    monkeypatch.setattr(llm, "structured", flaky)
    tid = await lead_says("hey")
    job = db.one("SELECT * FROM jobs WHERE kind='reply' AND thread_id=?", (tid,))
    job["attempts"] = worker.MAX_ATTEMPTS - 1
    import asyncio
    await worker._run(job, asyncio.Semaphore(1))
    assert db.one("SELECT status FROM jobs WHERE id=?", (job["id"],))["status"] == "failed"
    assert "failed after" in db.pending_draft(tid)["reason"]


async def test_one_nudge_per_window(env):
    tid = await lead_says("i want to lose weight")
    db.update_thread(tid, mode="autopilot", stage="DISCOVERY")
    db.add_message(tid, "out", "bot", "nice, how much are you looking to lose?")
    env["fake"].queue(bubbles=["you still thinking about that number?"])
    await engine.nudge_job(tid)
    assert sent_texts(env) == ["you still thinking about that number?"]
    note = env["fake"].calls[-1]["messages"][-1]
    assert note["role"] == "system" and "has not replied" in note["content"]
    await engine.nudge_job(tid)                                  # same window: never a second nudge
    assert len(sent_texts(env)) == 1


async def test_edited_approval_is_a_human_reply(env):
    tid = await lead_says("hey")
    await engine.reply_job(tid)
    d = db.pending_draft(tid)
    await engine.approve(d["id"], ["yo man, what's up"])
    assert sent_texts(env) == ["yo man, what's up"]
    assert db.one("SELECT by FROM messages WHERE thread_id=? AND dir='out'", (tid,))["by"] == "human"


async def test_manual_send_pauses_bot(env):
    tid = await lead_says("hey")
    msg = await engine.manual_send(tid, "hey it's Marcus\n\nwhat's going on?")
    assert sent_texts(env) == ["hey it's Marcus", "what's going on?"] and "paused" in msg
    assert db.thread(tid)["paused_until"] > time.time()


# --- demo account ------------------------------------------------------------------------------------------
DEMO_ACCOUNT = "17840000000000099"


@pytest.fixture
def demo_env(env):
    db.save_account(DEMO_ACCOUNT, "demo-hub", "yourbranddemo", "TOKEN", time.time() + 86400 * 50)
    return env


async def demo_says(text: str, igsid: str = "jane") -> int:
    from setter.webhook import Event as Ev
    await engine.ingest(Ev(kind="message", account_id=DEMO_ACCOUNT, igsid=igsid, mid=f"d{time.time_ns()}",
                           text=text, ts=time.time()))
    return db.one("SELECT id FROM threads WHERE igsid=?", (igsid,))["id"]


async def test_demo_menu_without_code_costs_no_model_call(demo_env):
    tid = await demo_says("hi what is this")
    await engine.reply_job(tid)
    assert demo_env["fake"].calls == []
    assert "DEMO" in demo_env["sender"].sent[-1][1]
    assert db.thread(tid)["coach"] == "demo-hub"


async def test_demo_code_switches_to_the_coach_and_starts_fresh(demo_env):
    tid = await demo_says("hey")
    await engine.reply_job(tid)
    tid = await demo_says("demo marcus")
    t = db.thread(tid)
    assert t["coach"] == "demo-coach" and t["mode"] == "autopilot" and t["stage"] == "OPENER"
    await engine.reply_job(tid)
    call = demo_env["fake"].calls[-1]
    first_user = call["messages"][0]["content"][0]["text"]
    assert first_user == "[opened the chat]"                    # the menu chat and the code are hidden
    assert "Marcus" in call["system"][1]["text"]                 # Marcus's pack, not the hub
    assert demo_env["sender"].sent[-1][1] == "what's the main thing you wanna change?"


async def test_demo_guardrail_sends_a_note_not_the_flagged_text(demo_env):
    tid = await demo_says("DEMO MARCUS")
    demo_env["fake"].queue(bubbles=["it's $2000 and I guarantee you'll lose 20kg"])
    await engine.reply_job(tid)
    last = demo_env["sender"].sent[-1][1]
    assert last.startswith("[demo note:") and "$2000" not in last
    assert not db.pending_draft(tid)


async def test_demo_reset_and_unknown_code(demo_env):
    tid = await demo_says("DEMO MARCUS")
    await engine.reply_job(tid)
    tid = await demo_says("RESET")
    assert db.thread(tid)["coach"] == "demo-hub"
    await engine.reply_job(tid)
    tid = await demo_says("DEMO NOPE1")
    await engine.reply_job(tid)
    assert "NOPE1" in demo_env["sender"].sent[-1][1]
    assert db.thread(tid)["coach"] == "demo-hub"
