#!/usr/bin/env python3
"""Stage 2 (deterministic source adapter): enumerate a LISTING on the open web.

WHY THIS EXISTS
`source_overpass.py` can only see what OpenStreetMap maps: physical premises.
Whole ICPs have no map presence at all — online stores, agencies, association
members, licensed-operator registers, portal rosters. Probed 2026-07-27, Salla's
68,000 merchants are reachable by NONE of the usual infrastructure tricks (no
public store directory, stores sit behind a wildcard cert so Certificate
Transparency shows only Salla's own hosts, and the Common Crawl index surfaces
salla.sa itself rather than merchant subdomains).

So Stage 2 is not one source, it is a SET of deterministic adapters. This is the
generic one: point it at any paginated listing, describe how to pull a row's name
and link with a regex, and it emits the standard candidate contract. No LLM, no
agent, no per-campaign code — a new source is config in the branch's
sourcing.json, exactly like a new place or a new selector.

Anything it can only get a NAME for (no outbound link) is handed to Stage 2.5
(resolve_domains.py) to resolve and verify, same as a name-only OSM element.

OUTPUT  <run-dir>/candidates-batch-<prefix>-NNN.txt   `domain|Name|ISO2|vertical|0`
        <run-dir>/unresolved-<prefix>-NNN.txt         `Name|ISO2|City|vertical`

CONFIG (one entry in sourcing.json "sources"):
  {"type": "directory",
   "url": "https://example.com/members?page={page}",   # {page} optional
   "pages": 20,                     # how many pages to walk (default 1)
   "start_page": 1,
   "row": "<li class=\\"member\\">.*?</li>",           # optional row splitter
   "name": "<h3[^>]*>(.*?)</h3>",                      # capture group 1 = name
   "link": "href=\\"(https?://[^\\"]+)\\"",            # optional, group 1 = url
   "country": "SA", "vertical": "merchant", "city": "",
   "skip_hosts": ["example.com"],   # never treat these as a business domain
   "sleep": 1.0}
"""
from __future__ import annotations
import argparse
import html as _html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve_domains import DIRECTORY as _AGGREGATORS   # shared never-a-business list

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url: str, timeout: int = 25) -> str | None:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en,ar;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            raw = r.read(2_000_000)
        for enc in ("utf-8", "windows-1256", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    except Exception:
        return None
    return None


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def host_of(url: str, base: str = "") -> str | None:
    if url.startswith("/") and base:
        url = urllib.parse.urljoin(base, url)
    m = re.match(r"https?://(?:www\.)?([^/?#:]+)", url)
    return m.group(1).lower() if m else None


def harvest(src: dict) -> tuple[list[str], list[str]]:
    """-> (candidate_lines, unresolved_lines) for one configured listing."""
    name_re = re.compile(src["name"], re.S | re.I)
    row_re = re.compile(src["row"], re.S | re.I) if src.get("row") else None
    link_re = re.compile(src["link"], re.S | re.I) if src.get("link") else None
    skip = {h.lower() for h in (src.get("skip_hosts") or [])}
    country = (src.get("country") or "").upper()
    vertical = src.get("vertical") or "business"
    city = src.get("city") or ""

    cands, unres, seen_dom, seen_name = [], [], set(), set()
    pages = int(src.get("pages", 1))
    start = int(src.get("start_page", 1))
    for p in range(start, start + pages):
        url = src["url"].replace("{page}", str(p))
        page = fetch(url)
        if page is None:
            print(f"  page {p}: fetch failed ({url})")
            continue
        chunks = [m.group(0) for m in row_re.finditer(page)] if row_re else [page]
        found = 0
        for chunk in chunks:
            for nm in name_re.finditer(chunk):
                name = clean(nm.group(1) if nm.groups() else nm.group(0))
                if not name or len(name) < 2 or name.lower() in seen_name:
                    continue
                seen_name.add(name.lower())
                found += 1
                dom = None
                if link_re:
                    lm = link_re.search(chunk)
                    if lm:
                        h = host_of(lm.group(1), url)
                        # a link back into the directory itself is not a business
                        if h and h not in skip and not _AGGREGATORS.search(h):
                            dom = h
                if dom and dom not in seen_dom:
                    seen_dom.add(dom)
                    cands.append(f"{dom}|{name.replace('|',' ')}|{country}|{vertical}|0")
                elif not dom:
                    # name only -> Stage 2.5 resolves + verifies the domain
                    unres.append(f"{name.replace('|',' ')}|{country}|{city}|{vertical}")
        print(f"  page {p}: {found} rows  (running: {len(cands)} linked, {len(unres)} name-only)")
        if not found:
            break                      # ran off the end of the listing
        time.sleep(float(src.get("sleep", 1.0)))
    return cands, unres


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--source", required=True, help="one source object as JSON")
    ap.add_argument("--batch-prefix", default="dir")
    a = ap.parse_args()
    run = Path(a.run_dir)
    run.mkdir(parents=True, exist_ok=True)
    src = json.loads(a.source)
    for req in ("url", "name"):
        if not src.get(req):
            sys.exit(f'ABORT: directory source needs "{req}"')

    cands, unres = harvest(src)
    if cands:
        (run / f"candidates-batch-{a.batch_prefix}-001.txt").write_text("\n".join(cands) + "\n")
    if unres:
        (run / f"unresolved-{a.batch_prefix}-001.txt").write_text("\n".join(unres) + "\n")
    print(f"directory[{a.batch_prefix}]: {len(cands)} with a domain, "
          f"{len(unres)} name-only handed to Stage 2.5")
    if not cands and not unres:
        sys.exit("ABORT: directory source produced nothing — check url/name/row regexes.")


if __name__ == "__main__":
    main()
