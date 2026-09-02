#!/usr/bin/env python3
"""Stage 2.5 (deterministic): resolve a business NAME to its OWN domain.

WHY THIS EXISTS
Overpass enumerates the ICP exhaustively and precisely — it knows 61 clinics in
Riyadh — but the chain can only use an element that carries a `website` tag, and
in Arabic-speaking markets almost none do (Riyadh clinics 2026-07-27: 61 named,
7 with a website, 3 phone-only, 51 name-only). The names are correct and the
businesses are real; the domain is the only thing missing. This stage recovers
it WITHOUT an LLM and without an agent: one web search per name, then a
VERIFICATION fetch.

WHY VERIFICATION IS NOT OPTIONAL
Measured 2026-07-27 on 15 website-less Riyadh clinics: taking the first
non-directory search result yields a plausible-looking domain ~12/15 times but
is actually CORRECT only ~2/15. The failures are silent and confident —
"Eternal Smile Clinic" resolved to ihg.com (InterContinental Hotels), two names
resolved to zhihu.com, one to ksaa.gov.sa. A wrong domain is far worse than a
missing one: it sends a pitch addressed to business A at business B, poisons the
sent-log, and burns a domain forever. So a candidate is emitted ONLY when the
fetched page actually proves it belongs to that business (name-token match in
title/meta/body). Everything unproven is DROPPED, per kill-on-fallback.

This buys volume only where the business genuinely has a site. It cannot invent
one: a Riyadh clinic that operates purely on phone/Instagram stays unreachable
by an email campaign, and that is a market fact, not a tooling gap.

INPUT   <run-dir>/unresolved-<prefix>-NNN.txt   `Name|ISO2|City|vertical`
OUTPUT  <run-dir>/candidates-batch-resolved-<prefix>-NNN.txt
        in the standard contract `domain|Name|ISO2|vertical|0`, so
        merge_candidates.py (glob candidates-batch-*.txt) and every later stage
        are unchanged.

PER-BRANCH CONFIG (sourcing.json — every knob optional, all have defaults):
  "resolve_domains": true            enable this stage for the campaign
  "resolve_query": "{name} {city} official website"    search phrasing
  "resolve_max": 400                 cap on names resolved per run
  "resolve_min_name_tokens": 2       tokens that must match on the page
  "resolve_blocklist": ["extra.com"] additional never-accept hosts
  "resolve_workers": 12              parallel resolver threads, cap 24 (env RESOLVE_WORKERS wins)
  "resolve_sleep": 0.6               upper bound of the per-row jitter (lower bound 0.15s)
  "resolve_max_fetches": 2           verification fetches per name (8s timeout each)

PARALLEL (2026-08-19). This stage was the pipeline's #1 wall-clock cost once the
fetch stage was fixed: strictly serial, one row at a time — a search, up to 3
verification fetches EACH with a 12s timeout, then an unconditional sleep(2).
Measured on 2026-08-19-au-trades: 602 names -> 70 minutes -> 171 domains (~7s a
row). And the work is 94% non-redundant (161/171 resolved domains were found by
NO other source that run), so the stage cannot be cut — only made concurrent.
Now a ThreadPoolExecutor runs `resolve_workers` rows at once, each worker with
its own DDGS session and a jittered pause between rows. Rate-limit errors
trigger a SHARED exponential backoff (all workers pause together, the row
retries once) so a throttled backend degrades to slower, never to wrong or
silently empty. Verification is byte-identical to the serial version — the
quality bar (fetched page must PROVE identity) is untouched.

THROUGHPUT PASS (2026-09-02). Measured on 2026-09-02-gcc-receptionist: 800
names -> 24 minutes -> 168 domains (21%) on 8 workers. Per row the cost was a
0.3-1.2 s jitter, then up to 3 verification fetches, each tried https THEN http
with a 12 s timeout apiece — a dead host cost 24 s and three of them 72 s.
Changed: workers 8 -> 12 (cap 16 -> 24), jitter 0.15-0.6 s, fetch timeout 8 s,
at most 2 fetches per name. Not 16 workers by default, deliberately: the
remote throttle is per-IP, not per-thread (ddgs keeps no process-level limiter;
each thread's DDGS session gets its own random browser impersonation), every
ddgs.text() call already fans out to 2 providers concurrently, and a throttled
IP stays throttled for a while and also starves li-search on the same machine.
The shared Pacer backoff (all workers hold together, 10 -> 60 s, one retry on a
fresh session) still bounds the damage at any worker count, but the release
after a hold is a synchronized burst that grows with the pool, so 12 is the
default and 16-24 is one env var away (RESOLVE_WORKERS) once a run shows it is
safe on this IP. Which domains are accepted is untouched.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Hosts that are never a business's OWN domain: social, maps, aggregators, and
# the regional business directories that dominate GCC/LB search results.
DIRECTORY = re.compile(
    r"(facebook|instagram|twitter|x\.com|linkedin|youtube|tiktok|pinterest|threads|"
    r"telegram|t\.me|whatsapp|wa\.me|snapchat|reddit|quora|zhihu|medium\.com|"
    r"google|bing|yandex|duckduckgo|apple\.com|wikipedia|wikimedia|wikidata|"
    r"tripadvisor|yelp|foursquare|booking\.com|agoda|expedia|hotels\.com|airbnb|"
    r"justdial|yellowpages|saudiayp|eyeofriyadh|sehaguide|zavis\.ai|daleli|dalilnet|"
    r"vymaps|near-place|locationbits|mapcarta|openstreetmap|maps\.|numbeo|olx|"
    r"amazon|ubuy|noon\.com|alibaba|indeed|glassdoor|bayt\.com|linktr\.ee|"
    r"altibbi|vezeeta|okadoc|practo|zocdoc|healthgrades|clinicspots|hospitals-sa|"
    r"visitsaudi|\.gov\.|\.gov$|forums\.|blogspot|wordpress\.com|wix\.com|"
    r"commentcamarche|archive\.org|scribd|slideshare|issuu)", re.I)

_WORD = re.compile(r"[A-Za-z؀-ۿ][A-Za-z0-9؀-ۿ]{2,}")
# Generic words that must not, on their own, count as proof of identity.
_STOP = {"the", "and", "for", "center", "centre", "clinic", "clinics", "medical",
         "dental", "dentist", "hospital", "group", "company", "co", "llc", "ltd",
         "salon", "spa", "beauty", "hair", "real", "estate", "properties",
         "restaurant", "cafe", "auto", "car", "service", "services", "trading",
         "est", "establishment", "riyadh", "jeddah", "dubai", "doha", "beirut",
         "saudi", "arabia", "emirates", "uae", "qatar", "kuwait", "bahrain", "oman",
         "مركز", "مجمع", "عيادة", "عيادات", "طبي", "الطبي", "للاسنان", "الأسنان",
         "مستوصف", "شركة", "مؤسسة", "مطعم", "صالون"}


def name_tokens(name: str) -> list[str]:
    """Identity-bearing tokens from a business name (generic words removed)."""
    return [w.lower() for w in _WORD.findall(name) if w.lower() not in _STOP]


FETCH_TIMEOUT = 8          # was 12; a verification page that has not answered in 8 s is not proving anything
MAX_FETCHES = 2            # was 3; hosts tried per name (each https then http)
JITTER_LO, JITTER_HI = 0.15, 0.6   # was 0.3-1.2


def fetch(url: str, timeout: int = FETCH_TIMEOUT) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en,ar;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            raw = r.read(400_000)
        for enc in ("utf-8", "windows-1256", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    except Exception:
        return None
    return None


def verifies(html: str, tokens: list[str], min_tokens: int, domain: str = "") -> bool:
    """The page must PROVE it belongs to this business.

    Identity-bearing name tokens are matched against the title, meta
    description/og:title, and visible text. Requiring `min_tokens` distinct hits
    is what stops 'Eternal Smile Clinic' from being accepted at ihg.com — the
    generic words ('clinic', 'medical') are stripped before matching, so only a
    real name overlap can pass."""
    # A name with too few identity-bearing tokens CANNOT be verified — do not
    # quietly lower the bar for it. ("exit 16" -> ['exit'] once digits/stopwords
    # are stripped, which matched exit.ch; "Future Medical Center" -> ['future'],
    # which matched genius.com. Both were accepted before this guard.)
    if len(tokens) < min_tokens:
        return False
    low = html.lower()
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", low, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    if len({t for t in tokens if t in text}) < min_tokens:
        return False
    # Body mention alone is weak: a directory's root page can mention many
    # businesses. Require the identity ALSO in a strong position — the domain
    # itself or the <title> of the page we fetched.
    title = " ".join(re.findall(r"<title[^>]*>(.*?)</title>", low, re.S))[:400]
    return any(t in title or t in domain for t in tokens)


class Pacer:
    """Shared politeness + backoff across resolver threads.

    Every worker calls wait() before its search: a jittered pause, plus honoring
    any global hold. A rate-limited worker calls backoff(); the hold applies to
    ALL workers, so one throttled backend slows the pool as a unit instead of
    each thread independently hammering it into a harder block."""

    def __init__(self, base_sleep: float):
        self._lock = threading.Lock()
        self._hold_until = 0.0
        self._penalty = 10.0          # grows 10 -> 20 -> 40 -> 60 (cap)
        self.base = max(JITTER_LO + 0.05, base_sleep)

    def wait(self) -> None:
        held = False
        while True:
            with self._lock:
                hold = self._hold_until
            now = time.monotonic()
            if now >= hold:
                break
            held = True
            time.sleep(min(hold - now, 2.0))
        if held:
            # Every worker wakes from a hold at the same instant; without this
            # the retry is a synchronized burst of `workers` searches inside
            # half a second, which is exactly the shape that re-trips a
            # throttle. Spread the release over a few seconds.
            time.sleep(random.uniform(0.0, 3.0))
        time.sleep(random.uniform(JITTER_LO, self.base))

    def backoff(self) -> float:
        with self._lock:
            p = self._penalty
            self._penalty = min(60.0, self._penalty * 2)
            self._hold_until = max(self._hold_until, time.monotonic() + p)
        return p

    def ease(self) -> None:
        """A successful search decays the penalty back toward its floor."""
        with self._lock:
            self._penalty = max(10.0, self._penalty * 0.9)


def resolve_one(name: str, city: str, country: str, ddgs, cfg: dict,
                blocked: re.Pattern) -> tuple[str | None, str]:
    """-> (domain, reason). domain is None unless a fetched page PROVES identity."""
    q = cfg["resolve_query"].format(name=name, city=city, country=country)
    try:
        results = list(ddgs.text(q, max_results=cfg.get("resolve_max_results", 8)))
    except Exception as e:
        return None, f"search-error:{type(e).__name__}"
    if not results:
        return None, "no-results"
    tokens = name_tokens(name)
    if len(tokens) < 1:
        return None, "name-too-generic"
    tried = 0
    for r in results:
        m = re.match(r"https?://(?:www\.)?([^/?#]+)", r.get("href", "") or "")
        if not m:
            continue
        host = m.group(1).lower()
        if DIRECTORY.search(host) or blocked.search(host):
            continue
        tried += 1
        if tried > cfg.get("resolve_max_fetches", MAX_FETCHES):
            break
        for scheme in ("https", "http"):
            html = fetch(f"{scheme}://{host}/")
            if html:
                break
        if not html:
            continue
        if verifies(html, tokens, cfg.get("resolve_min_name_tokens", 2), host):
            return host, "verified"
    return None, "unverified" if tried else "directories-only"


DEFAULT_WORKERS = 12
MAX_WORKERS = 24


def worker_count(cfg: dict) -> int:
    """RESOLVE_WORKERS env > sourcing.json resolve_workers > 12; clamped 1..24."""
    workers = int(os.environ.get("RESOLVE_WORKERS",
                                 str(cfg.get("resolve_workers", DEFAULT_WORKERS))))
    return max(1, min(workers, MAX_WORKERS))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config", default="{}", help="sourcing.json as a JSON string")
    a = ap.parse_args()
    run = Path(a.run_dir)
    cfg = json.loads(a.config) if a.config else {}
    cfg.setdefault("resolve_query", "{name} {city} official website")
    cfg.setdefault("resolve_max", 400)
    cfg.setdefault("resolve_min_name_tokens", 2)
    extra = cfg.get("resolve_blocklist") or []
    blocked = re.compile("|".join(re.escape(x) for x in extra) if extra else r"(?!x)x", re.I)

    rows: list[tuple[str, str, str, str]] = []
    for f in sorted(run.glob("unresolved-*.txt")):
        for line in f.read_text().splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 4 and parts[0]:
                rows.append(tuple(parts))          # name|ISO2|city|vertical
    if not rows:
        print("resolve: nothing to resolve (no unresolved-*.txt)")
        return

    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS      # deprecated fallback
            print("resolve: WARNING using deprecated duckduckgo_search; `pip install ddgs`")
        except ImportError:
            sys.exit("ABORT: resolve_domains needs the `ddgs` package (pip install ddgs).")

    cap = int(cfg["resolve_max"])
    dropped = max(0, len(rows) - cap)
    rows = rows[:cap]
    print(f"resolve: {len(rows)} name-only businesses -> searching + VERIFYING "
          f"(unproven domains are dropped, never guessed)")
    if dropped:
        # Never let a cap truncate silently: a capped run looks identical to a
        # run that simply found less. This matters more since places sources
        # default to Pro tier — ALL of their yield is name-only, so it all
        # arrives here rather than just OSM's leftovers.
        print(f"resolve: WARNING {dropped} name-only businesses were CAPPED OFF by "
              f"resolve_max={cap} and will not be resolved this run. Raise "
              f'"resolve_max" in sourcing.json to reach them.')
    workers = worker_count(cfg)
    pacer = Pacer(float(cfg.get("resolve_sleep", JITTER_HI)))
    local = threading.local()

    def _worker(row: tuple[str, str, str, str]) -> tuple[tuple[str, str, str, str], str | None, str]:
        name, iso, city, vertical = row
        if not hasattr(local, "ddgs"):
            local.ddgs = DDGS()       # one session per thread, never shared
        pacer.wait()
        dom, why = resolve_one(name, city, iso, local.ddgs, cfg, blocked)
        if why.startswith("search-error"):
            # Back the whole pool off and retry this ONE row once. Anything
            # still failing after that is recorded as the error it is — never
            # guessed around. A fresh DDGS session for the retry: a throttled
            # session tends to stay throttled.
            pacer.backoff()
            pacer.wait()
            local.ddgs = DDGS()
            dom, why = resolve_one(name, city, iso, local.ddgs, cfg, blocked)
        else:
            pacer.ease()
        return row, dom, why

    print(f"resolve: {workers} parallel workers (RESOLVE_WORKERS to override)")
    out, stats, emitted = [], {}, set()
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for row, dom, why in ex.map(_worker, rows):
            name, iso, city, vertical = row
            done += 1
            stats[why] = stats.get(why, 0) + 1
            if dom and dom not in emitted:
                emitted.add(dom)      # two OSM branches of one business -> one candidate
                out.append(f"{dom}|{name.replace('|',' ')}|{iso}|{vertical}|0")
            if done % 25 == 0:
                print(f"  {done}/{len(rows)} … {len(out)} verified so far")

    if out:
        (run / "candidates-batch-resolved-000.txt").write_text("\n".join(out) + "\n")
    rate = 100 * len(out) / len(rows) if rows else 0
    print(f"resolve: {len(out)}/{len(rows)} verified ({rate:.0f}%)  breakdown={stats}")


if __name__ == "__main__":
    main()
