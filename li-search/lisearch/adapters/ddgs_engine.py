"""ddgs — multi-engine web index via the `ddgs` library. The free workhorse.

WHY THIS EXISTS NEXT TO openweb
openweb talks to ONE engine's HTML page (DuckDuckGo) with stdlib urllib. That
engine answers a client it dislikes with an HTTP 202 interstitial and, once it
has decided that about an IP, keeps doing so for a while — which is exactly what
turned four fires in a row into zero leads on 2026-09-01. The `ddgs` library
rotates across Bing, Google, Yahoo, Yandex (and others) with the request shape
each one expects, so one engine sulking costs a little recall instead of all of
it. Measured on this machine, 2026-09-02, for one Dubai owner query: bing 10,
google 6, yahoo 7, yandex 6, duckduckgo 0 (throttled), brave 0.

WHY A SUBPROCESS
li-search is stdlib-only on purpose, and the operator's default python3 does not
have ddgs installed. Rather than make ddgs a hard dependency, this adapter looks
for ANY interpreter that can import it — config `python`, the env var
LI_SEARCH_DDGS_PYTHON, the running interpreter, python3 on PATH, and finally the
pipeline's venv at ../project/tools/venv, which happens to have it. If none can,
the provider is SKIPPED with the reason printed, never fatal. Nothing else is
shared with project/.

COMPLIANCE
The child script only calls DDGS().text(): it queries search engines and reads
their result lists. It never fetches a result URL, so it never contacts
linkedin.com. The parent parses the returned hrefs and nothing more.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import cache
from ..audience import Audience
from ..lead import canonical_account, country_from_url, make_lead
from .base import Adapter, location_from_snippet, split_headline, web_queries

ROOT = Path(__file__).resolve().parent.parent.parent

# Engines that answered on this machine on 2026-09-02, best first. "auto" is
# also valid and lets the library pick; it is slower and tries the throttled
# ones too, so the explicit list is the default. Measured the same day for one
# Dubai query: the four engines separately gave the SAME 13 profiles as one
# combined call, while Bing pages 2 and 3 each added 7 new — so depth (pages)
# is the recall lever here, not engine count. Default is 3 pages.
DEFAULT_BACKEND = "bing,google,yahoo,yandex"

CHILD = r'''
import json, sys, time
try:
    from ddgs import DDGS
except Exception as e:  # pragma: no cover
    print(json.dumps({"fatal": "import ddgs failed: %s" % e})); sys.exit(0)
job = json.load(sys.stdin)
d = DDGS()
streak = 0                      # consecutive REAL errors (not "no results")
for item in job["queries"]:
    q, start = item["q"], int(item.get("from_page", 1))
    for page in range(start, int(job.get("pages", 1)) + 1):
        out = {"q": q, "page": page}
        kw = dict(region=job.get("region", "wt-wt"), safesearch="off",
                  max_results=int(job.get("max_results", 30)),
                  backend=job.get("backend", "auto"))
        try:
            try:
                rows = d.text(q, page=page, **kw)
            except TypeError:           # older ddgs without a page kwarg
                rows = d.text(q, **kw) if page == 1 else []
            out["results"] = [{"title": r.get("title", ""), "href": r.get("href", ""),
                               "body": r.get("body", "")} for r in (rows or [])]
            streak = 0
        except Exception as e:
            msg = str(e)
            if "no results" in msg.lower():
                # A genuine empty (seed names are often empty on page 2+).
                # Cache it as such, or every fire re-spends the call.
                out["results"] = []
                streak = 0
            else:
                out["error"] = msg[:300]
                streak += 1
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush()
        if out.get("error"):
            if streak >= int(job.get("max_error_streak", 8)):
                print(json.dumps({"fatal": "throttled: %d consecutive engine errors, stopping "
                                  "so the rest is retried on the next fire" % streak}))
                sys.stdout.flush(); sys.exit(0)
            time.sleep(min(90.0, 5.0 * (2 ** (streak - 1))))   # 5, 10, 20, 40, 80, 90…
            break
        if len(out.get("results") or []) < 5:
            break                        # nothing more on later pages
        time.sleep(float(job.get("delay", 1.0)))
    time.sleep(float(job.get("delay", 1.0)))
'''


class DdgsAdapter(Adapter):
    name = "ddgs"
    kind = "index"
    env_key = None
    cost_note = "free, keyless. Rotates Bing/Google/Yahoo/Yandex via the ddgs library."
    doc = "https://pypi.org/project/ddgs/"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self._python: Optional[str] = None
        self._tried: List[str] = []

    # -- interpreter discovery ---------------------------------------------
    def _candidates(self) -> List[str]:
        c: List[str] = []
        if self.config.get("python"):
            c.append(str(self.config["python"]))
        if os.environ.get("LI_SEARCH_DDGS_PYTHON"):
            c.append(os.environ["LI_SEARCH_DDGS_PYTHON"])
        c.append(sys.executable)
        w = shutil.which("python3")
        if w:
            c.append(w)
        c.append(str(ROOT.parent / "project" / "tools" / "venv" / "bin" / "python"))
        seen, out = set(), []
        for x in c:
            if x and x not in seen and Path(x).exists():
                seen.add(x)
                out.append(x)
        return out

    def interpreter(self) -> Optional[str]:
        if self._python is not None:
            return self._python or None
        for py in self._candidates():
            self._tried.append(py)
            try:
                r = subprocess.run([py, "-c", "import ddgs"], capture_output=True, timeout=30)
            except Exception:
                continue
            if r.returncode == 0:
                self._python = py
                return py
        self._python = ""
        return None

    def available(self) -> bool:
        return self.interpreter() is not None

    def why_unavailable(self) -> str:
        return ("no interpreter can import ddgs (tried %s). `pip3 install ddgs`, or set "
                "LI_SEARCH_DDGS_PYTHON=/path/to/python that has it."
                % (", ".join(self._tried) or "nothing"))

    # -- search ----------------------------------------------------------------
    def _key(self, q: str, page: int, backend: str, n: int) -> Dict[str, Any]:
        return {"q": q, "backend": backend, "page": page, "n": n}

    def _migrate(self, q: str, backend: str, n: int, ttl: int) -> None:
        """Before 2026-09-02 a query was cached as one blob keyed on its page
        COUNT, so deepening from 3 to 4 pages refetched everything. Split any
        such blob into per-page entries once, so nothing already paid for is
        fetched again."""
        for old_pages in (3, 1, 2):
            blob = cache.get(self.name, {"q": q, "backend": backend, "pages": old_pages, "n": n}, ttl)
            if blob:
                for rec in blob:
                    key = self._key(q, int(rec.get("page", 1)), backend, n)
                    if cache.get(self.name, key, ttl) is None:
                        cache.put(self.name, key, rec)
                return

    def search(self, audience: Audience, limit: int, ttl: int = 0) -> List[Dict[str, Any]]:
        py = self.interpreter()
        if not py:
            return []
        max_queries = int(self.config.get("max_queries", 150))
        per_query = int(self.config.get("max_results", 30))
        pages = int(self.config.get("pages", 3))
        backend = str(self.config.get("backend", DEFAULT_BACKEND))
        queries = web_queries(audience, max_queries, max_titles=int(self.config.get("max_titles", 12)))

        # Per (query, page) cache. A query is "complete" when a cached page
        # came back short (the engine had no more) or every requested page is
        # cached; otherwise the child resumes from the first missing page.
        plan: List[Dict[str, Any]] = []
        pages_by_q: Dict[str, List[Dict[str, Any]]] = {}
        for q in queries:
            self._migrate(q, backend, per_query, ttl)
            recs: List[Dict[str, Any]] = []
            need_from = 0
            for page in range(1, pages + 1):
                hit = cache.get(self.name, self._key(q, page, backend, per_query), ttl)
                if hit is None:
                    need_from = page
                    break
                recs.append(hit)
                if len(hit.get("results") or []) < 5:
                    break
            pages_by_q[q] = recs
            if need_from:
                plan.append({"q": q, "from_page": need_from})

        fetched = set()
        errors = 0
        if plan:
            job = {"queries": plan, "max_results": per_query, "pages": pages,
                   "backend": backend, "region": self.config.get("region", "wt-wt"),
                   "delay": float(self.config.get("delay_seconds", 1.5)),
                   "max_error_streak": int(self.config.get("max_error_streak", 8))}
            proc = subprocess.Popen([py, "-c", CHILD], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                out, err = proc.communicate(json.dumps(job),
                                            timeout=int(self.config.get("timeout_seconds", 3600)))
            except subprocess.TimeoutExpired as e:
                proc.kill()
                out = e.stdout or ""
                if isinstance(out, bytes):
                    out = out.decode("utf-8", "replace")
                print("    ddgs: timed out; kept what had streamed back")
            for line in (out or "").splitlines():
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("fatal"):
                    print("    ddgs: %s" % rec["fatal"])
                    break
                if rec.get("error"):
                    errors += 1
                    continue
                q, page = rec["q"], int(rec.get("page", 1))
                cache.put(self.name, self._key(q, page, backend, per_query), rec)
                pages_by_q.setdefault(q, []).append(rec)
                fetched.add(q)
            if errors:
                print("    ddgs: %d quer%s returned an engine error (not cached)."
                      % (errors, "y" if errors == 1 else "ies"))

        leads: List[Dict[str, Any]] = []
        seen = set()
        for q in queries:
            age = 0 if q in fetched else (cache.age_days(
                self.name, self._key(q, 1, backend, per_query)) or 0)
            for rec in pages_by_q.get(q, []):
                for r in rec.get("results") or []:
                    href = r.get("href", "")
                    acct = canonical_account(href)
                    if not acct or acct in seen:
                        continue
                    seen.add(acct)
                    parsed = split_headline(r.get("title", ""))
                    leads.append(make_lead(
                        source=self.name,
                        account=acct,
                        full_name=parsed["full_name"],
                        title=parsed["title"],
                        company=parsed["company"],
                        location=location_from_snippet(r.get("body", "")),
                        country=country_from_url(href),
                        summary=r.get("body", ""),
                        freshness_days=age,
                        raw_query=q,
                    ))
            if len(leads) >= limit:
                break
        return leads[:limit]
