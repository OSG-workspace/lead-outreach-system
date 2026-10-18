"""Voice notes -> text (OpenRouter's Whisper endpoint on the same key), photos -> one sentence (cheap Claude model).

Each is done once and stored in the message log as text, so the reply model never re-reads audio or images on
later turns: a photo costs a few hundred cheap-model tokens one time instead of image tokens on every turn.
"""

from __future__ import annotations

import base64

import httpx

from . import db, llm
from .config import settings

DESCRIBE_PROMPT = ("A lead sent this photo to an online coach in an Instagram DM. Describe it in one short sentence "
                   "the coach's setter can react to: what it shows, plus any visible text. No preamble.")


async def _download(url: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "application/octet-stream").split(";")[0]


async def transcribe(url: str) -> str:
    if not settings.stt_api_key:
        return ""
    data, ctype = await _download(url)
    ext = {"audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/wav": "wav",
           "audio/aac": "aac"}.get(ctype, "mp4")
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(settings.stt_api_url, headers={"Authorization": f"Bearer {settings.stt_api_key}"},
                         data={"model": settings.stt_model}, files={"file": (f"voice.{ext}", data, ctype)})
        r.raise_for_status()
        return (r.json().get("text") or "").strip()


async def describe(url: str, coach: str, thread_id: int) -> str:
    data, ctype = await _download(url)
    if ctype not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        ctype = "image/jpeg"
    out, _ = await llm.text(model=settings.cheap_model, system=DESCRIBE_PROMPT, coach=coach, thread_id=thread_id,
                            purpose="photo", max_tokens=120, messages=[{"role": "user", "content": [
                                {"type": "image", "source": {"type": "base64", "media_type": ctype,
                                                             "data": base64.b64encode(data).decode()}},
                                {"type": "text", "text": "Describe it."}]}])
    return out


async def enrich(coach: str, thread_id: int, burst: list[dict]) -> list[str]:
    """Fill in transcripts and descriptions. Returns problems a person must look at (e.g. an unheard voice note)."""
    problems = []
    for m in burst:
        if not m.get("media_url") or m["kind"] not in ("audio", "image"):
            continue
        if m["kind"] == "audio" and m["text"].startswith("[voice note]") and "said:" not in m["text"]:
            try:
                said = await transcribe(m["media_url"])
            except (httpx.HTTPError, ValueError):
                said = ""
            if said:
                db.run("UPDATE messages SET text=? WHERE id=?", (f"[voice note] said: {said}", m["id"]))
            else:
                problems.append("voice note not transcribed (no STT key or it failed): listen in the app")
        elif m["kind"] == "image" and m["text"].startswith("[photo]") and "shows:" not in m["text"]:
            try:
                what = await describe(m["media_url"], coach, thread_id)
            except (httpx.HTTPError, llm.LLMError):
                what = ""
            if what:
                rest = m["text"][len("[photo]"):].strip()
                db.run("UPDATE messages SET text=? WHERE id=?", (f"[photo] shows: {what} {rest}".strip(), m["id"]))
    return problems
