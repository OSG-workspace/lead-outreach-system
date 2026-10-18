"""Tell a person when a thread needs them: handoffs, hot leads, held drafts, bookings, budget stops.
One incoming-webhook URL; the payload carries both Slack ("text") and Discord ("content") keys."""

from __future__ import annotations

import httpx

from .coach import Coach
from .config import settings


async def send(coach: Coach | None, thread: dict | None, text: str) -> None:
    url = (coach.get("notify.webhook_url") if coach else "") or settings.notify_webhook_url
    if not url:
        return
    who = f"@{thread['username']}" if thread and thread.get("username") else (f"thread {thread['id']}" if thread else "")
    link = f" {settings.public_base_url}/admin/t/{thread['id']}" if thread and settings.public_base_url else ""
    msg = f"[{coach.name if coach else 'dm-setter'}] {who}: {text}{link}".strip()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, json={"text": msg, "content": msg[:1900]})
    except httpx.HTTPError:
        pass
