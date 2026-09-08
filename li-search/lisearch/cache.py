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


def key_path(provider: str, payload: Any) -> Path:
    """The on-disk path a (provider, payload) pair caches to — exposed so a
    prune can compute the set of files that are still worth keeping."""
    return _key(provider, payload)


def prune(keep: "set[Path]", ttl: int = DEFAULT_TTL, providers: "tuple[str, ...]" = ("ddgs", "openweb")) -> dict:
    """Delete cached pages that are no longer useful.

    A page is useless when it has EXPIRED (older than ttl) or is ORPHANED —
    no stored audience's query matrix can ever ask for it again (the audience
    was deleted or redefined). Orphan detection applies only to `providers`,
    whose keys are computable from the audiences; other providers' entries are
    pruned on age alone. The operator's standing rule (2026-09-02): once a
    cached page can no longer serve a fire, it must not stay on disk.
    """
    now = time.time()
    out = {"expired": 0, "orphaned": 0, "kept": 0, "skipped": ""}
    files = list(CACHE.rglob("*.json"))
    # Safety: if the keep-set matches NOTHING on disk while pages exist, the
    # keep-set is wrong (a key-shape change), not the cache. Refuse to prune
    # orphans in that case rather than wipe every useful page.
    if files and keep and not any(f in keep for f in files):
        out["skipped"] = "keep-set matched no cached file; orphan prune refused (expiry still applied)"
        providers = ()
    for f in files:
        try:
            at = json.loads(f.read_text(encoding="utf-8")).get("_at", 0)
        except Exception:
            at = 0
        if now - at > ttl:
            f.unlink(missing_ok=True)
            out["expired"] += 1
            continue
        if f.parent.name in providers and f not in keep:
            f.unlink(missing_ok=True)
            out["orphaned"] += 1
            continue
        out["kept"] += 1
    for d in CACHE.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    return out
