"""Draft a demo coach pack from public material: `./setter-run demo-pack <slug> --code JANE7 --url <site> ...`.

Reads the coach's website pages (plus any text files you pass with --notes: video transcripts, a sales page you
copied, your call notes), finds their booking link, and has the reply model write the pack: coach.yaml fields,
offer.md, script.md, voice.md and faq.md. Every fact must come from the material; gaps stay as TODO lines for you.
Review the pack, then try it with `./setter-run chat <slug>` before sending the coach the code.
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path

import httpx
import yaml

from . import llm
from .config import settings

MAX_CHARS = 60_000
BOOKING = re.compile(r"https?://(?:www\.)?(?:calendly\.com|cal\.com|app\.cal\.com|tidycal\.com|savvycal\.com|"
                     r"[a-z0-9.-]*hubspot\.com/meetings|[a-z0-9.-]*leadconnectorhq\.com|[a-z0-9.-]*oncehub\.com)"
                     r"/[^\s\"'<>)]+", re.I)

PROMPT = """You build the knowledge pack for an AI Instagram DM setter that will text AS this online coach, in the \
first person, to qualify leads and book them onto a call. Use ONLY the material given (their website and any notes). \
Never invent prices, results, guarantees, credentials or program details: when something is missing, write a line \
starting with "TODO:" so a person fills it in.

- what_i_do: one line, "online <type> coaching for <who> who want <result>".
- qualification: 4-6 slots the setter must learn before pitching the call (goal, current situation, obstacle, \
urgency, commitment to invest), with `ask` worded for THIS coach's niche. Mark the essential ones required.
- disqualify_if: who this coach clearly does not serve, from the material.
- offer_md: bullet facts the setter may state (program name, format, length, who it's for, what's included). \
Write "price: discussed on the call" unless a price is published.
- script_md: the setter script by stage (opener, discovery, gap, pitch the call, objections) in this coach's words, \
with answers to the 4-6 objections their leads will raise.
- voice_md: how this coach talks, from their own writing and transcripts: tone, energy, phrases they repeat, \
slang, emoji habits, what they never say. Quote real phrases.
- faq_md: Q/A pairs found in the material (empty if none)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "what_i_do": {"type": "string"},
        "language": {"type": "string"},
        "call_name": {"type": "string"},
        "call_length": {"type": "string"},
        "qualification": {"type": "array", "items": {
            "type": "object", "properties": {"slot": {"type": "string"}, "ask": {"type": "string"},
                                             "required": {"type": "boolean"}},
            "required": ["slot", "ask", "required"], "additionalProperties": False}},
        "disqualify_if": {"type": "array", "items": {"type": "string"}},
        "offer_md": {"type": "string"},
        "script_md": {"type": "string"},
        "voice_md": {"type": "string"},
        "faq_md": {"type": "string"},
    },
    "required": ["what_i_do", "language", "call_name", "call_length", "qualification", "disqualify_if",
                 "offer_md", "script_md", "voice_md", "faq_md"],
    "additionalProperties": False,
}


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|section)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(x for x in (re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()) if x)


async def gather(urls: list[str], notes: list[Path]) -> tuple[str, str]:
    """(material, booking link found on their pages or '')."""
    parts, link = [], ""
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Macintosh) dm-setter demo-pack"}) as c:
        for u in urls:
            try:
                r = await c.get(u)
                r.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  skipped {u}: {e}")
                continue
            link = link or next(iter(BOOKING.findall(r.text)), "")
            parts.append(f"## Page: {u}\n{page_text(r.text)}")
    for n in notes:
        parts.append(f"## Notes: {n.name}\n{n.read_text(errors='ignore')}")
    return "\n\n".join(parts)[:MAX_CHARS], link


def write_pack(slug: str, a: dict, *, name: str, handle: str, code: str, link: str) -> Path:
    d = settings.coaches_dir / slug
    if not d.exists():
        shutil.copytree(settings.coaches_dir / "_template", d)
    cfg = yaml.safe_load((d / "coach.yaml").read_text()) or {}
    cfg.update(name=name, handle=handle.lstrip("@"), what_i_do=a["what_i_do"], language=a["language"] or "English",
               demo_code=code.upper(), qualification=a["qualification"] or cfg.get("qualification"),
               disqualify_if=a["disqualify_if"] or cfg.get("disqualify_if"))
    cfg["call"] = {**(cfg.get("call") or {}), "name": a["call_name"] or "free strategy call",
                   "length": a["call_length"] or "20-minute",
                   "link": link or f"https://example.com/book-{slug}  # TODO: their real booking link"}
    (d / "coach.yaml").write_text(f"# Drafted by demo-pack from public material. Review before sending DEMO {code.upper()}.\n"
                                  + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False, width=110))
    for f, key in (("offer.md", "offer_md"), ("script.md", "script_md"), ("voice.md", "voice_md"), ("faq.md", "faq_md")):
        (d / f).write_text((a[key] or "").strip() + "\n")
    return d


async def build(slug: str, *, name: str, handle: str, code: str, urls: list[str], notes: list[Path]) -> Path:
    material, link = await gather(urls, notes)
    if not material.strip():
        raise SystemExit("no material: pass at least one --url that loads or a --notes file")
    a, usage = await llm.structured(
        model=settings.reply_model, system=[{"type": "text", "text": PROMPT}], schema=SCHEMA, coach=slug,
        thread_id=None, purpose="demo_pack", effort="medium", max_tokens=8000,
        messages=[{"role": "user", "content": f"Coach: {name} (@{handle.lstrip('@')})\n\n{material}"}])
    d = write_pack(slug, a, name=name, handle=handle, code=code, link=link)
    todos = sum(open(p).read().count("TODO") for p in d.glob("*.*") if p.is_file())
    print(f"pack: {d}  (${usage['usd']:.3f}, booking link {'found' if link else 'NOT found'}, {todos} TODOs)")
    print(f"next: review it, then `./setter-run chat {slug}`; the coach DMs \"DEMO {code.upper()}\" to the demo account")
    return d
