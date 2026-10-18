"""Claude calls through OpenRouter, with spend accounting. Every call logs input / cache-write / cache-read / output
tokens and dollars to the usage table, so the dashboard shows what each coach costs and whether the cache is hitting.

OpenRouter's Messages endpoint (/api/v1/messages) speaks the Anthropic request format, so cache breakpoints (with
the 1h TTL), effort, JSON-schema output, thinking and the mid-conversation system note go through unchanged and the
same Claude model writes the same replies. One key (OPEN_ROUTER_API_KEY) also covers Jev and voice notes.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from . import db
from .config import settings

# $ per million tokens: input, output, cache read. Cache writes: 1.25x input (5 min) or 2x input (1 hour).
# Only a fallback: OpenRouter reports the billed cost of every call, and that number is what gets logged.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
# Models that accept {"role": "system"} inside messages (used for the per-turn context note).
MID_SYSTEM_MODELS = {"claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8",
                     "claude-fable-5", "claude-fable-5-1"}
RETRIES = 2                                       # quick in-call retries; the job queue retries again after that


class LLMError(RuntimeError):
    """The call failed; `retryable` says whether trying again later can help."""

    def __init__(self, msg: str, retryable: bool = True):
        super().__init__(msg)
        self.retryable = retryable


class Refused(LLMError):
    def __init__(self, msg: str):
        super().__init__(msg, retryable=False)


def canonical(model: str) -> str:
    """'anthropic/claude-sonnet-5.5', 'claude-sonnet-5-5' and a dated id all -> 'claude-sonnet-5-5'."""
    m = model.strip().split("/")[-1].replace(".", "-")
    return re.sub(r"-\d{8}$", "", m)


def slug(model: str) -> str:
    """The OpenRouter model id: 'claude-sonnet-5-5' -> 'anthropic/claude-sonnet-5.5'. Other vendors pass as given."""
    if "/" in model:
        return model
    return "anthropic/" + re.sub(r"^(claude-[a-z]+-\d+)-(\d+)$", r"\1.\2", canonical(model))


def thinking_param(model: str, mode: str) -> dict | None:
    """None = leave the model's default (adaptive). 'off' maps to each model's own off switch."""
    if mode != "off":
        return None
    m = canonical(model)
    if m == "claude-sonnet-5-5":
        return {"type": "between_tools"}         # no tools here, so: no thinking at all
    if m in ("claude-haiku-5-5", "claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"):
        return {"type": "disabled"}
    return None                                  # Opus 5.5 / Fable cannot turn thinking off: effort controls it


def usage_dict(u: dict) -> dict:
    u = u or {}
    w1h = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    return {
        "input": int(u.get("input_tokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or 0),
        "cache_write_1h": w1h,
        "cache_read": int(u.get("cache_read_input_tokens") or 0),
        "output": int(u.get("output_tokens") or 0),
    }


def cost(model: str, u: dict) -> float:
    pin, pout, pread = PRICES.get(canonical(model), PRICES["claude-sonnet-5-5"])
    w1h = u.get("cache_write_1h", 0)
    w5m = max(0, u.get("cache_write", 0) - w1h)
    usd = (u.get("input", 0) * pin + w5m * pin * 1.25 + w1h * pin * 2 + u.get("cache_read", 0) * pread
           + u.get("output", 0) * pout)
    return usd / 1_000_000


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if not settings.openrouter_key:
            raise LLMError("no OPEN_ROUTER_API_KEY", retryable=False)
        _client = httpx.AsyncClient(base_url=settings.openrouter_base, timeout=60.0, headers={
            "Authorization": f"Bearer {settings.openrouter_key}", "X-Title": "dm-setter"})
    return _client


def _error(status: int, body: str) -> LLMError:
    try:
        msg = (json.loads(body).get("error") or {}).get("message") or body
    except (ValueError, AttributeError):
        msg = body
    msg = str(msg)[:200]
    if status == 402:
        return LLMError("OpenRouter credit balance too low (openrouter.ai/credits)", retryable=False)
    if status in (401, 403):
        return LLMError(f"auth: {msg}", retryable=False)
    if status == 429:
        return LLMError("rate limited", retryable=True)
    if status >= 500:
        return LLMError(f"API {status}", retryable=True)
    return LLMError(f"bad request: {msg}", retryable=False)


async def _create(model: str, *, session: str | None = None, **kw: Any) -> dict:
    body: dict[str, Any] = {"model": slug(model), **kw}
    if settings.llm_provider_order:
        body["provider"] = {"order": settings.llm_provider_order}       # Anthropic first; others only on outage
    if session:
        body["session_id"] = session                                    # same provider every turn = cache hits
    # Refusal / outage fallback. Skipped when thinking is forced off: that switch differs between models.
    fb = settings.llm_fallback_model
    if settings.llm_fallbacks and fb and canonical(fb) != canonical(model) and "thinking" not in kw:
        body["fallbacks"] = [{"model": slug(fb)}]
    c = client()
    for attempt in range(RETRIES + 1):
        try:
            r = await c.post("/v1/messages", json=body)
        except httpx.HTTPError as e:
            err = LLMError("connection error", retryable=True)
            err.__cause__ = e
        else:
            if r.status_code == 200:
                data = r.json()
                if data.get("type") == "error":
                    err = _error(int((data.get("error") or {}).get("code") or 500), r.text)
                else:
                    return data
            else:
                err = _error(r.status_code, r.text)
        if not err.retryable or attempt == RETRIES:
            raise err
        await asyncio.sleep(1.5 * (attempt + 1))
    raise LLMError("unreachable")


def _log(resp: dict, model: str, coach: str, thread_id: int | None, purpose: str) -> dict:
    raw = resp.get("usage") or {}
    u = usage_dict(raw)
    served = resp.get("model") or model
    usd = raw.get("cost")
    usd = float(usd) if isinstance(usd, (int, float)) else cost(served if canonical(served) in PRICES else model, u)
    db.add_usage(coach, thread_id, purpose, served, u, usd)
    return {**u, "usd": usd, "model": served}


def _text(resp: dict) -> str:
    return "".join(b.get("text", "") for b in resp.get("content") or [] if b.get("type") == "text")


def _session(coach: str, thread_id: int | None) -> str | None:
    return f"{coach}:{thread_id}" if thread_id is not None else None


async def structured(*, model: str, system: list[dict], messages: list[dict], schema: dict, coach: str,
                     thread_id: int | None, purpose: str, effort: str = "low", thinking: str = "adaptive",
                     max_tokens: int = 2000) -> tuple[dict, dict]:
    """One JSON-schema-constrained call. Returns (parsed answer, usage)."""
    kw: dict[str, Any] = dict(max_tokens=max_tokens, system=system, messages=messages,
                              output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}})
    tp = thinking_param(model, thinking)
    if tp:
        kw["thinking"] = tp
    resp = await _create(model, session=_session(coach, thread_id), **kw)
    usage = _log(resp, model, coach, thread_id, purpose)
    if resp.get("stop_reason") == "refusal":
        raise Refused(f"refused ({(resp.get('stop_details') or {}).get('category')})")
    if resp.get("stop_reason") == "max_tokens":
        raise LLMError("hit max_tokens", retryable=False)
    text = _text(resp)
    try:
        return json.loads(text), usage
    except json.JSONDecodeError as e:
        raise LLMError(f"unparseable answer: {text[:120]!r}", retryable=False) from e


async def text(*, model: str, system: str | list[dict], messages: list[dict], coach: str, thread_id: int | None,
               purpose: str, effort: str = "low", max_tokens: int = 1200) -> tuple[str, dict]:
    """A plain text call (summaries, photo descriptions, simulated leads). Thinking off where the model allows."""
    kw: dict[str, Any] = dict(max_tokens=max_tokens, system=system, messages=messages,
                              output_config={"effort": effort})
    tp = thinking_param(model, "off")
    if tp:
        kw["thinking"] = tp
    resp = await _create(model, session=_session(coach, thread_id), **kw)
    usage = _log(resp, model, coach, thread_id, purpose)
    if resp.get("stop_reason") == "refusal":
        raise Refused("refused")
    return _text(resp).strip(), usage


async def count_tokens(*, model: str, system: list[dict], messages: list[dict]) -> int:
    """OpenRouter has no token-count endpoint: a 1-token call reports the input size (costs about one turn)."""
    tp = thinking_param(model, "off")
    resp = await _create(model, max_tokens=1, system=system, messages=messages, **({"thinking": tp} if tp else {}))
    u = usage_dict(resp.get("usage") or {})
    return u["input"] + u["cache_write"] + u["cache_read"]
