import json

from fastapi.testclient import TestClient

from setter import db, engine, train
from setter.app import app
from setter.config import settings

from conftest import lead_says


def _mojibake(s: str) -> str:
    return s.encode("utf-8").decode("latin-1")         # how Instagram exports store UTF-8


def _thread(root, name, msgs):
    d = root / "your_instagram_activity" / "messages" / "inbox" / name
    d.mkdir(parents=True)
    data = {"participants": [{"name": "Coach Mike"}, {"name": name.title()}],
            "messages": [{"sender_name": s, "timestamp_ms": 1000 * (len(msgs) - i), "content": _mojibake(t)}
                         for i, (s, t) in enumerate(msgs)]}      # exports list newest first
    (d / "message_1.json").write_text(json.dumps(data))


def test_train_measures_style_and_picks_booked_examples(tmp_path):
    msgs = [("Jake", "yo saw your reel"), ("Coach Mike", "ayy appreciate it man 🔥"),
            ("Coach Mike", "what are you working on rn"), ("Jake", "trying to lose 10kg"),
            ("Coach Mike", "love that, whats been the hardest part"), ("Jake", "time honestly"),
            ("Coach Mike", "makes sense, wanna hop on a quick call"), ("Jake", "sure"),
            ("Coach Mike", "here https://calendly.com/mike/call"), ("Jake", "booked for tuesday Jake out")]
    _thread(tmp_path, "jake", list(reversed(msgs)))
    _thread(tmp_path, "sam", list(reversed([("Sam", "hey"), ("Coach Mike", "ayy whats good"),
                                            ("Coach Mike", "makes sense bro"), ("Sam", "cool")])))
    a = train.analyse(tmp_path, "unit-test", call_link="https://calendly.com/mike/call")
    assert a["me"] == "Coach Mike"
    style = train.style_of(a)
    assert style["lowercase"] and not style["end_period"]
    assert "🔥" in style["top_emojis"]
    assert "ayy" in a["openers"]
    ex = train.pick_examples(a["convos"])
    assert len(ex) == 1 and ex[0].startswith("(outcome: booked)")
    assert "calendly" not in ex[0] and "[link]" in ex[0]
    assert "Jake" not in ex[0]                                   # lead name anonymised


async def test_thread_page_renders_with_draft(env):
    settings.admin_password = "pw"
    tid = await lead_says("hey <script>alert(1)</script>")
    await engine.reply_job(tid)                                  # draft mode -> pending draft
    r = TestClient(app).get(f"/admin/t/{tid}", auth=("admin", "pw"))
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;" in r.text
    assert "Draft" in r.text and db.pending_draft(tid)
