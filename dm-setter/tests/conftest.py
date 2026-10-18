import copy
import os
import time

import pytest

os.environ["JEV"] = "off"                    # no network in tests: the pattern layer decides
os.environ.setdefault("OPEN_ROUTER_API_KEY", "test-key-not-used")

from setter import coach as coaches  # noqa: E402
from setter import db, engine, llm  # noqa: E402
from setter.config import settings  # noqa: E402
from setter.webhook import Event  # noqa: E402

ACCOUNT = "17840000000000001"


class FakeClaude:
    """Stands in for llm.structured: returns queued answers, records every request."""

    def __init__(self):
        self.answers: list[dict] = []
        self.calls: list[dict] = []

    def queue(self, **kw):
        base = {"bubbles": ["haha appreciate that man", "what's the main thing you wanna change?"],
                "stage": "DISCOVERY", "slots": [], "confidence": 0.9, "handoff": False, "reason": "",
                "send_link": False, "heart": False}
        base.update(kw)
        self.answers.append(base)

    async def __call__(self, **kw):
        self.calls.append(kw)
        ans = self.answers.pop(0) if self.answers else self.answers_default()
        return ans, {"input": 100, "cache_write": 0, "cache_read": 3000, "output": 60, "usd": 0.001, "model": kw["model"]}

    def answers_default(self):
        self.queue()
        return self.answers.pop(0)


@pytest.fixture
def env(monkeypatch):
    db.connect(":memory:")
    settings.dry_run = False
    settings.humanize_timing = False
    settings.enabled = True
    settings.skip_signature = False
    settings.ig_app_secret = "secret"
    settings.ig_verify_token = "verify-me"
    db.save_account(ACCOUNT, "demo-coach", "marcus.hale.fit", "TOKEN", time.time() + 86400 * 50)
    fake = FakeClaude()
    monkeypatch.setattr(llm, "structured", fake)
    sender = engine.CaptureSender()
    monkeypatch.setattr(engine, "SENDER", sender)
    c = coaches.get("demo-coach")
    saved = copy.deepcopy(c.cfg)
    c.cfg["disclosure"]["profile_confirmed"] = True
    yield {"fake": fake, "sender": sender, "coach": c}
    c.cfg.clear()
    c.cfg.update(saved)


_n = [0]


async def lead_says(text: str, igsid: str = "lead1", **kw) -> int:
    _n[0] += 1
    await engine.ingest(Event(kind="message", account_id=ACCOUNT, igsid=igsid, mid=f"mid{_n[0]}", text=text,
                              ts=time.time(), **kw))
    return db.one("SELECT id FROM threads WHERE igsid=?", (igsid,))["id"]
