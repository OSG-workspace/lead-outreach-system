"""python -m setter <command>

  serve [--host H] [--port P]       run the webhook server, worker and dashboard
  check                             what is configured, what is missing, which coaches are ready
  new-coach <slug>                  start a coach pack from coaches/_template
  train <slug> <export> [--me NAME] [--draft-voice]   learn the coach's voice from an Instagram export
  connect-token <slug> <token>      attach an account with a token from the Meta App Dashboard (no OAuth)
  chat <slug>                       be the lead: talk to the setter in the terminal (real Claude, no Instagram)
  eval <slug> [--only a,b] [--turns N]   simulated leads against the setter, graded
  demo-pack <slug> --code C --name N --url U [--notes F]   draft a demo coach pack from their site / transcripts
  demo-codes                        list the demo hub and every DEMO <code>
  prompt <slug> [--count]           print the cached prefix (and count its tokens)
  stats [--days N]                  spend, cache hit rate, drafts and bookings per coach
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time

from . import db
from . import coach as coaches
from .config import ROOT, settings


def cmd_serve(a) -> None:
    import logging

    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("setter.app:app", host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")


def cmd_check(a) -> None:
    def row(ok: bool, what: str, hint: str = "") -> None:
        print(f"  {'ok ' if ok else '-- '} {what}" + (f"   ({hint})" if hint and not ok else ""))
    print("Server")
    row(bool(settings.public_base_url.startswith("https://")), f"PUBLIC_BASE_URL = {settings.public_base_url or '(unset)'}",
        "Meta needs a public https URL for webhooks")
    row(bool(settings.admin_password), "ADMIN_PASSWORD", "the dashboard refuses every login without it")
    print(f"  {'!! ' if settings.dry_run else 'ok '} DRY_RUN = {settings.dry_run}" +
          ("   (nothing reaches Instagram; set DRY_RUN=0 to go live)" if settings.dry_run else ""))
    print("AI (one key: Claude, Jev guardrails and voice notes)")
    row(bool(settings.openrouter_key), "OPEN_ROUTER_API_KEY", "openrouter.ai → Keys (the account needs credit)")
    print(f"      reply model {settings.reply_model} (effort {settings.reply_effort}) · cheap model {settings.cheap_model}")
    print(f"      voice notes {settings.stt_model} · fallback {settings.llm_fallback_model if settings.llm_fallbacks else 'off'}")
    print("Instagram")
    row(bool(settings.ig_app_id), "IG_APP_ID", "Meta app → Instagram → API setup with Instagram login")
    row(bool(settings.ig_app_secret), "IG_APP_SECRET", "same page; also signs every webhook")
    row(bool(settings.ig_verify_token), "IG_VERIFY_TOKEN", "any random string; paste the same in the webhook form")
    print(f"      webhook URL: {settings.public_base_url or 'https://<host>'}/webhooks/instagram")
    print("Optional")
    from .jev_client import endpoint
    row(bool(endpoint()[2]), "Jev guardrails (same key)", "guardrails fall back to patterns")
    row(bool(settings.stt_api_key), "voice notes (same key)", "voice notes go to a person instead")
    row(bool(settings.notify_webhook_url), "NOTIFY_WEBHOOK_URL", "no Slack/Discord pings for handoffs")
    row(bool(settings.booking_secret), "BOOKING_WEBHOOK_SECRET", "bookings won't be detected automatically")
    print("Coaches")
    db.connect()
    for slug in coaches.all_slugs():
        c = coaches.get(slug)
        acc = db.account_for_coach(slug)
        issues = []
        if not c.call_link:
            issues.append("no call.link")
        if not c.voice:
            issues.append("no voice.md")
        if not c.offer:
            issues.append("no offer.md")
        if c.mode == "autopilot" and c.get("disclosure.mode", "profile") == "profile" and not c.get("disclosure.profile_confirmed"):
            issues.append("autopilot but disclosure.profile_confirmed is false (replies will be held)")
        conn = f"@{acc['username']}" if acc else "not connected"
        print(f"  {slug:<18} mode={c.mode:<9} {conn:<20} examples={len(c.examples)}"
              + (f"  ! {'; '.join(issues)}" if issues else ""))


def cmd_new_coach(a) -> None:
    dst = settings.coaches_dir / a.slug
    if dst.exists():
        sys.exit(f"{dst} already exists")
    shutil.copytree(settings.coaches_dir / "_template", dst)
    print(f"created {dst}\nnext: fill coach.yaml, offer.md, script.md; then `python -m setter train {a.slug} <export.zip>`")


def cmd_train(a) -> None:
    from pathlib import Path

    from . import train
    c = coaches.get(a.slug)
    if not c:
        sys.exit(f"no coach pack coaches/{a.slug} (run new-coach first)")
    src = Path(a.export).expanduser()
    an = train.analyse(src, a.slug, me=a.me or "", call_link=c.call_link)
    if not an["lines"]:
        sys.exit(f"found {an['threads']} threads but no messages from {an['me']!r}; pass --me \"Exact Display Name\"")
    style = train.style_of(an)
    examples = train.pick_examples(an["convos"])
    samples = train.write(a.slug, an, style, examples)
    booked = sum(1 for x in an["convos"] if x["outcome"] == "booked")
    print(f"owner: {an['me']} · {an['threads']} threads · {len(an['lines'])} of their messages · "
          f"{len(an['convos'])} reached the booking link ({booked} confirmed booked)")
    print(f"style.yaml: {style}")
    print(f"examples written: {len(examples)} · samples: {samples}")
    if a.draft_voice:
        asyncio.run(train.draft_voice(a.slug, samples))


def cmd_demo_pack(a) -> None:
    from pathlib import Path

    from . import demopack
    if not a.url and not a.notes:
        sys.exit("pass at least one --url (their site, sales page) or --notes file (video transcript, notes)")
    asyncio.run(demopack.build(a.slug, name=a.name, handle=a.handle or a.slug, code=a.code, urls=a.url,
                               notes=[Path(n).expanduser() for n in a.notes]))


def cmd_demo_codes(a) -> None:

    db.connect()
    for slug in coaches.all_slugs():
        c = coaches.get(slug)
        if c and c.get("demo.hub"):
            acc = db.account_for_coach(slug)
            print(f"hub: {slug} on {'@' + acc['username'] if acc else 'NOT CONNECTED (connect the demo account here)'}")
    for slug in coaches.all_slugs():
        c = coaches.get(slug)
        if c and c.cfg.get("demo_code"):
            print(f"  DEMO {str(c.cfg['demo_code']).upper():<12} -> {slug} ({c.name})")



def cmd_connect_token(a) -> None:
    from . import instagram

    async def go():
        me = await instagram.whoami(a.token)
        db.connect()
        db.save_account(me["user_id"], a.slug, me["username"], a.token, time.time() + 55 * 86400)
        await instagram.Account(me["user_id"], a.token).subscribe()
        print(f"connected @{me['username']} (ig user id {me['user_id']}) to {a.slug}; webhooks subscribed")
    asyncio.run(go())


def cmd_chat(a) -> None:
    from .evals import Sandbox
    sb = Sandbox(a.slug)
    tid = sb.new_lead(name=a.name, username="you", source=a.source)
    print(f"You are a lead DMing {sb.coach.name}. /state shows what the setter knows, /new starts over, /quit exits.\n")

    async def loop():
        nonlocal tid
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text:
                continue
            if text == "/quit":
                return
            if text == "/new":
                tid = sb.new_lead(name=a.name, username="you", source=a.source)
                print("(new conversation)\n")
                continue
            if text == "/state":
                t = db.thread(tid)
                print(f"  stage={t['stage']} slots={t['slots']} hot={t['hot']} handoff={t['handoff'] or '-'}\n")
                continue
            turn = await sb.say(tid, text)
            for b in turn.bot:
                print(f"{sb.coach.first_name.lower()}> {b}")
            if turn.heart:
                print("  (hearted your message)")
            notes = [f"stage {turn.stage}", f"${turn.usd:.4f}"]
            if turn.held:
                notes.insert(0, f"HELD: {turn.held}")
            if turn.handoff:
                notes.insert(0, f"HANDOFF: {turn.handoff}")
            print(f"  [{' · '.join(notes)}]\n")
    asyncio.run(loop())


def cmd_eval(a) -> None:
    from . import evals
    asyncio.run(evals.run(a.slug, only=a.only or "", max_turns=a.turns))


def cmd_prompt(a) -> None:
    from . import brain
    c = coaches.get(a.slug)
    if not c:
        sys.exit("no such coach")
    blocks = brain.system_blocks(c)
    for b in blocks:
        print(b["text"], "\n" + "-" * 80)
    chars = sum(len(b["text"]) for b in blocks)
    print(f"cached prefix: {chars} chars (~{chars // 4} tokens est.)")
    if a.count:
        from . import llm
        n = asyncio.run(llm.count_tokens(model=settings.reply_model, system=blocks,
                                         messages=[{"role": "user", "content": "hey"}]))
        print(f"counted: {n} tokens for {settings.reply_model} (a 1-token call, billed like one turn)")


def cmd_stats(a) -> None:
    db.connect()
    since = time.time() - a.days * 86400
    print(f"last {a.days} day(s)")
    for slug in coaches.all_slugs():
        u = db.one("SELECT COALESCE(SUM(usd),0) usd, COALESCE(SUM(input),0) i, COALESCE(SUM(cache_read),0) r,"
                   " COALESCE(SUM(cache_write),0) w, COALESCE(SUM(output),0) o, COUNT(*) n FROM usage"
                   " WHERE coach=? AND created_at>=?", (slug, since))
        tot = u["i"] + u["r"] + u["w"]
        convs = db.one("SELECT COUNT(*) n FROM threads WHERE coach=? AND last_in_at>=?", (slug, since))["n"]
        booked = db.one("SELECT COUNT(*) n FROM threads WHERE coach=? AND booked_at>=?", (slug, since))["n"]
        sent = db.one("SELECT COUNT(*) n FROM messages m JOIN threads t ON t.id=m.thread_id WHERE t.coach=? AND"
                      " m.dir='out' AND m.by='bot' AND m.created_at>=?", (slug, since))["n"]
        held = db.one("SELECT COUNT(*) n FROM drafts d JOIN threads t ON t.id=d.thread_id WHERE t.coach=? AND"
                      " d.created_at>=?", (slug, since))["n"]
        print(f"  {slug:<16} convs={convs:<4} booked={booked:<3} bot msgs={sent:<5} drafts={held:<4} calls={u['n']:<5}"
              f" ${u['usd']:.3f}  cache hit {100 * u['r'] / tot if tot else 0:.0f}%  "
              f"${u['usd'] / convs if convs else 0:.4f}/conversation")
    for r in db.rows("SELECT purpose, COUNT(*) n, SUM(usd) usd FROM usage WHERE created_at>=? GROUP BY purpose", (since,)):
        print(f"    {r['purpose']:<12} {r['n']:>5} calls  ${r['usd']:.4f}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m setter", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    sub.add_parser("check")
    s = sub.add_parser("new-coach")
    s.add_argument("slug")
    s = sub.add_parser("train")
    s.add_argument("slug")
    s.add_argument("export")
    s.add_argument("--me", default="")
    s.add_argument("--draft-voice", action="store_true")
    s = sub.add_parser("demo-pack")
    s.add_argument("slug")
    s.add_argument("--code", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--handle", default="")
    s.add_argument("--url", action="append", default=[])
    s.add_argument("--notes", action="append", default=[])
    sub.add_parser("demo-codes")
    s = sub.add_parser("connect-token")
    s.add_argument("slug")
    s.add_argument("token")
    s = sub.add_parser("chat")
    s.add_argument("slug")
    s.add_argument("--name", default="Sam")
    s.add_argument("--source", default="story reply")
    s = sub.add_parser("eval")
    s.add_argument("slug")
    s.add_argument("--only", default="")
    s.add_argument("--turns", type=int, default=10)
    s = sub.add_parser("prompt")
    s.add_argument("slug")
    s.add_argument("--count", action="store_true")
    s = sub.add_parser("stats")
    s.add_argument("--days", type=int, default=1)
    a = p.parse_args(argv)
    {"serve": cmd_serve, "check": cmd_check, "new-coach": cmd_new_coach, "train": cmd_train,
     "connect-token": cmd_connect_token, "demo-pack": cmd_demo_pack, "demo-codes": cmd_demo_codes, "chat": cmd_chat, "eval": cmd_eval, "prompt": cmd_prompt,
     "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
