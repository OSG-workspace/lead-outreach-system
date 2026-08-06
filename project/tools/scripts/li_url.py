#!/usr/bin/env python3
"""ONE canonical form for a LinkedIn profile URL, shared by the whole chain.

WHY THIS FILE EXISTS
A person reaches the queue through two routes and three hands, and each one used
to spell the same profile differently:

  * the company walk writes what the page's href gave it — usually no trailing
    slash, sometimes a country subdomain (`ae.linkedin.com`);
  * resolve_li_profiles.py normalised its own way and stripped the trailing
    slash;
  * li-writer is a language model echoing a URL back in JSON, and it adds or
    drops the trailing slash at will.

Every join between those hands was an EXACT STRING MATCH, so a single trailing
slash silently dropped that person's composed DM. The lead then reached
generate.js with no message and was rejected there as "no follow-up DM composed"
— a message written, paid for, and thrown away, reported as a gate.

The canonical form here is byte-identical to `normalizeProfileUrl` in
linkedin/lib/state.js, which is what the Node side dedups and looks leads up on.
The two must not drift: keep them the same string.
"""
from __future__ import annotations
import re

_IN_RE = re.compile(r"/in/([^/?#]+)", re.I)
_HOST_RE = re.compile(r"^https?://([a-z0-9-]+\.)*linkedin\.com", re.I)


def canonical_profile_url(url: str | None) -> str | None:
    """-> "https://www.linkedin.com/in/<slug>/" (lowercased), or None.

    Returns None for anything that is not a real person profile — company pages,
    school pages, feed URLs, bare slugs with no /in/ segment. Those are exactly
    what an agent returns when it could not find a person, so they must not
    normalise into something that looks valid.
    """
    u = (url or "").strip()
    if not u:
        return None
    u = u.split("?")[0].split("#")[0]
    if not u.startswith("http"):
        u = "https://www.linkedin.com" + (u if u.startswith("/") else "/" + u)
    if not _HOST_RE.match(u):
        return None
    m = _IN_RE.search(u)
    if not m:
        return None
    slug = m.group(1).strip().lower()
    if not slug:
        return None
    return f"https://www.linkedin.com/in/{slug}/"
