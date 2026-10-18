"""The background loop: runs due jobs (up to WORKERS at once), refreshes tokens daily, and reconciles comments
(Meta's comment webhooks drop events, so recent posts are polled for keyword comments the webhook missed)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from . import db, engine, instagram
from . import coach as coaches
from .config import settings
from .webhook import Event

log = logging.getLogger("setter.worker")
MAX_ATTEMPTS = 4


async def _run(job: dict, sem: asyncio.Semaphore) -> None:
    async with sem:
        try:
            await engine.run_job(job)
            db.finish_job(job["id"])
        except Exception as e:  # noqa: BLE001 (one bad job must never stop the loop)
            log.exception("job %s (%s) failed", job["id"], job["kind"])
            if job["attempts"] + 1 < MAX_ATTEMPTS:
                db.finish_job(job["id"], error=repr(e), retry_at=time.time() + 30 * (job["attempts"] + 1) ** 2)
            else:
                db.finish_job(job["id"], status="failed", error=repr(e))
                db.audit(job["thread_id"], "", "job_failed", kind=job["kind"], error=repr(e))
                t = db.thread(job["thread_id"]) if job["thread_id"] else None
                c = coaches.get(t["coach"]) if t else None
                if job["kind"] == "reply" and t and c:      # never fail silently on a lead's message
                    await engine.hold(c, t, f"automatic reply failed after {MAX_ATTEMPTS} tries: {e}")


async def jobs_loop(stop: asyncio.Event) -> None:
    db.recover_running()
    sem = asyncio.Semaphore(settings.workers)
    tasks: set[asyncio.Task] = set()
    while not stop.is_set():
        free = settings.workers - len(tasks)
        for job in db.claim_due(limit=max(0, free)) if free > 0 else []:
            task = asyncio.create_task(_run(job, sem))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    if tasks:
        await asyncio.wait(tasks, timeout=30)


async def refresh_tokens() -> None:
    """Long-lived Instagram tokens last 60 days; refresh any that expire within 10."""
    for a in db.rows("SELECT * FROM accounts WHERE token_expires_at < ?", (time.time() + 10 * 86400,)):
        try:
            token, exp = await instagram.refresh(a["access_token"])
            db.run("UPDATE accounts SET access_token=?, token_expires_at=? WHERE ig_user_id=?",
                   (token, exp, a["ig_user_id"]))
            db.audit(None, a["coach"], "token_refreshed")
        except instagram.IGError as e:
            db.audit(None, a["coach"], "token_refresh_failed", error=str(e))


async def poll_comments() -> None:
    for a in db.rows("SELECT * FROM accounts"):
        c = coaches.get(a["coach"])
        if not c or not c.cfg.get("comment_triggers"):
            continue
        acct = instagram.Account(a["ig_user_id"], a["access_token"])
        try:
            media = await acct.recent_media()
        except instagram.IGError:
            continue
        cutoff = time.time() - 72 * 3600
        for m in media:
            try:
                if datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00")).timestamp() < cutoff:
                    continue
                comments = await acct.comments(m["id"])
            except (instagram.IGError, KeyError, ValueError):
                continue
            for cm in comments:
                frm = cm.get("from") or {}
                await engine.ingest(Event(kind="comment", account_id=a["ig_user_id"], igsid=str(frm.get("id", "")),
                                          username=frm.get("username", ""), comment_id=str(cm.get("id", "")),
                                          text=cm.get("text", ""), media_id=m["id"], source="comment"))


async def periodic_loop(stop: asyncio.Event) -> None:
    last_refresh = last_poll = 0.0
    while not stop.is_set():
        now = time.time()
        try:
            if now - last_refresh > 86400:
                last_refresh = now
                await refresh_tokens()
            if settings.comment_poll_minutes > 0 and now - last_poll > settings.comment_poll_minutes * 60:
                last_poll = now
                await poll_comments()
        except Exception:  # noqa: BLE001
            log.exception("periodic task failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
