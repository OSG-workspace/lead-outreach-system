"""SQLite state: accounts, threads, the mirrored message log, the job queue, drafts, audit and token spend.

Meta's API only returns the last 20 messages of a conversation, so every message in and out is mirrored here.
The job table is the queue: debounced replies, nudges and profile lookups survive a restart.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  ig_user_id TEXT PRIMARY KEY, coach TEXT NOT NULL, username TEXT, access_token TEXT,
  token_expires_at REAL, connected_at REAL);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, coach TEXT NOT NULL, account_id TEXT NOT NULL, igsid TEXT NOT NULL,
  username TEXT DEFAULT '', name TEXT DEFAULT '', follows INTEGER, source TEXT DEFAULT '',
  stage TEXT DEFAULT 'OPENER', slots TEXT DEFAULT '{}', summary TEXT DEFAULT '', summary_upto INTEGER DEFAULT 0,
  mode TEXT DEFAULT '', paused_until REAL DEFAULT 0, handoff TEXT DEFAULT '', hot INTEGER DEFAULT 0,
  opted_out INTEGER DEFAULT 0, token TEXT UNIQUE, links_sent INTEGER DEFAULT 0, disclosed INTEGER DEFAULT 0,
  last_in_at REAL, last_out_at REAL, nudged_for REAL DEFAULT 0, booked_at REAL, created_at REAL,
  UNIQUE(account_id, igsid));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, dir TEXT NOT NULL, by TEXT NOT NULL,
  mid TEXT UNIQUE, text TEXT DEFAULT '', kind TEXT DEFAULT 'text', media_url TEXT DEFAULT '', created_at REAL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, thread_id INTEGER, due_at REAL, first_at REAL,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, payload TEXT DEFAULT '{}', error TEXT DEFAULT '',
  updated_at REAL);
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, due_at);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, bubbles TEXT DEFAULT '[]', reason TEXT DEFAULT '',
  confidence REAL, status TEXT DEFAULT 'pending', meta TEXT DEFAULT '{}', created_at REAL, decided_at REAL);
CREATE INDEX IF NOT EXISTS drafts_status ON drafts(status, thread_id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, thread_id INTEGER, coach TEXT, event TEXT, data TEXT DEFAULT '{}', created_at REAL);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY, coach TEXT, thread_id INTEGER, purpose TEXT, model TEXT, input INTEGER,
  cache_write INTEGER, cache_read INTEGER, output INTEGER, usd REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS comments (
  comment_id TEXT PRIMARY KEY, account_id TEXT, media_id TEXT, igsid TEXT, text TEXT, action TEXT, created_at REAL);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def now() -> float:
    return time.time()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open (or reopen, for tests) the database. ':memory:' works."""
    global _conn
    p = str(path or settings.db_path)
    if p != ":memory:":
        Path(p).parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        if p != ":memory:":
            _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def conn() -> sqlite3.Connection:
    return _conn or connect()


def rows(sql: str, args: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple | list = ()) -> dict | None:
    with _lock:
        r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql: str, args: tuple | list = ()) -> int:
    """Execute; return lastrowid (inserts) or rowcount."""
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


# --- accounts ----------------------------------------------------------------------------------------------
def save_account(ig_user_id: str, coach: str, username: str, token: str, expires_at: float) -> None:
    run("INSERT INTO accounts VALUES (?,?,?,?,?,?) ON CONFLICT(ig_user_id) DO UPDATE SET coach=excluded.coach,"
        " username=excluded.username, access_token=excluded.access_token,"
        " token_expires_at=excluded.token_expires_at", (ig_user_id, coach, username, token, expires_at, now()))


def account(ig_user_id: str) -> dict | None:
    return one("SELECT * FROM accounts WHERE ig_user_id=?", (ig_user_id,))


def account_for_coach(coach: str) -> dict | None:
    return one("SELECT * FROM accounts WHERE coach=? ORDER BY connected_at DESC LIMIT 1", (coach,))


# --- threads -----------------------------------------------------------------------------------------------
def thread(thread_id: int) -> dict | None:
    t = one("SELECT * FROM threads WHERE id=?", (thread_id,))
    if t:
        t["slots"] = json.loads(t["slots"] or "{}")
    return t


def get_or_create_thread(coach: str, account_id: str, igsid: str) -> tuple[dict, bool]:
    t = one("SELECT id FROM threads WHERE account_id=? AND igsid=?", (account_id, igsid))
    if t:
        return thread(t["id"]), False
    tid = run("INSERT INTO threads (coach, account_id, igsid, token, created_at) VALUES (?,?,?,?,?)",
              (coach, account_id, igsid, "st_" + secrets.token_hex(6), now()))
    return thread(tid), True


def update_thread(thread_id: int, **fields: Any) -> None:
    if "slots" in fields and not isinstance(fields["slots"], str):
        fields["slots"] = json.dumps(fields["slots"], ensure_ascii=False, sort_keys=True)
    sets = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE threads SET {sets} WHERE id=?", (*fields.values(), thread_id))


# --- messages ----------------------------------------------------------------------------------------------
def add_message(thread_id: int, dir: str, by: str, text: str, mid: str | None = None, kind: str = "text",
                media_url: str = "", at: float | None = None) -> int | None:
    """Mirror one message. A duplicate mid (webhook retry) returns None."""
    try:
        return run("INSERT INTO messages (thread_id, dir, by, mid, text, kind, media_url, created_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", (thread_id, dir, by, mid, text, kind, media_url, at or now()))
    except sqlite3.IntegrityError:
        return None


def messages(thread_id: int, after_id: int = 0) -> list[dict]:
    return rows("SELECT * FROM messages WHERE thread_id=? AND id>? AND kind!='deleted' ORDER BY id",
                (thread_id, after_id))


def unanswered(thread_id: int) -> list[dict]:
    """Inbound messages after the last outbound one: the burst to answer."""
    last = one("SELECT MAX(id) AS m FROM messages WHERE thread_id=? AND dir='out'", (thread_id,))["m"] or 0
    return rows("SELECT * FROM messages WHERE thread_id=? AND dir='in' AND id>? AND kind!='deleted' ORDER BY id",
                (thread_id, last))


def last_inbound_id(thread_id: int) -> int:
    return one("SELECT MAX(id) AS m FROM messages WHERE thread_id=? AND dir='in'", (thread_id,))["m"] or 0


# --- jobs --------------------------------------------------------------------------------------------------
def schedule_reply(thread_id: int, debounce: float, max_wait: float) -> None:
    """Debounce: every new inbound message pushes the reply back, never past first message + max_wait."""
    t = now()
    j = one("SELECT id, first_at FROM jobs WHERE kind='reply' AND thread_id=? AND status='queued'", (thread_id,))
    if j:
        run("UPDATE jobs SET due_at=?, updated_at=? WHERE id=?",
            (min(t + debounce, (j["first_at"] or t) + max_wait), t, j["id"]))
    else:
        run("INSERT INTO jobs (kind, thread_id, due_at, first_at, updated_at) VALUES ('reply',?,?,?,?)",
            (thread_id, t + debounce, t, t))


def schedule(kind: str, thread_id: int | None, due_at: float, payload: dict | None = None,
             replace: bool = True) -> int:
    if replace and thread_id is not None:
        run("UPDATE jobs SET status='cancelled' WHERE kind=? AND thread_id=? AND status='queued'", (kind, thread_id))
    return run("INSERT INTO jobs (kind, thread_id, due_at, first_at, payload, updated_at) VALUES (?,?,?,?,?,?)",
               (kind, thread_id, due_at, now(), json.dumps(payload or {}), now()))


def cancel_jobs(thread_id: int, kinds: tuple[str, ...] = ("reply", "nudge")) -> None:
    q = ",".join("?" * len(kinds))
    run(f"UPDATE jobs SET status='cancelled' WHERE thread_id=? AND status='queued' AND kind IN ({q})",
        (thread_id, *kinds))


def claim_due(limit: int = 20) -> list[dict]:
    with _lock:
        # One job per thread at a time: a second reply job waits until the first has finished sending.
        due = rows("SELECT * FROM jobs WHERE status='queued' AND due_at<=? AND (thread_id IS NULL OR thread_id NOT IN"
                   " (SELECT thread_id FROM jobs WHERE status='running' AND thread_id IS NOT NULL))"
                   " ORDER BY due_at LIMIT ?", (now(), limit))
        seen: set = set()
        due = [j for j in due if j["thread_id"] is None or not (j["thread_id"] in seen or seen.add(j["thread_id"]))]
        for j in due:
            run("UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?", (now(), j["id"]))
    return due


def finish_job(job_id: int, status: str = "done", error: str = "", retry_at: float | None = None) -> None:
    if retry_at:
        run("UPDATE jobs SET status='queued', due_at=?, error=?, updated_at=? WHERE id=?",
            (retry_at, error[:500], now(), job_id))
    else:
        run("UPDATE jobs SET status=?, error=?, updated_at=? WHERE id=?", (status, error[:500], now(), job_id))


def recover_running() -> None:
    """After a crash, jobs left 'running' go back on the queue."""
    run("UPDATE jobs SET status='queued' WHERE status='running'")


# --- drafts ------------------------------------------------------------------------------------------------
def add_draft(thread_id: int, bubbles: list[str], reason: str, confidence: float | None, meta: dict,
              status: str = "pending") -> int:
    supersede_drafts(thread_id)
    return run("INSERT INTO drafts (thread_id, bubbles, reason, confidence, status, meta, created_at)"
               " VALUES (?,?,?,?,?,?,?)", (thread_id, json.dumps(bubbles, ensure_ascii=False), reason, confidence,
                                           status, json.dumps(meta, ensure_ascii=False, default=str), now()))


def supersede_drafts(thread_id: int) -> None:
    run("UPDATE drafts SET status='superseded', decided_at=? WHERE thread_id=? AND status='pending'",
        (now(), thread_id))


def draft(draft_id: int) -> dict | None:
    d = one("SELECT * FROM drafts WHERE id=?", (draft_id,))
    if d:
        d["bubbles"] = json.loads(d["bubbles"])
        d["meta"] = json.loads(d["meta"] or "{}")
    return d


def pending_draft(thread_id: int) -> dict | None:
    d = one("SELECT id FROM drafts WHERE thread_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (thread_id,))
    return draft(d["id"]) if d else None


# --- audit + spend -----------------------------------------------------------------------------------------
def audit(thread_id: int | None, coach: str, event: str, **data: Any) -> None:
    run("INSERT INTO audit (thread_id, coach, event, data, created_at) VALUES (?,?,?,?,?)",
        (thread_id, coach, event, json.dumps(data, ensure_ascii=False, default=str), now()))


def add_usage(coach: str, thread_id: int | None, purpose: str, model: str, u: dict, usd: float) -> None:
    run("INSERT INTO usage (coach, thread_id, purpose, model, input, cache_write, cache_read, output, usd,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (coach, thread_id, purpose, model, u.get("input", 0), u.get("cache_write", 0), u.get("cache_read", 0),
         u.get("output", 0), usd, now()))


def spend_since(coach: str | None, since: float) -> float:
    if coach:
        r = one("SELECT COALESCE(SUM(usd),0) AS s FROM usage WHERE coach=? AND created_at>=?", (coach, since))
    else:
        r = one("SELECT COALESCE(SUM(usd),0) AS s FROM usage WHERE created_at>=?", (since,))
    return float(r["s"])
