"""Make sent messages look and arrive like the coach typed them.

Text: a deterministic pass after the model, so the coach's measured habits hold even when the model drifts
(casing, trailing periods, emoji, no em dashes / semicolons / markdown, bubble length). Costs zero tokens.
Timing: mark seen, a short read pause, typing indicator for a length-proportional time per bubble, small gaps
between bubbles, and no replies at 3 a.m. coach time unless the 24-hour window would close first.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .questions import AI_TELLS

EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️‍]+")
DASH = re.compile(r"\s*[—–]\s*|\s+-\s+")
MARKDOWN = re.compile(r"\*\*|__|`|^#+\s*|^\s*(?:[-•*]|\d+[.)])\s+", re.M)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def clean(text: str, style: dict) -> str:
    t = (text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
         .replace("…", "..."))
    t = MARKDOWN.sub("", t)
    t = DASH.sub(", ", t)
    t = re.sub(r"\s*;\s*", ", ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r",\s*,", ",", t).strip(" ,")
    if float(style.get("emoji_rate", 0.15)) < 0.05:
        t = EMOJI.sub("", t).strip()
    if not style.get("end_period") and t.endswith(".") and not t.endswith(".."):
        t = t[:-1]
    if style.get("lowercase") and t[:1].isupper() and not (len(t) > 1 and t[1].isupper()) \
            and not re.match(r"I\b", t):
        t = t[0].lower() + t[1:]
    if style.get("lower_i"):
        t = re.sub(r"\bI\b", "i", t)
    return t.strip()


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = SENTENCE_END.split(text)
    if len(parts) < 2:
        return [text]
    half, acc = len(text) / 2, ""
    for i, p in enumerate(parts):
        acc = (acc + " " + p).strip()
        if len(acc) >= half or i == len(parts) - 2:
            return [acc, " ".join(parts[i + 1:]).strip()]
    return [text]


def style(bubbles: list[str], style_: dict) -> list[str]:
    limit = max(160, int(float(style_.get("p90_chars", 120)) * 1.6))
    out: list[str] = []
    for b in bubbles:
        c = clean(b, style_)
        if c:
            out.extend(x for x in _split(c, limit) if x)
    if len(out) > 3:
        out = out[:2] + [" ".join(out[2:])]
    return out


def tells(bubbles: list[str]) -> list[str]:
    low = " ".join(bubbles).lower()
    return [t for t in AI_TELLS if t in low]


# --- timing ------------------------------------------------------------------------------------------------
def read_seconds(inbound: str) -> float:
    return min(7.0, max(1.5, len(inbound) / 30 + random.uniform(1.0, 3.0)))


def typing_seconds(text: str) -> float:
    return min(11.0, max(1.2, len(text) / random.uniform(5.5, 9.0) + random.uniform(0.4, 1.4)))


def gap_seconds() -> float:
    return random.uniform(0.6, 2.2)


def _hm(s: str) -> tuple[int, int]:
    h, m = (s or "0:0").split(":")
    return int(h), int(m)


def next_active(hours: dict | None, at: float) -> float | None:
    """None when `at` is inside the coach's active hours, else the timestamp of the next start (plus jitter)."""
    if not hours or not hours.get("start") or not hours.get("end"):
        return None
    try:
        tz = ZoneInfo(hours.get("timezone", "UTC"))
    except Exception:  # noqa: BLE001
        return None
    local = datetime.fromtimestamp(at, tz)
    (sh, sm), (eh, em) = _hm(hours["start"]), _hm(hours["end"])
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:                                   # overnight window, e.g. 09:00 -> 01:00
        inside = local >= start or local < end
    else:
        inside = start <= local < end
    if inside:
        return None
    nxt = start if local < start else start + timedelta(days=1)
    return nxt.timestamp() + random.uniform(120, 1500)
