"""Coach packs: one folder per coach under coaches/, everything that makes the setter talk like that coach.

coaches/<slug>/
  coach.yaml     settings: name, call link, mode, slots, disclosure, hours, nudges, comment triggers, limits
  voice.md       how the coach texts, in words (train --draft-voice writes a first version from real DMs)
  script.md      the setter script: what to ask at each stage, objection answers
  offer.md       the ONLY facts the setter may state
  faq.md         common questions and the coach's answers (optional)
  style.yaml     measured texting habits (written by `train`; coach.yaml `style:` overrides it)
  examples/*.txt real conversations that booked (written by `train`, editable)

A pack is re-read when any of its files changes, so edits apply to the next reply without a restart.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import db
from .config import settings
from .stages import MODEL_STAGES

DEFAULT_STYLE = {
    "lowercase": False, "lower_i": False, "end_period": False, "emoji_rate": 0.15, "top_emojis": [],
    "median_chars": 45, "p90_chars": 120, "bubbles_per_turn": 1.6, "laugh": "haha", "openers": [], "phrases": [],
}
DEFAULT_SLOTS = [
    {"slot": "goal", "ask": "the result they want", "required": True},
    {"slot": "current", "ask": "where they are now and what they've tried", "required": True},
    {"slot": "obstacle", "ask": "what's in the way", "required": True},
    {"slot": "urgency", "ask": "why now", "required": False},
    {"slot": "commitment", "ask": "ready to invest time and money in getting help", "required": True},
]
EXAMPLE_TOKEN_BUDGET = 3500          # ~chars/4: few-shot examples sit in the cached prefix, keep them tight


def _read(p: Path) -> str:
    """File text without <!-- guidance comments --> and empty template bullets: they would cost tokens forever."""
    if not p.is_file():
        return ""
    t = re.sub(r"<!--.*?-->", "", p.read_text(), flags=re.S)
    t = re.sub(r"(?m)^\s*-\s*$\n?", "", t)
    return t.strip()


@dataclass
class Coach:
    slug: str
    dir: Path
    cfg: dict
    voice: str = ""
    script: str = ""
    offer: str = ""
    faq: str = ""
    examples: list[str] = field(default_factory=list)
    style: dict = field(default_factory=dict)
    stamp: float = 0.0
    _block: str = ""

    # --- settings with defaults --------------------------------------------------------------------------
    def get(self, dotted: str, default=None):
        cur = self.cfg
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    @property
    def name(self) -> str:
        return self.cfg.get("name") or self.slug

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    @property
    def mode(self) -> str:
        return self.cfg.get("mode", "draft")

    @property
    def slots(self) -> list[dict]:
        return self.cfg.get("qualification") or DEFAULT_SLOTS

    @property
    def slot_names(self) -> list[str]:
        return [s["slot"] for s in self.slots]

    @property
    def call_link(self) -> str:
        return self.get("call.link", "")

    # --- the cached prompt block -------------------------------------------------------------------------
    def block(self) -> str:
        """Everything coach-specific, rendered once per pack version. It must be byte-stable between turns:
        it is the second cache breakpoint, so any per-turn value here would re-bill the whole pack."""
        if not self._block:
            self._block = render_block(self)
        return self._block

    def output_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "bubbles": {"type": "array", "items": {"type": "string"}},
                "stage": {"type": "string", "enum": MODEL_STAGES},
                "slots": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"slot": {"type": "string", "enum": self.slot_names},
                                   "value": {"type": "string"}},
                    "required": ["slot", "value"], "additionalProperties": False}},
                "confidence": {"type": "number"},
                "handoff": {"type": "boolean"},
                "reason": {"type": "string"},
                "send_link": {"type": "boolean"},
                "heart": {"type": "boolean"},
            },
            "required": ["bubbles", "stage", "slots", "confidence", "handoff", "reason", "send_link", "heart"],
            "additionalProperties": False,
        }


def _style_lines(c: Coach) -> list[str]:
    s = c.style
    out = []
    out.append("starts messages with a lowercase letter" if s["lowercase"] else "starts messages with a capital")
    if s.get("lower_i"):
        out.append('writes "i" in lowercase')
    out.append("ends messages with a period" if s["end_period"] else "almost never ends a message with a period")
    out.append(f"typical message is about {int(s['median_chars'])} characters, rarely over {int(s['p90_chars'])}")
    out.append(f"sends about {s['bubbles_per_turn']:.1f} messages per turn")
    rate = float(s["emoji_rate"])
    emo = " ".join(s.get("top_emojis") or [])
    if rate < 0.05:
        out.append("basically never uses emoji")
    else:
        freq = "often" if rate > 0.35 else "sometimes"
        out.append(f"uses emoji {freq} (~{int(rate * 100)}% of messages)" + (f", mostly {emo}" if emo else ""))
    if s.get("laugh"):
        out.append(f'laughs with "{s["laugh"]}"')
    if s.get("openers"):
        out.append("opens with: " + ", ".join(f'"{o}"' for o in s["openers"][:6]))
    if s.get("phrases"):
        out.append("phrases they actually use: " + ", ".join(f'"{p}"' for p in s["phrases"][:12]))
    return out


def render_block(c: Coach) -> str:
    call = c.cfg.get("call") or {}
    closer = call.get("closer") or "me"
    closer_txt = "you (the coach)" if closer.lower() in ("me", "i", c.name.lower()) else closer
    parts = [
        "# The coach",
        f"You are writing as {c.name} (@{c.cfg.get('handle', c.slug)}), from their own Instagram account. "
        f"What they do: {c.cfg.get('what_i_do', 'online coaching')}. Write in the first person as {c.first_name}. "
        f"Qualified leads book a {call.get('length', '20-minute')} {call.get('name', 'call')} run by {closer_txt}.",
        f"Language: {c.cfg.get('language', 'English')} unless the lead writes in another language.",
        "", "# Voice guide", c.voice or "(none yet: write short, warm, casual texts)",
        "", f"# How {c.first_name} texts (measured from their real DMs)",
        *[f"- {line}" for line in _style_lines(c)],
        "", "# Setter script", c.script or "(use the default stage flow)",
        "", "# What you must learn before pitching the call (slots)",
        *[f"- {s['slot']}{' (required)' if s.get('required') else ''}: {s.get('ask', '')}" for s in c.slots],
        "Pitch the call only once every required slot is known, unless the lead asks how to work together.",
    ]
    dq = c.cfg.get("disqualify_if") or []
    if dq:
        parts += ["", "# Not a fit (move to DISQUALIFIED and exit kindly)", *[f"- {d}" for d in dq]]
    if c.cfg.get("downsell"):
        parts.append(f"Free resource you can offer people who aren't a fit: {c.cfg['downsell']}")
    parts += ["", "# Offer facts (the only facts you may state)", c.offer or "(none: say the call covers details)"]
    if c.faq:
        parts += ["", "# FAQ (the coach's own answers)", c.faq]
    if c.examples:
        parts += ["", f"# Real conversations that booked a call (match {c.first_name}'s voice, not the content)"]
        parts += [f'<conversation n="{i + 1}">\n{ex}\n</conversation>' for i, ex in enumerate(c.examples)]
    return "\n".join(parts).strip()


def load(dir: Path) -> Coach:
    cfg = yaml.safe_load(_read(dir / "coach.yaml")) or {}
    style = dict(DEFAULT_STYLE)
    style.update(yaml.safe_load(_read(dir / "style.yaml")) or {})
    style.update(cfg.get("style") or {})
    examples, budget = [], EXAMPLE_TOKEN_BUDGET * 4
    for f in sorted((dir / "examples").glob("*.txt")) if (dir / "examples").is_dir() else []:
        text = _read(f)
        if text and len(text) <= budget:
            examples.append(text)
            budget -= len(text)
    return Coach(slug=dir.name, dir=dir, cfg=cfg, voice=_read(dir / "voice.md"), script=_read(dir / "script.md"),
                 offer=_read(dir / "offer.md"), faq=_read(dir / "faq.md"), examples=examples, style=style,
                 stamp=_stamp(dir))


def _stamp(dir: Path) -> float:
    files = [p for p in dir.rglob("*") if p.is_file()]
    return max((p.stat().st_mtime for p in files), default=0.0)


_packs: dict[str, Coach] = {}
_checked: dict[str, float] = {}


def get(slug: str) -> Coach | None:
    d = settings.coaches_dir / slug
    if not (d / "coach.yaml").is_file() or slug.startswith("_"):
        return None
    t = time.time()
    if slug in _packs and t - _checked.get(slug, 0) < 5:
        return _packs[slug]
    _checked[slug] = t
    if slug not in _packs or _stamp(d) != _packs[slug].stamp:
        _packs[slug] = load(d)
    return _packs[slug]


def all_slugs() -> list[str]:
    if not settings.coaches_dir.is_dir():
        return []
    return sorted(p.name for p in settings.coaches_dir.iterdir()
                  if (p / "coach.yaml").is_file() and not p.name.startswith("_"))


def for_account(ig_user_id: str) -> Coach | None:
    """The coach that owns an Instagram account: connected via /connect, or pinned with ig_user_id in yaml."""
    acc = db.account(ig_user_id)
    if acc:
        return get(acc["coach"])
    for slug in all_slugs():
        c = get(slug)
        if c and str(c.cfg.get("ig_user_id", "")) == str(ig_user_id):
            return c
    return None
