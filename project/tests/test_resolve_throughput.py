"""resolve_domains.py throughput knobs (2026-09-02).

Measured: 800 names -> 24 minutes -> 168 domains on 8 workers, with a 0.3-1.2 s
jitter per search, up to 3 verification fetches per name, 12 s each (https then
http, so a dead host cost 24 s). Now: 12 workers by default (cap 24), jitter
0.15-0.6 s, 8 s fetch timeout, at most 2 hosts fetched per name. The name-token
verification itself is untouched — these tests pin the knobs and prove that the
accept/reject decision is unchanged. Offline: search and fetch are faked.
"""
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import resolve_domains as rd  # noqa: E402

NOMATCH = re.compile(r"(?!x)x")     # the "no extra blocklist" pattern main() builds


def test_worker_defaults_and_clamp(monkeypatch):
    monkeypatch.delenv("RESOLVE_WORKERS", raising=False)
    assert rd.worker_count({}) == 12
    assert rd.worker_count({"resolve_workers": 16}) == 16
    assert rd.worker_count({"resolve_workers": 99}) == 24, "cap is 24"
    assert rd.worker_count({"resolve_workers": 0}) == 1
    monkeypatch.setenv("RESOLVE_WORKERS", "20")
    assert rd.worker_count({"resolve_workers": 4}) == 20, "env wins over the fixture"
    monkeypatch.setenv("RESOLVE_WORKERS", "64")
    assert rd.worker_count({}) == 24


def test_jitter_window_is_015_to_06(monkeypatch):
    seen = []
    monkeypatch.setattr(rd.random, "uniform", lambda a, b: seen.append((a, b)) or 0.0)
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    rd.Pacer(rd.JITTER_HI).wait()
    assert seen == [(0.15, 0.6)]


def test_fetch_default_timeout_is_8s(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        raise OSError("offline")

    monkeypatch.setattr(rd.urllib.request, "urlopen", fake_urlopen)
    assert rd.fetch("https://example.test/") is None
    assert captured["timeout"] == 8


def test_at_most_two_hosts_are_fetched_per_name(monkeypatch):
    fetched = []
    monkeypatch.setattr(rd, "fetch", lambda url, timeout=rd.FETCH_TIMEOUT: fetched.append(url) or None)

    class DDG:
        def text(self, q, max_results=8):
            return [{"href": f"https://host{i}.test/page"} for i in range(5)]

    cfg = {"resolve_query": "{name} {city} official website", "resolve_min_name_tokens": 2}
    dom, why = rd.resolve_one("Eternal Smile Dental", "Riyadh", "SA", DDG(), cfg, NOMATCH)
    assert dom is None and why == "unverified"
    hosts = {u.split("//")[1].split("/")[0] for u in fetched}
    assert hosts == {"host0.test", "host1.test"}, fetched


def test_verification_decision_is_unchanged(monkeypatch):
    """The knobs never change WHICH domain is accepted: the same name-token
    rule decides, and a match on the second (still fetched) host still wins."""
    pages = {
        "https://wrong.test/": "<html><title>Some Other Place</title><body>nothing here</body></html>",
        "https://eternal-smile.test/": "<html><title>Eternal Smile Dental Riyadh</title>"
                                       "<body>Welcome to Eternal Smile</body></html>",
    }
    monkeypatch.setattr(rd, "fetch", lambda url, timeout=rd.FETCH_TIMEOUT: pages.get(url))

    class DDG:
        def text(self, q, max_results=8):
            return [{"href": "https://facebook.com/x"},          # directory: skipped, not counted
                    {"href": "https://wrong.test/a"},
                    {"href": "https://eternal-smile.test/b"},
                    {"href": "https://never-reached.test/c"}]

    cfg = {"resolve_query": "{name} {city} official website", "resolve_min_name_tokens": 2}
    dom, why = rd.resolve_one("Eternal Smile Dental", "Riyadh", "SA", DDG(), cfg, NOMATCH)
    assert (dom, why) == ("eternal-smile.test", "verified")
