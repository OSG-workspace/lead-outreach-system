"""TTL cache keyed on (provider, query). Credits are the scarce resource here.

Two TTLs, because the brief's freshness argument cuts both ways: stable
identity fields are worth caching for weeks, job-change signals are worth
nothing the moment they are cached. `fire` uses the long TTL; anything asking
for a change signal must pass ttl=0.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
DEFAULT_TTL = 30 * 86400


def _key(provider: str, payload: Any) -> Path:
    h = hashlib.sha256(
        (provider + "|" + json.dumps(payload, sort_keys=True, default=str)).encode("utf-8")
    ).hexdigest()[:32]
    return CACHE / provider / ("%s.json" % h)


def get(provider: str, payload: Any, ttl: int = DEFAULT_TTL) -> Optional[Any]:
    if ttl <= 0:
        return None
    p = _key(provider, payload)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - blob.get("_at", 0) > ttl:
        return None
    return blob.get("value")


def put(provider: str, payload: Any, value: Any) -> None:
    p = _key(provider, payload)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"_at": time.time(), "value": value}, default=str), encoding="utf-8")


def age_days(provider: str, payload: Any) -> Optional[int]:
    p = _key(provider, payload)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return int((time.time() - blob.get("_at", 0)) // 86400)


def clear() -> int:
    n = 0
    for f in CACHE.rglob("*.json"):
        f.unlink()
        n += 1
    return n
