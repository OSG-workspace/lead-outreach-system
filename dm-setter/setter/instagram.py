"""Instagram API with Instagram Login (graph.instagram.com): send, typing, seen, react, private replies, profiles,
comments, token exchange/refresh and webhook subscription. Official API only.

Tokens travel as the access_token query parameter and are never logged: errors carry the status and Meta's error
body only, never the request URL.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .config import settings

WEBHOOK_FIELDS = "messages,messaging_postbacks,messaging_seen,message_reactions,messaging_referral,comments"
SCOPES = "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments"
OUTSIDE_WINDOW_SUBCODES = {2018278, 2534022}     # "sent outside of allowed window"


class IGError(RuntimeError):
    def __init__(self, status: int, body: Any):
        err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
        self.status, self.code, self.subcode = status, err.get("code"), err.get("error_subcode")
        self.message = err.get("message") or str(body)[:200]
        super().__init__(f"Instagram {status} code={self.code}/{self.subcode}: {self.message}")

    @property
    def outside_window(self) -> bool:
        return self.subcode in OUTSIDE_WINDOW_SUBCODES or "outside of allowed window" in self.message.lower()

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500 or self.code in (1, 2, 4, 17, 32, 613)


_http: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=20.0)
    return _http


def base() -> str:
    return f"https://{settings.ig_graph_host}/{settings.ig_graph_version}"


async def _req(method: str, url: str, token: str | None = None, *, params: dict | None = None,
               json: dict | None = None, data: dict | None = None) -> dict:
    params = dict(params or {})
    if token:
        params["access_token"] = token
    for attempt in range(3):
        try:
            r = await http().request(method, url, params=params, json=json, data=data)
        except httpx.HTTPError as e:
            if attempt == 2:
                raise IGError(0, {"error": {"message": f"network: {type(e).__name__}"}}) from None
            await asyncio.sleep(1.5 * (attempt + 1))
            continue
        try:
            body = r.json()
        except ValueError:
            body = {"error": {"message": r.text[:200]}}
        if r.status_code < 400 and not (isinstance(body, dict) and "error" in body):
            return body
        err = IGError(r.status_code, body)
        if not err.retryable or attempt == 2:
            raise err
        await asyncio.sleep(2.0 * (attempt + 1))
    raise IGError(0, {})


class Account:
    """One connected professional account."""

    def __init__(self, ig_user_id: str, token: str):
        self.id, self.token = ig_user_id, token

    async def _send(self, payload: dict) -> dict:
        return await _req("POST", f"{base()}/{self.id}/messages", self.token, json=payload)

    async def send_text(self, igsid: str, text: str, human_agent: bool = False) -> str:
        payload: dict = {"recipient": {"id": igsid}, "message": {"text": text[:1000]}}
        if human_agent:
            payload.update(messaging_type="MESSAGE_TAG", tag="HUMAN_AGENT")
        return (await self._send(payload)).get("message_id", "")

    async def action(self, igsid: str, action: str) -> None:
        """typing_on | typing_off | mark_seen. Cosmetic: failures are swallowed."""
        try:
            await self._send({"recipient": {"id": igsid}, "sender_action": action})
        except IGError:
            pass

    async def react(self, igsid: str, mid: str) -> None:
        try:
            await self._send({"recipient": {"id": igsid}, "sender_action": "react",
                              "payload": {"message_id": mid, "reaction": "love"}})
        except IGError:
            pass

    async def private_reply(self, comment_id: str, text: str) -> str:
        r = await self._send({"recipient": {"comment_id": comment_id}, "message": {"text": text[:1000]}})
        return r.get("message_id", "")

    async def public_reply(self, comment_id: str, text: str) -> None:
        await _req("POST", f"{base()}/{comment_id}/replies", self.token, data={"message": text})

    async def profile(self, igsid: str) -> dict:
        return await _req("GET", f"{base()}/{igsid}", self.token,
                          params={"fields": "name,username,is_user_follow_business,follower_count"})

    async def recent_media(self, limit: int = 8) -> list[dict]:
        r = await _req("GET", f"{base()}/{self.id}/media", self.token,
                       params={"fields": "id,timestamp", "limit": limit})
        return r.get("data", [])

    async def comments(self, media_id: str) -> list[dict]:
        r = await _req("GET", f"{base()}/{media_id}/comments", self.token,
                       params={"fields": "id,text,timestamp,from", "limit": 50})
        return r.get("data", [])

    async def subscribe(self) -> dict:
        return await _req("POST", f"{base()}/me/subscribed_apps", self.token,
                          params={"subscribed_fields": WEBHOOK_FIELDS})


# --- OAuth (Instagram Login) ---------------------------------------------------------------------------------
def authorize_url(state: str) -> str:
    q = httpx.QueryParams({"client_id": settings.ig_app_id, "redirect_uri": redirect_uri(),
                           "response_type": "code", "scope": SCOPES, "state": state})
    return f"https://www.instagram.com/oauth/authorize?{q}"


def redirect_uri() -> str:
    return f"{settings.public_base_url}/oauth/callback"


async def exchange_code(code: str) -> dict:
    """code -> short-lived token -> long-lived token (60 days). Returns {token, expires_at, user_id, username}."""
    short = await _req("POST", "https://api.instagram.com/oauth/access_token", data={
        "client_id": settings.ig_app_id, "client_secret": settings.ig_app_secret,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri(), "code": code})
    long = await _req("GET", f"https://{settings.ig_graph_host}/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": settings.ig_app_secret,
        "access_token": short["access_token"]})
    me = await whoami(long["access_token"])
    return {"token": long["access_token"], "expires_at": time.time() + float(long.get("expires_in", 5184000)),
            **me}


async def whoami(token: str) -> dict:
    """The professional account id that webhooks use (user_id) and its username."""
    me = await _req("GET", f"{base()}/me", token, params={"fields": "user_id,username"})
    return {"user_id": str(me.get("user_id") or me.get("id")), "username": me.get("username", "")}


async def refresh(token: str) -> tuple[str, float]:
    r = await _req("GET", f"https://{settings.ig_graph_host}/refresh_access_token",
                   params={"grant_type": "ig_refresh_token", "access_token": token})
    return r["access_token"], time.time() + float(r.get("expires_in", 5184000))
