"""Stdlib-only HTTP. No requests, no venv, no install step.

Every provider call goes through here so retry, timeout, and the "never talk to
linkedin.com" guard are enforced in exactly one place.
"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .compliance import assert_allowed_host

UA = "li-search/1.0 (+standalone people-search; no linkedin.com access)"


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__("HTTP %s for %s: %s" % (status, url, body[:400]))
        self.status = status
        self.body = body
        self.url = url


def request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Any = None,
    timeout: int = 45,
    retries: int = 3,
) -> Tuple[int, str]:
    """One HTTP call with bounded retry. Returns (status, text).

    Retries only on 429/5xx and transport errors — a 400 or 401 is a
    configuration mistake and retrying it just burns the provider's rate limit.
    """
    assert_allowed_host(url)
    data = None
    hdrs = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return resp.status, raw.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                if e.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            except Exception:
                pass
            text = raw.decode("utf-8", "replace")
            last = HttpError(e.code, text, url)
            if e.code not in (429, 500, 502, 503, 504):
                raise last
        except Exception as e:  # transport
            last = HttpError(0, str(e), url)
        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))
    raise last  # type: ignore[misc]


def get_json(url: str, **kw) -> Any:
    _, text = request(url, **kw)
    return json.loads(text) if text.strip() else {}


def post_json(url: str, body: Any, **kw) -> Any:
    kw.setdefault("method", "POST")
    _, text = request(url, body=body, **kw)
    return json.loads(text) if text.strip() else {}


def qs(params: Dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(clean)
