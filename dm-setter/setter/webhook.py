"""Instagram webhook payloads -> flat events. Signature check (X-Hub-Signature-256) included."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


@dataclass
class Event:
    kind: str                 # message | echo | deleted | reaction | read | referral | comment
    account_id: str           # the coach's professional account (entry.id)
    igsid: str = ""           # the lead (Instagram-scoped id)
    mid: str = ""
    text: str = ""
    msg_kind: str = "text"    # text | audio | image | video | share | story_mention | postback | unsupported
    media_url: str = ""
    source: str = ""          # what started this: story reply, story mention, ad, comment, ice breaker
    ts: float = 0.0
    comment_id: str = ""
    media_id: str = ""
    username: str = ""


def verify(body: bytes, header: str, secret: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header.split("=", 1)[1])


_ATTACHMENT_TEXT = {
    "audio": ("audio", "[voice note]"), "image": ("image", "[photo]"), "video": ("video", "[video]"),
    "share": ("share", "[shared a post]"), "ig_reel": ("share", "[shared a reel]"), "reel": ("share", "[shared a reel]"),
    "ig_post": ("share", "[shared a post]"), "story_mention": ("story_mention", "[mentioned you in their story]"),
    "file": ("file", "[sent a file]"), "animated_image": ("image", "[sent a GIF]"),
}


def _messaging(acct: str, m: dict) -> Event | None:
    sender = str((m.get("sender") or {}).get("id", ""))
    recipient = str((m.get("recipient") or {}).get("id", ""))
    ts = float(m.get("timestamp") or 0) / 1000
    if "message" in m:
        msg = m["message"] or {}
        echo = bool(msg.get("is_echo"))
        ev = Event(kind="echo" if echo else "message", account_id=acct, igsid=recipient if echo else sender,
                   mid=msg.get("mid", ""), text=(msg.get("text") or "").strip(), ts=ts)
        if msg.get("is_deleted"):
            ev.kind = "deleted"
            return ev
        if msg.get("is_unsupported"):
            ev.msg_kind, ev.text = "unsupported", ev.text or "[sent a message type the API can't show]"
        for a in msg.get("attachments") or []:
            kind, label = _ATTACHMENT_TEXT.get(a.get("type", ""), ("share", "[sent an attachment]"))
            ev.msg_kind, ev.media_url = kind, (a.get("payload") or {}).get("url", "")
            ev.text = f"{label} {ev.text}".strip()
            if kind == "story_mention":
                ev.source = "story mention"
        story = (msg.get("reply_to") or {}).get("story")
        if story:
            ev.source = "story reply"
            ev.text = f"[replied to your story] {ev.text}".strip()
        ref = msg.get("referral") or m.get("referral")
        if ref:
            ev.source = "ad" if str(ref.get("source", "")).upper() == "ADS" else (ref.get("source") or "").lower()
        return ev
    if "postback" in m:
        pb = m["postback"] or {}
        return Event(kind="message", account_id=acct, igsid=sender, mid=pb.get("mid", ""), msg_kind="postback",
                     text=pb.get("title") or pb.get("payload") or "", source="ice breaker", ts=ts)
    if "reaction" in m:
        r = m["reaction"] or {}
        return Event(kind="reaction", account_id=acct, igsid=sender, mid=r.get("mid", ""),
                     text=f"{r.get('action', '')}:{r.get('emoji') or r.get('reaction', '')}", ts=ts)
    if "read" in m:
        return Event(kind="read", account_id=acct, igsid=sender, mid=(m["read"] or {}).get("mid", ""), ts=ts)
    if "referral" in m:
        ref = m["referral"] or {}
        return Event(kind="referral", account_id=acct, igsid=sender, ts=ts,
                     source="ad" if str(ref.get("source", "")).upper() == "ADS" else str(ref.get("source", "")).lower())
    return None


def parse(payload: dict) -> list[Event]:
    events: list[Event] = []
    if payload.get("object") not in (None, "instagram", "page"):
        return events
    for entry in payload.get("entry") or []:
        acct = str(entry.get("id", ""))
        for m in (entry.get("messaging") or []) + (entry.get("standby") or []):
            ev = _messaging(acct, m)
            if ev:
                events.append(ev)
        for ch in entry.get("changes") or []:
            field, v = ch.get("field"), ch.get("value") or {}
            if field == "comments":
                frm = v.get("from") or {}
                events.append(Event(kind="comment", account_id=acct, igsid=str(frm.get("id", "")),
                                    username=frm.get("username", ""), comment_id=str(v.get("id", "")),
                                    text=(v.get("text") or "").strip(), media_id=str((v.get("media") or {}).get("id", "")),
                                    source="comment"))
            elif field in ("messages", "messaging_postbacks", "message_reactions", "messaging_seen"):
                ev = _messaging(acct, v)               # the App Dashboard "Test" button sends this shape
                if ev:
                    events.append(ev)
    return events
