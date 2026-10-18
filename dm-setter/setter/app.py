"""The web service: Instagram webhooks, the booking webhook, the Instagram Login connect flow, the dashboard, and
the privacy / data-deletion pages Meta's App Review asks for. Run: python -m setter serve"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import admin, db, engine, instagram, webhook, worker
from . import coach as coaches
from .config import settings

log = logging.getLogger("setter")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.connect()
    stop = asyncio.Event()
    tasks = [asyncio.create_task(worker.jobs_loop(stop)), asyncio.create_task(worker.periodic_loop(stop))]
    log.info("dm-setter up · DRY_RUN=%s · coaches=%s", settings.dry_run, coaches.all_slugs())
    yield
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="dm-setter", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(admin.router)


@app.get("/health")
def health() -> dict:
    q = db.one("SELECT COUNT(*) n FROM jobs WHERE status='queued' AND due_at<=?", (time.time(),))["n"]
    return {"ok": True, "dry_run": settings.dry_run, "enabled": settings.enabled, "overdue_jobs": q}


# --- Instagram webhooks ----------------------------------------------------------------------------------------
@app.get("/webhooks/instagram")
def verify(request: Request):
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and settings.ig_verify_token and \
            hmac.compare_digest(p.get("hub.verify_token", ""), settings.ig_verify_token):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(403, "verify token mismatch")


@app.post("/webhooks/instagram")
async def receive(request: Request):
    body = await request.body()
    if not settings.skip_signature and not webhook.verify(
            body, request.headers.get("x-hub-signature-256", ""), settings.ig_app_secret):
        raise HTTPException(401, "bad signature")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "not json") from None
    for ev in webhook.parse(payload):
        try:
            await engine.ingest(ev)
        except Exception:  # noqa: BLE001 (ack Meta anyway; a 5xx makes it retry the whole batch)
            log.exception("ingest failed for %s", ev.kind)
    return {"ok": True}


# --- bookings (Calendly / Cal.com / GHL / anything that posts JSON) --------------------------------------------
@app.post("/hooks/booking/{secret}")
async def booking(secret: str, request: Request):
    if not settings.booking_secret or not hmac.compare_digest(secret, settings.booking_secret):
        raise HTTPException(404)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "not json") from None
    return {"ok": True, "threads": await engine.booking(payload if isinstance(payload, dict) else {"data": payload})}


# --- connect a coach's Instagram account (Instagram Login) ---------------------------------------------------
def _state(slug: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(settings.ig_app_secret.encode(), f"{slug}.{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    return f"{slug}.{ts}.{sig}"


def _check_state(state: str) -> str | None:
    try:
        slug, ts, sig = state.rsplit(".", 2)
    except ValueError:
        return None
    want = hmac.new(settings.ig_app_secret.encode(), f"{slug}.{ts}".encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(sig, want) or time.time() - int(ts) > 1800:
        return None
    return slug


@app.get("/connect/{slug}")
def connect(slug: str, _: str = Depends(admin.auth)):
    if not coaches.get(slug):
        raise HTTPException(404, f"no coach pack coaches/{slug}/coach.yaml")
    if not (settings.ig_app_id and settings.ig_app_secret and settings.public_base_url):
        raise HTTPException(500, "set IG_APP_ID, IG_APP_SECRET and PUBLIC_BASE_URL first")
    return RedirectResponse(instagram.authorize_url(_state(slug)))


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error_description: str = ""):
    slug = _check_state(state)
    if error_description or not code or not slug:
        raise HTTPException(400, error_description or "bad or expired state: start again from /connect/<coach>")
    try:
        r = await instagram.exchange_code(code.split("#")[0])
        db.save_account(r["user_id"], slug, r["username"], r["token"], r["expires_at"])
        await instagram.Account(r["user_id"], r["token"]).subscribe()
    except instagram.IGError as e:
        raise HTTPException(502, f"Instagram said: {e.message}") from None
    db.audit(None, slug, "account_connected", username=r["username"], ig_user_id=r["user_id"])
    return RedirectResponse(f"/admin?msg=connected @{r['username']} to {slug}", status_code=303)


# --- pages App Review needs ------------------------------------------------------------------------------------
@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return HTMLResponse("""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Privacy</title><body style="font:16px/1.5 system-ui;max-width:720px;margin:40px auto;padding:0 16px">
<h1>Privacy policy</h1><p>This service answers Instagram direct messages on behalf of the coach whose account you
messaged. It stores the messages you exchange with that account, your Instagram username and display name, to continue
the conversation and to book calls you ask for. Replies may be drafted with the help of AI and reviewed by the coach.
Data is not sold or shared with third parties beyond the processors needed to run the service (hosting and the AI
model provider). To delete your data, message the account with the word DELETE or follow the data deletion
instructions at /data-deletion.</p></body>""")


@app.get("/data-deletion", response_class=HTMLResponse)
def data_deletion():
    return HTMLResponse("""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Data deletion</title><body style="font:16px/1.5 system-ui;max-width:720px;margin:40px auto;padding:0 16px">
<h1>Delete your data</h1><p>Send the message <b>DELETE</b> to the Instagram account you were talking to, or email the
coach. Your conversation record is erased within 30 days.</p></body>""")
