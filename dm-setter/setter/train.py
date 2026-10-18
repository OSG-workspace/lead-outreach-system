"""Learn a coach's texting voice from their Instagram data export ("Download your information", JSON format).

  python -m setter train <coach> <export.zip | export folder> [--me "Display Name"] [--draft-voice]

Writes, inside coaches/<coach>/:
  style.yaml          measured habits: casing, periods, emoji, length, bubbles per turn, openers, phrases
  examples/auto-*.txt the best conversations that reached the booking link (lead anonymised, links stripped)
  voice.draft.md      (--draft-voice) a first voice guide written by Claude from ~300 real messages; review it,
                      then rename to voice.md. One call, a few cents.
Your own hand-written examples (any examples/*.txt not starting with auto-) are never touched.
"""

from __future__ import annotations

import json
import random
import re
import statistics
import zipfile
from collections import Counter
from pathlib import Path

import yaml

from .config import ROOT, settings
from .humanize import EMOJI

BOOKING_HOSTS = ("calendly.com", "cal.com", "leadconnectorhq.com", "msgsndr.com", "savvycal.com", "tidycal.com",
                 "acuityscheduling.com", "oncehub.com", "zcal.co", "hubspot.com/meetings", "typeform.com",
                 "/book", "/apply", "/call")
BOOKED_WORDS = re.compile(r"\b(booked|done|scheduled|confirmed|see you|just booked|signed up|locked in|grabbed)\b", re.I)
SYSTEM_TEXT = re.compile(r"^(liked a message|reacted .* to your message|.* sent an attachment\.?|you sent an attachment"
                         r"|.* changed the theme|.* unsent a message)$", re.I)
URL = re.compile(r"https?://\S+|www\.\S+")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
LAUGHS = ("hahaha", "haha", "lol", "lmao", "😂", "🤣")
STOP = set("the a an and or but to of in on for with at is are was it i you we me my your this that be do just so "
           "im i'm it's its if as have has not no yes can will what how".split())


def fix(s: str) -> str:
    """Instagram exports double-encode UTF-8 as latin-1."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def thread_files(src: Path, scratch: Path) -> dict[Path, list[Path]]:
    if src.suffix == ".zip":
        scratch.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if re.search(r"messages/(inbox|message_requests)/[^/]+/message_\d+\.json$", n):
                    z.extract(n, scratch)
        src = scratch
    groups: dict[Path, list[Path]] = {}
    for f in src.rglob("message_*.json"):
        if f.parent.parent.name in ("inbox", "message_requests"):
            groups.setdefault(f.parent, []).append(f)
    return groups


def load(files: list[Path]) -> tuple[list[str], list[dict]]:
    participants, msgs = [], []
    for f in sorted(files):
        d = json.loads(f.read_text())
        participants = [fix(p.get("name", "")) for p in d.get("participants", [])] or participants
        msgs.extend(d.get("messages", []))
    msgs.sort(key=lambda m: m.get("timestamp_ms", 0))
    return participants, msgs


def text_of(m: dict) -> str:
    if m.get("content"):
        t = fix(m["content"])
        return "" if SYSTEM_TEXT.match(t.strip()) else t
    if m.get("audio_files"):
        return "[voice note]"
    if m.get("photos"):
        return "[photo]"
    if m.get("videos"):
        return "[video]"
    if m.get("share"):
        link = (m["share"] or {}).get("link", "")
        return f"[shared] {link}".strip()
    return ""


def scrub(t: str, lead_names: list[str]) -> str:
    t = URL.sub("[link]", t)
    t = EMAIL.sub("[email]", t)
    t = PHONE.sub("[phone]", t)
    for n in lead_names:
        for part in n.split():
            if len(part) > 2:
                t = re.sub(rf"\b{re.escape(part)}\b", "Lead", t, flags=re.I)
    return t


def detect_owner(groups: dict[Path, list[Path]]) -> str:
    seen = Counter()
    for files in groups.values():
        parts, _ = load(files)
        seen.update(set(parts))
    return seen.most_common(1)[0][0] if seen else ""


def analyse(src: Path, slug: str, me: str = "", call_link: str = "") -> dict:
    groups = thread_files(src, ROOT / "data" / "train" / slug)
    me = me or detect_owner(groups)
    hosts = BOOKING_HOSTS + ((call_link.split("//")[-1].split("/")[0],) if call_link else ())
    coach_lines: list[str] = []
    runs: list[int] = []
    openers: Counter = Counter()
    convos: list[dict] = []
    for files in groups.values():
        parts, raw = load(files)
        if me not in parts or len(parts) != 2:
            continue                                   # group chats teach nothing about 1:1 setting
        lead_names = [p for p in parts if p != me]
        turns: list[tuple[str, str]] = []
        for m in raw:
            t = text_of(m).strip()
            if not t:
                continue
            who = "YOU" if fix(m.get("sender_name", "")) == me else "LEAD"
            turns.append((who, t))
        run = 0
        for i, (who, t) in enumerate(turns):
            if who == "YOU":
                if not URL.search(t):
                    coach_lines.append(t)
                if run == 0:
                    first = re.findall(r"[\w']+", t.lower())[:1]
                    if first:
                        openers[first[0]] += 1
                run += 1
            elif run:
                runs.append(run)
                run = 0
        link_at = next((i for i, (w, t) in enumerate(turns)
                        if w == "YOU" and any(h in t.lower() for h in hosts) and URL.search(t)), None)
        if link_at is None:
            continue
        after = [t for w, t in turns[link_at + 1: link_at + 9] if w == "LEAD"][:4]
        outcome = "booked" if any(BOOKED_WORDS.search(t) for t in after) else "link_sent"
        convos.append({"turns": turns[: link_at + 4], "outcome": outcome, "lead": lead_names})
    return {"me": me, "lines": coach_lines, "runs": runs, "openers": openers, "convos": convos,
            "threads": len(groups)}


def style_of(a: dict) -> dict:
    lines = [l for l in a["lines"] if l and not l.startswith("[")]
    if not lines:
        return {}
    letters = [l for l in lines if l[0].isalpha()]
    lower = sum(1 for l in letters if l[0].islower()) / max(1, len(letters))
    i_low = len(re.findall(r"(?:^|\s)i(?:\s|'|$)", " ".join(lines)))
    i_up = len(re.findall(r"(?:^|\s)I(?:\s|'|$)", " ".join(lines)))
    period = sum(1 for l in lines if l.endswith(".") and not l.endswith("..")) / len(lines)
    emoji_lines = [l for l in lines if EMOJI.search(l)]
    emos = Counter(ch for l in emoji_lines for ch in EMOJI.findall(l) if ch.strip("️‍"))
    lengths = sorted(len(l) for l in lines)
    laugh = Counter()
    for l in lines:
        low = l.lower()
        for w in LAUGHS:
            if w in low:
                laugh[w] += 1
                break
    grams: Counter = Counter()
    for l in lines:
        words = re.findall(r"[\w']+", l.lower())
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                g = words[i:i + n]
                if not all(w in STOP for w in g):
                    grams[" ".join(g)] += 1
    phrases = [g for g, c in grams.most_common(60) if c >= 4][:12]
    return {
        "lowercase": lower > 0.5,
        "lower_i": i_low > i_up and i_low >= 5,
        "end_period": period > 0.3,
        "emoji_rate": round(len(emoji_lines) / len(lines), 2),
        "top_emojis": [e for e, _ in emos.most_common(5)],
        "median_chars": int(statistics.median(lengths)),
        "p90_chars": int(lengths[int(len(lengths) * 0.9) - 1]) if len(lengths) > 1 else lengths[0],
        "bubbles_per_turn": round(statistics.mean(a["runs"]), 1) if a["runs"] else 1.5,
        "laugh": laugh.most_common(1)[0][0] if laugh else "",
        "openers": [w for w, c in a["openers"].most_common(8) if c >= 3][:6],
        "phrases": phrases,
    }


def pick_examples(convos: list[dict], n: int = 6) -> list[str]:
    ranked = sorted(convos, key=lambda c: (c["outcome"] != "booked", abs(len(c["turns"]) - 16)))
    out = []
    for c in ranked[:n]:
        turns = c["turns"]
        if len(turns) > 28:
            turns = turns[:6] + [("...", "[earlier messages skipped]")] + turns[-20:]
        lines = [f"{w}: {scrub(t, c['lead'])}" if w != "..." else t for w, t in turns]
        out.append(f"(outcome: {c['outcome']})\n" + "\n".join(lines))
    return out


def write(slug: str, a: dict, style: dict, examples: list[str]) -> Path:
    d = settings.coaches_dir / slug
    (d / "examples").mkdir(parents=True, exist_ok=True)
    (d / "style.yaml").write_text("# Measured from the Instagram export by `python -m setter train`. Override any key\n"
                                  "# under `style:` in coach.yaml instead of editing this file.\n"
                                  + yaml.safe_dump(style, allow_unicode=True, sort_keys=False))
    for old in (d / "examples").glob("auto-*.txt"):
        old.unlink()
    for i, ex in enumerate(examples, 1):
        (d / "examples" / f"auto-{i:02d}.txt").write_text(ex + "\n")
    samples = ROOT / "data" / "train" / f"{slug}-samples.txt"
    samples.parent.mkdir(parents=True, exist_ok=True)
    lines = [scrub(l, []) for l in a["lines"]]
    random.Random(7).shuffle(lines)
    samples.write_text("\n".join(lines[:300]))
    return samples


VOICE_PROMPT = """Below are ~300 real Instagram DMs written by an online coach (one per line, shuffled). Write the \
voice guide a ghostwriter would need to text exactly like them: tone, energy, how they open and close, how they ask \
questions, how they show empathy, slang and phrases they repeat, what they never say, how they handle hesitation. \
Use short bullet points under 6-8 headings, quote real phrases in quotes, under 450 words, no preamble."""


async def draft_voice(slug: str, samples: Path) -> Path:
    from . import llm
    text, usage = await llm.text(model=settings.reply_model, system=VOICE_PROMPT, coach=slug, thread_id=None,
                                 purpose="train_voice", max_tokens=1500, effort="medium",
                                 messages=[{"role": "user", "content": samples.read_text()}])
    out = settings.coaches_dir / slug / "voice.draft.md"
    out.write_text(text + "\n")
    print(f"voice draft: {out} (${usage['usd']:.3f})")
    return out
