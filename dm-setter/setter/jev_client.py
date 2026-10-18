#!/usr/bin/env python3
"""Jev — TypeSafe's System One model — as a harness-ready client. Stdlib only: copy this file into a project.

Jev never writes text. Send `state` plus named, typed questions in ONE request and get typed answers with
probabilities back: a `noul` (probability of yes), a `choice` (one option + every option's probability +
confidence) or a `score` (a position on ordered levels + per-level probabilities + confidence).
Docs (source of truth): https://docs.typesafe.ai/llms.txt — append `.md` to any page path.

The harness contract this client enforces, proven in lead-outreach-system (project/tools/scripts/jev.py):
  * The CALLER owns thresholds and control flow; this client only asks and validates.
  * Any failure (no key, auth/credit, bad request, timeout, malformed answer) raises JevError, and
    `try_ask` returns None, so the caller takes the step's older path. Never a guess.
  * Answers are cached by (model, state, questions): a re-run never pays twice and never flips.
  * Per-decision modes: JEV_<DECISION>=on | shadow | off ; JEV=off kills every decision.
    `shadow` asks and records, and the caller keeps the old verdict.
  * 401/402/403 trip a process-wide breaker: a dead key costs one request, not one per item.

Endpoint, picked by which key is set (override with JEV_ENDPOINT / JEV_MODEL):
  TYPESAFE_API_KEY                        -> https://api.typesafe.ai/v1/systemone      model jev-1.13.0
  OPEN_ROUTER_API_KEY / OPENROUTER_API_KEY -> https://openrouter.ai/api/v1/systemone   model typesafe/jev-1.13
The model is PINNED: aliases (jev-latest) move and silently invalidate tuned thresholds.

Cache: JEV_CACHE (default ~/.cache/jev/answers.db). If state holds PII, point JEV_CACHE at the project's
gitignored data folder.

    from jev_client import Jev, mode, noul, choice, score, trail
    J = Jev("icp")
    a = J.try_ask({"business": {...}}, {"fit": noul("Is `business` a law firm?")}, subject="Acme LLP")
    if a is None: ...older path...
    elif a.p("fit") >= FIT_KEEP: ...

`python3 jev_client.py` runs a live check (one tiny request, ~$0.00001).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

TYPESAFE = ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0")
OPENROUTER = ("https://openrouter.ai/api/v1/systemone", "typesafe/jev-1.13")
PRICE_PER_INPUT_TOKEN = 0.042 / 1_000_000          # output tokens are free (docs.typesafe.ai/models)
RETRY = (408, 409, 429, 500, 502, 503, 504, 529)
CACHE = Path(os.environ.get("JEV_CACHE", Path.home() / ".cache" / "jev" / "answers.db"))


class JevError(RuntimeError):
    """No usable answer: the caller takes the step's older path."""


# --- questions (TypeSafe's request shape) -------------------------------------------------------------------
def noul(instructions: Any, true: Any = None, false: Any = None) -> dict:
    """A yes/no question; the answer is the probability of yes. `true`/`false` say what each answer means."""
    q: dict = {"type": "noul", "instructions": instructions}
    crit = {k: v for k, v in (("true", true), ("false", false)) if v}
    if crit:
        q["criteria"] = crit
    return q


def choice(instructions: Any, criteria: dict) -> dict:
    """One option of `criteria` (option -> description or None; at most 255). Add a no-match option."""
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: Any, levels: list) -> dict:
    """A position on 2-10 ordered `levels` (0 = the first). Threshold it; never interpolate a number from it."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


# --- answers ------------------------------------------------------------------------------------------------
@dataclass
class Asked:
    key: str
    model: str
    answers: dict[str, dict]
    cost: float = 0.0
    cached: bool = False

    def p(self, qid: str) -> float:
        """A noul's probability of yes."""
        return float(self.answers[qid]["noul"])

    def pick(self, qid: str) -> tuple[str, float]:
        """A choice's option and its confidence."""
        a = self.answers[qid]
        return str(a["choice"]), float(a.get("confidence") or 0.0)

    def prob(self, qid: str, option: str) -> float:
        """One option's (or one score level's, as "0", "1", ...) probability."""
        return float((self.answers[qid].get("probabilities") or {}).get(str(option)) or 0.0)

    def level(self, qid: str) -> float:
        """A score's probability-weighted level (0 .. n-1; can land between levels)."""
        return float(self.answers[qid]["score"])

    def confidence(self, qid: str) -> float:
        """0 = even spread, 1 = certain. Nouls carry none, so it is |2p - 1| (the docs' own equivalent)."""
        a = self.answers[qid]
        return abs(2 * a["noul"] - 1) if a["type"] == "noul" else float(a.get("confidence") or 0.0)


def _check(questions: dict, answers: Any) -> dict[str, dict]:
    """Every question answered, with its type and inside its options, or JevError."""
    if not isinstance(answers, dict):
        raise JevError("the response carries no answers")
    out: dict[str, dict] = {}
    for qid, q in questions.items():
        a = answers.get(qid)
        if not isinstance(a, dict) or a.get("type") != q["type"]:
            raise JevError(f"no {q['type']} answer for {qid}")
        probs = {str(k): float(v) for k, v in (a.get("probabilities") or {}).items()}
        if q["type"] == "noul":
            v = a.get("noul")
            if not isinstance(v, (int, float)) or not 0 <= v <= 1:
                raise JevError(f"{qid}: noul {v!r} is not a probability")
            out[qid] = {"type": "noul", "noul": float(v)}
        elif q["type"] == "choice":
            c = a.get("choice")
            if c not in q["criteria"]:
                raise JevError(f"{qid}: {c!r} is not an option")
            out[qid] = {"type": "choice", "choice": c, "confidence": float(a.get("confidence") or 0.0),
                        "probabilities": probs}
        else:
            v = a.get("score")
            if not isinstance(v, (int, float)) or not -1e-6 <= v <= len(q["criteria"]) - 1 + 1e-6:
                raise JevError(f"{qid}: score {v!r} is outside the levels")
            out[qid] = {"type": "score", "score": float(v), "confidence": float(a.get("confidence") or 0.0),
                        "probabilities": probs}
    return out


def cache_key(model: str, state: Any, questions: dict) -> str:
    blob = json.dumps([model, state, questions], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


# --- configuration ------------------------------------------------------------------------------------------
def _dotenv() -> dict[str, str]:
    """KEY=value pairs from the nearest .env at or above the working directory."""
    for d in [Path.cwd(), *Path.cwd().parents]:
        f = d / ".env"
        if f.is_file():
            try:
                return {k: v.strip().strip('"').strip("'")
                        for k, v in re.findall(r"^([A-Z_0-9]+)=(.*)$", f.read_text(), re.M)}
            except OSError:
                return {}
    return {}


def endpoint() -> tuple[str, str, str]:
    """(url, model, key) for the first configured provider; key is '' when none is."""
    env = {**_dotenv(), **os.environ}
    if env.get("TYPESAFE_API_KEY"):
        url, model, key = *TYPESAFE, env["TYPESAFE_API_KEY"]
    else:
        key = env.get("OPEN_ROUTER_API_KEY") or env.get("OPENROUTER_API_KEY") or ""
        url, model = OPENROUTER
    return env.get("JEV_ENDPOINT", url), env.get("JEV_MODEL", model), key.strip()


def mode(decision: str, default: str = "on") -> str:
    """on | shadow | off for one decision (JEV=off wins)."""
    if os.environ.get("JEV", "").lower() == "off":
        return "off"
    m = os.environ.get(f"JEV_{decision.upper()}", default).lower()
    return m if m in ("on", "shadow", "off") else default


_lock = threading.Lock()
_breaker: dict[str, str] = {}          # set once on 401/402/403: every later ask fails fast
_spend: dict[str, list] = {}           # decision -> [requests, cached, cost, failed]


def _db() -> sqlite3.Connection:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(CACHE, timeout=30, check_same_thread=False)
    c.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, decision TEXT, subject TEXT,"
              " model TEXT, answers TEXT, cost REAL, created_at TEXT)")
    return c


# --- the client ---------------------------------------------------------------------------------------------
class Jev:
    """One decision's client. `ask` sends one request, or returns its cached answer."""

    def __init__(self, decision: str, post: Callable[[dict], dict] | None = None, cache: bool = True,
                 timeout: float = 30):
        self.decision = decision
        self.url, self.model, self.key = endpoint()
        self._post = post or self._http
        self._cache = cache
        self._timeout = timeout

    def _http(self, payload: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"Authorization": f"Bearer {self.key}",
                                              "Content-Type": "application/json"})
        last = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode(errors="replace")
                last = f"HTTP {e.code} {body}".strip()
                if e.code in (401, 402, 403):
                    with _lock:
                        _breaker.setdefault("why", f"HTTP {e.code} (auth/credit) from {self.url}")
                    break
                if e.code not in RETRY:
                    break                           # 422 = a malformed question: the body names the field
            except Exception as e:  # noqa: BLE001 (network, timeout, bad JSON)
                last = str(e)[:160]
            time.sleep(1.5 * (attempt + 1))
        raise JevError(last or "no response")

    def ask(self, state: Any, questions: dict, subject: str = "") -> Asked:
        key = cache_key(self.model, state, questions)
        if self._cache:
            with _lock:
                db = _db()
                row = db.execute("SELECT answers, model FROM answers WHERE key=?", (key,)).fetchone()
                db.close()
            if row:
                try:
                    a = Asked(key, row[1], _check(questions, json.loads(row[0])), cached=True)
                    self._count(0.0, cached=True)
                    return a
                except (ValueError, JevError):
                    pass
        if _breaker:
            raise JevError(_breaker["why"])
        if not self.key:
            raise JevError("no TYPESAFE_API_KEY or OPEN_ROUTER_API_KEY")
        payload = self._post({"model": self.model, "state": state, "questions": questions})
        answers = _check(questions, payload.get("answers"))
        usage = payload.get("usage") or {}
        cost = float(usage.get("cost") or 0.0) or (usage.get("input_tokens") or 0) * PRICE_PER_INPUT_TOKEN
        answered_by = payload.get("model") or self.model      # log the versioned id that answered
        self._count(cost)
        if self._cache:
            try:
                with _lock:
                    db = _db()
                    db.execute("INSERT OR REPLACE INTO answers VALUES (?,?,?,?,?,?,?)",
                               (key, self.decision, subject[:300], answered_by, json.dumps(answers), cost,
                                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
                    db.commit()
                    db.close()
            except sqlite3.Error as exc:  # the cache must never break a decision
                print(f"[jev] could not cache the answer: {exc}", file=sys.stderr)
        return Asked(key, answered_by, answers, cost)

    def try_ask(self, state: Any, questions: dict, subject: str = "") -> Asked | None:
        """`ask`, or None on any failure (the caller takes the older path)."""
        try:
            return self.ask(state, questions, subject)
        except JevError as exc:
            with _lock:
                _spend.setdefault(self.decision, [0, 0, 0.0, 0])[3] += 1
            if not getattr(self, "_warned", False):
                self._warned = True
                print(f"[jev] {self.decision}: {exc} (the older path decides)", file=sys.stderr)
            return None

    def ask_many(self, items: list[tuple[Any, dict, str]], workers: int = 8) -> list[Asked | None]:
        """Many independent (state, questions, subject) requests in parallel; each slot is Asked or None.
        8 workers per key: wider pools hit the rate limit (PyModel field lesson), and callers fail open."""
        if not items:
            return []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
            return list(ex.map(lambda it: self.try_ask(*it), items))

    def _count(self, cost: float, cached: bool = False) -> None:
        with _lock:
            s = _spend.setdefault(self.decision, [0, 0, 0.0, 0])
            s[1 if cached else 0] += 1
            s[2] += cost


def spend() -> dict:
    """{decision: {requests, cached, cost_usd, failed}} for this process."""
    with _lock:
        return {d: {"requests": s[0], "cached": s[1], "cost_usd": round(s[2], 6), "failed": s[3]}
                for d, s in _spend.items()}


def trail(path: Path | str | None, decision: str, row: dict) -> None:
    """Append one decision's evidence (inputs, p, old verdict, new verdict) to a JSONL file.
    Shadow mode is only useful if this is written: it is what the go-live comparison reads."""
    if not path:
        return
    try:
        with _lock, open(path, "a") as f:
            f.write(json.dumps({"decision": decision, **row}, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    j = Jev("live_check", cache=False)
    a = j.ask({"items": ["banana", "calibration"]}, {"fruit": noul("Is `items[0]` the name of a fruit?")})
    print(f"ok · {j.url} · answered by {a.model} · fruit? {a.p('fruit'):.2f} · ${a.cost:.6f}")
