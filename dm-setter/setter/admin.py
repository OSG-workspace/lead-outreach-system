"""The review dashboard at /admin (HTTP basic auth): drafts to approve or edit, handoffs, every thread's transcript
and state, manual replies, per-thread pause/mode, and what each coach costs (with the cache hit rate)."""

from __future__ import annotations

import html
import json
import secrets
import time
from datetime import datetime
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import db, engine
from . import coach as coaches
from .config import settings

router = APIRouter()
basic = HTTPBasic()
e = html.escape


def auth(request: Request, cred: HTTPBasicCredentials = Depends(basic)) -> str:
    ok = settings.admin_password and secrets.compare_digest(cred.username, settings.admin_user) \
        and secrets.compare_digest(cred.password, settings.admin_password)
    if not ok:
        raise HTTPException(401, "auth required", headers={"WWW-Authenticate": "Basic"})
    if request.method == "POST":                      # same-origin check instead of CSRF tokens
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if origin and urlparse(origin).netloc != request.url.netloc:
            raise HTTPException(403, "cross-origin form post")
    return cred.username


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a66;--line:#e4e2dc;--lead:#eceae4;--me:#1f5f5b;--meink:#fff;
--warn:#a4471d;--ok:#2d6a3e;--chip:#efede7}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--ink:#ecebe6;--mute:#9a9892;--line:#2e2d2a;
--lead:#2a2926;--me:#2f7c76;--chip:#2a2926;--warn:#e08a5e;--ok:#7cc490}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,
BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:inherit}main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px}h1{font-size:20px;margin:0 8px 0 0}
h2{font-size:15px;margin:26px 0 10px;color:var(--mute);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.chip{background:var(--chip);border-radius:99px;padding:2px 10px;font-size:12px}.warn{color:var(--warn)}.ok{color:var(--ok)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;
overflow:hidden}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:14px}
th{color:var(--mute);font-weight:500;font-size:12px}.wrap{overflow-x:auto}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:18px}@media(max-width:820px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.chat{display:flex;flex-direction:column;gap:6px}.b{max-width:78%;padding:7px 12px;border-radius:16px;white-space:pre-wrap;
word-wrap:break-word}.in{background:var(--lead);align-self:flex-start}.out{background:var(--me);color:var(--meink);
align-self:flex-end}.meta{font-size:11px;color:var(--mute);align-self:center}textarea{width:100%;min-height:110px;
font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{font:inherit;border:1px solid var(--line);background:var(--chip);color:var(--ink);border-radius:8px;
padding:6px 12px;cursor:pointer;margin:4px 4px 0 0}button.primary{background:var(--me);color:#fff;border-color:var(--me)}
dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;margin:0;font-size:14px}dt{color:var(--mute)}
.flash{background:var(--chip);padding:8px 12px;border-radius:8px;margin-bottom:12px}
"""


def page(title: str, body: str, flash: str = "") -> HTMLResponse:
    badges = (f'<span class="chip {"warn" if settings.dry_run else "ok"}">'
              f'{"DRY_RUN: nothing is sent" if settings.dry_run else "LIVE"}</span>'
              + ("" if settings.enabled else '<span class="chip warn">SETTER_ENABLED=0</span>'))
    f = f'<div class="flash">{e(flash)}</div>' if flash else ""
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>{CSS}</style></head>
<body><main><header><h1><a href="/admin" style="text-decoration:none">DM setter</a></h1>{badges}</header>{f}{body}
</main></body></html>""")


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = time.time() - ts
    if s < 3600:
        return f"{int(s // 60)}m ago"
    if s < 86400:
        return f"{int(s // 3600)}h ago"
    return datetime.fromtimestamp(ts).strftime("%d %b")


def who(t: dict) -> str:
    return f"@{t['username']}" if t.get("username") else (t.get("name") or f"lead {t['igsid'][-6:]}")


@router.get("/admin", response_class=HTMLResponse)
def overview(_: str = Depends(auth), msg: str = "") -> HTMLResponse:
    day = engine.day_start()
    rows_c = []
    for slug in coaches.all_slugs():
        c = coaches.get(slug)
        acc = db.account_for_coach(slug)
        u = db.one("SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(input),0) i, COALESCE(SUM(cache_read),0) r,"
                   " COALESCE(SUM(cache_write),0) w, COUNT(*) n FROM usage WHERE coach=? AND created_at>=?", (slug, day))
        total_in = u["i"] + u["r"] + u["w"]
        hit = f"{100 * u['r'] / total_in:.0f}%" if total_in else "-"
        n24 = db.one("SELECT COUNT(*) n FROM threads WHERE coach=? AND last_in_at>=?", (slug, time.time() - 86400))["n"]
        booked = db.one("SELECT COUNT(*) n FROM threads WHERE coach=? AND booked_at>=?", (slug, time.time() - 7 * 86400))["n"]
        pend = db.one("SELECT COUNT(*) n FROM drafts d JOIN threads t ON t.id=d.thread_id WHERE t.coach=? AND"
                      " d.status='pending'", (slug,))["n"]
        rows_c.append(f"<tr><td><b>{e(c.name)}</b><br><span class=meta>{e(slug)}</span></td><td>{e(c.mode)}</td>"
                      f"<td>{'@' + e(acc['username']) if acc else '<span class=warn>not connected</span>'}</td>"
                      f"<td>{n24}</td><td>{booked}</td><td>{pend}</td><td>${u['usd']:.3f} · {u['n']} calls</td>"
                      f"<td>{hit}</td></tr>")
    drafts = db.rows("SELECT d.*, t.username, t.name, t.igsid, t.coach, t.stage FROM drafts d JOIN threads t ON"
                     " t.id=d.thread_id WHERE d.status IN ('pending','dry') ORDER BY d.id DESC LIMIT 60")
    dr = "".join(
        f"<tr><td><a href='/admin/t/{d['thread_id']}'>{e(who(d))}</a><br><span class=meta>{e(d['coach'])} · "
        f"{e(d['stage'])}</span></td><td>{e(' / '.join(json.loads(d['bubbles']))[:220]) or '<i>(no text: reply yourself)</i>'}"
        f"</td><td class={'warn' if d['status'] == 'pending' else ''}>{e(d['reason'])[:160]}</td>"
        f"<td>{ago(d['created_at'])}</td></tr>" for d in drafts) or "<tr><td colspan=4>Nothing waiting.</td></tr>"
    threads = db.rows("SELECT * FROM threads ORDER BY COALESCE(last_in_at, created_at) DESC LIMIT 60")
    tr = "".join(
        f"<tr><td><a href='/admin/t/{t['id']}'>{e(who(t))}</a></td><td>{e(t['coach'])}</td><td>{e(t['stage'])}</td>"
        f"<td>{ago(t['last_in_at'])}</td><td>{flags(t)}</td></tr>" for t in threads) or "<tr><td colspan=5>No threads yet.</td></tr>"
    body = f"""<h2>Coaches (today)</h2><div class=wrap><table><tr><th>coach</th><th>mode</th><th>account</th>
<th>active 24h</th><th>booked 7d</th><th>drafts</th><th>model spend</th><th>cache hit</th></tr>{''.join(rows_c)}</table></div>
<h2>Waiting for you</h2><div class=wrap><table><tr><th>lead</th><th>draft</th><th>why held</th><th></th></tr>{dr}</table></div>
<h2>Recent threads</h2><div class=wrap><table><tr><th>lead</th><th>coach</th><th>stage</th><th>last message</th><th></th></tr>{tr}</table></div>"""
    return page("DM setter", body, msg)


def flags(t: dict) -> str:
    out = []
    if t["handoff"]:
        out.append(f"<span class='chip warn'>handoff: {e(t['handoff'])}</span>")
    if t["paused_until"] and t["paused_until"] > time.time():
        out.append("<span class=chip>coach replying</span>")
    if t["hot"]:
        out.append("<span class='chip warn'>hot</span>")
    if t["opted_out"]:
        out.append("<span class=chip>opted out</span>")
    if t["mode"]:
        out.append(f"<span class=chip>{e(t['mode'])}</span>")
    return " ".join(out)


@router.get("/admin/t/{tid}", response_class=HTMLResponse)
def thread_view(tid: int, _: str = Depends(auth), msg: str = "") -> HTMLResponse:
    t = db.thread(tid)
    if not t:
        raise HTTPException(404)
    c = coaches.get(t["coach"])
    chat = []
    for m in db.messages(tid):
        cls = "in" if m["dir"] == "in" else "out"
        tag = "" if m["by"] in ("lead", "bot") else f"<div class=meta>{e(m['by'])}</div>"
        chat.append(f"{tag}<div class='b {cls}' title='{datetime.fromtimestamp(m['created_at']):%d %b %H:%M}'>"
                    f"{e(m['text'])}</div>")
    d = db.pending_draft(tid)
    if not d:
        row = db.one("SELECT id FROM drafts WHERE thread_id=? AND status='dry' ORDER BY id DESC LIMIT 1", (tid,))
        d = db.draft(row["id"]) if row else None
    draft_html = ""
    if d:
        draft_text = "\n\n".join(d["bubbles"])
        draft_html = f"""<div class=card><b>Draft</b> <span class=meta>{e(d['reason'])}</span>
<form method=post action='/admin/draft/{d['id']}/approve'><textarea name=text>{e(draft_text)}</textarea>
<div class=meta>One message per paragraph (blank line between). Editing makes it a human reply.</div>
<button class=primary>Send</button><button formaction='/admin/draft/{d['id']}/reject'>Discard</button></form></div>"""
    state = "".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in [
        ("coach", c.name if c else t["coach"]), ("stage", t["stage"]),
        ("mode", t["mode"] or f"{c.mode if c else '?'} (coach default)"), ("came from", t["source"] or "-"),
        ("follows", {1: "yes", 0: "no"}.get(t["follows"], "?")), ("window", "open" if engine.window_open(t) else "closed"),
        ("links sent", t["links_sent"]), ("booked", ago(t["booked_at"]) if t["booked_at"] else "no"),
    ])
    slots = "".join(f"<dt>{e(k)}</dt><dd>{e(str(v))}</dd>" for k, v in (t["slots"] or {}).items()) or "<dd>-</dd>"
    body = f"""<p><a href=/admin>← all threads</a> &nbsp; <b>{e(who(t))}</b> {flags(t)}</p><div class=grid>
<div class=card><div class=chat>{''.join(chat) or '<i>No messages.</i>'}</div></div><div>
{draft_html}<div class=card style='margin-top:12px'><dl>{state}</dl><h2>Known</h2><dl>{slots}</dl>
{f"<h2>Summary</h2><p>{e(t['summary'])}</p>" if t['summary'] else ''}</div>
<div class=card style='margin-top:12px'><form method=post action='/admin/t/{tid}/send'><b>Reply yourself</b>
<textarea name=text placeholder='One message per paragraph'></textarea><button class=primary>Send as you</button></form>
<form method=post action='/admin/t/{tid}/control'>
<button name=action value=resume>Resume bot</button><button name=action value=pause>Pause 24h</button>
<button name=action value=mode_draft>Draft mode</button><button name=action value=mode_autopilot>Autopilot</button>
<button name=action value=mode_default>Coach default</button><button name=action value=booked>Mark booked</button>
</form></div></div></div>"""
    return page(who(t), body, msg)


@router.post("/admin/draft/{did}/approve")
async def approve(did: int, text: str = Form(""), _: str = Depends(auth)):
    d = db.draft(did)
    edited = [b.strip() for b in text.replace("\r", "").split("\n\n") if b.strip()] if d else None
    result = await engine.approve(did, edited if edited != (d or {}).get("bubbles") else None)
    return RedirectResponse(f"/admin/t/{d['thread_id'] if d else ''}?msg={quote(result)}", status_code=303)


@router.post("/admin/draft/{did}/reject")
def reject(did: int, _: str = Depends(auth)):
    d = db.draft(did)
    db.run("UPDATE drafts SET status='rejected', decided_at=? WHERE id=?", (time.time(), did))
    return RedirectResponse(f"/admin/t/{d['thread_id'] if d else ''}?msg=discarded", status_code=303)


@router.post("/admin/t/{tid}/send")
async def send(tid: int, text: str = Form(""), _: str = Depends(auth)):
    result = await engine.manual_send(tid, text.replace("\r", "")) if text.strip() else "empty message"
    return RedirectResponse(f"/admin/t/{tid}?msg={quote(result)}", status_code=303)


@router.post("/admin/t/{tid}/control")
def control(tid: int, action: str = Form(...), _: str = Depends(auth)):
    t = db.thread(tid)
    if not t:
        raise HTTPException(404)
    changes = {
        "resume": dict(paused_until=0, handoff="", opted_out=0),
        "pause": dict(paused_until=time.time() + 86400),
        "mode_draft": dict(mode="draft"), "mode_autopilot": dict(mode="autopilot"), "mode_default": dict(mode=""),
        "booked": dict(stage="BOOKED", booked_at=time.time()),
    }.get(action)
    if changes:
        db.update_thread(tid, **changes)
        db.audit(tid, t["coach"], f"admin_{action}")
        if action == "resume" and db.unanswered(tid):
            db.schedule_reply(tid, 2, 5)
        if action in ("pause", "booked"):
            db.cancel_jobs(tid)
    return RedirectResponse(f"/admin/t/{tid}?msg={action}", status_code=303)
