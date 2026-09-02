"""li-search CLI.

  define    store a target audience (this tool's memory)
  list      show stored audiences
  show      print one audience
  fire      run a stored audience -> leads with their LinkedIn account names
  bakeoff   Stage 1 of the research brief: same audience, every provider,
            compare recall/completeness BEFORE committing to a vendor
  providers what is configured, what is not, and what each costs
  suppress  add an account to the never-emit list (erasure requests)
  cache     clear the cache
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import audience as aud
from . import cache as cache_mod
from .adapters import ALL, DEFAULT_ENABLED, build, default_providers
from .fire import done_line, fire, score
from .lead import canonical_account
from .export import export

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> Dict[str, Any]:
    """config.json is optional. Env vars win, so a key never has to be written
    to disk in this repo."""
    p = ROOT / "config.json"
    cfg: Dict[str, Any] = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise SystemExit("config.json is not valid JSON: %s" % e)
    # A .env at the repo root is the operator's habit here; honour it without
    # importing anything, and never overwrite an already-set env var.
    for env_path in (ROOT / ".env", ROOT.parent / ".env"):
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return cfg


def csvlist(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def cmd_define(args) -> int:
    a = aud.new_audience(
        name=args.name,
        industry=args.industry or "",
        titles=csvlist(args.titles),
        seniority=csvlist(args.seniority),
        countries=csvlist(args.countries),
        cities=csvlist(args.cities),
        keywords=csvlist(args.keywords),
        exclude_titles=csvlist(args.exclude_titles),
        exclude_keywords=csvlist(args.exclude_keywords),
        headcount=[int(x) for x in csvlist(args.headcount)],
        description=args.description or "",
        notes=args.notes or "",
        companies=csvlist(args.companies),
        people=csvlist(args.people),
    )
    existing = aud.AUDIENCES / ("%s.json" % a["slug"])
    if existing.exists() and not args.force:
        raise SystemExit(
            "audience %r already stored (%s). Re-run with --force to overwrite, "
            "or pick another name." % (a["slug"], existing))
    p = aud.save(a)
    print("Stored audience %r -> %s" % (a["slug"], p))
    print()
    print("  titles:    %s" % ", ".join(a.titles()[:10]))
    print("  geo:       %s" % ", ".join(a.geo_terms()[:10]))
    print("  industry:  %s" % (a.get("industry") or "-"))
    print("  keywords:  %s" % (", ".join(a.get("keywords") or []) or "-"))
    print("  excluding: %s" % (", ".join((a.get("exclude_titles") or []) +
                                         (a.get("exclude_keywords") or [])) or "-"))
    print("  seeds:     %d compan%s, %d named person(s)" % (
        len(a["companies"]), "y" if len(a["companies"]) == 1 else "ies", len(a["people"])))
    print()
    print("Fire it with:  ./li-search fire %s" % a["slug"])
    return 0


def cmd_list(args) -> int:
    rows = aud.load_all()
    if not rows:
        print("No stored audiences yet.\nDefine one:  ./li-search define \"<name>\" "
              "--industry \"...\" --countries AE,SA --cities Dubai,Riyadh")
        return 0
    print("%-28s %-9s %-26s %s" % ("SLUG", "FIRES", "GEO", "INDUSTRY"))
    for a in rows:
        print("%-28s %-9d %-26s %s" % (
            a["slug"], len(a.get("fired") or []),
            (", ".join(a.geo_terms()[:3]))[:26], (a.get("industry") or "-")[:40]))
    return 0


def cmd_show(args) -> int:
    print(json.dumps(aud.load(args.slug), indent=2, ensure_ascii=False))
    return 0


def cmd_providers(args) -> int:
    cfg = load_config()
    print("%-12s %-10s %-9s %s" % ("PROVIDER", "KIND", "READY", "NOTE"))
    for cls in ALL:
        ad = cls(cfg.get(cls.name, {}))
        ready = "yes" if ad.available() else "NO"
        note = ad.cost_note if ad.available() else ad.why_unavailable()
        print("%-12s %-10s %-9s %s" % (ad.name, ad.kind, ready, note))
    print()
    print("Fires by default:   %s  (every provider that can run right now)"
          % (", ".join(default_providers(cfg)) or "nothing"))
    print("Priority order:     %s" % ", ".join(DEFAULT_ENABLED))
    print()
    print("This tool never contacts linkedin.com and never uses a logged-in")
    print("session. See lisearch/compliance.py — it is enforced, not advisory.")
    return 0


def _print_table(leads: List[Dict[str, Any]], top: int) -> None:
    if not leads:
        return
    print()
    print("%-4s %-2s %-30s %-24s %-28s %-22s %-5s %s" %
          ("#", "Q", "LINKEDIN ACCOUNT", "NAME", "TITLE", "COMPANY", "SCORE", "MATCH"))
    print("-" * 160)
    for i, l in enumerate(leads[:top], 1):
        print("%-4d %-2s %-30s %-24s %-28s %-22s %-5.1f %s" % (
            i,
            "Y" if l.get("qualified") else "-",
            l["linkedin_account"][:30],
            (l.get("full_name") or "")[:24],
            (l.get("title") or "")[:28],
            (l.get("company") or "")[:22],
            l.get("score") or 0.0,
            (l.get("match") or "")[:40],
        ))
    if len(leads) > top:
        print("... %d more in leads.csv" % (len(leads) - top))


def cmd_fire(args) -> int:
    cfg = load_config()
    a = aud.load(args.slug)
    if args.queries:
        # Per-fire query budget for the web-index providers, so a widening
        # pass is a flag rather than a config edit.
        for name in ("ddgs", "openweb", "exa"):
            cfg.setdefault(name, {})["max_queries"] = args.queries
    if args.pages:
        cfg.setdefault("ddgs", {})["pages"] = args.pages
    providers = csvlist(args.providers) or default_providers(cfg)
    if not providers:
        providers = list(DEFAULT_ENABLED)
    res = fire(a, providers, cfg, limit=args.max, ttl=0 if args.no_cache else args.ttl * 86400,
               dry_run=args.dry_run, quiet=args.quiet)

    if args.quiet:
        # Token-discipline mode for a session running /li-fire: one line to
        # relay, one path to name. Rows live in the files.
        print(done_line(res))
        if res.get("run_dir"):
            aud.record_fire(a, res["run_id"], len(res["leads"]))
            print("run_dir: %s  (leads.csv, accounts-qualified.txt, summary.json)" % res["run_dir"])
        elif not args.dry_run:
            print("nothing written (fetched 0; prior run preserved)")
        return 0

    print()
    print("  fetched              : %d" % res["raw"])
    print("  carried over         : %d  (from %d prior run(s))" % (res["carried_over"], res["prior_runs"]))
    print("  NEW accounts         : %d" % res["new"])
    print("  unique               : %d" % res["unique"])
    print("  excluded / suppressed: %d / %d" % (res["excluded"], res["suppressed"]))
    print("  KEPT                 : %d" % len(res["leads"]))
    print("  QUALIFIED            : %d  (title + geo + industry all shown in the row)" % res["qualified"])
    if res["eu_records"]:
        print("  EEA/UK flagged       : %d  — GDPR Art.14 notice and lawful basis apply." % res["eu_records"])
    if args.dry_run:
        print("\nDRY RUN — nothing fetched, nothing written.")
        return 0

    _print_table(res["leads"], args.top)
    if res.get("run_dir"):
        aud.record_fire(a, res["run_id"], len(res["leads"]))
        print()
        print(done_line(res))
        print("run_dir: %s" % res["run_dir"])
        print("  leads.csv · leads.json · accounts.txt · accounts-qualified.txt · summary.json · status.txt")
    if not res["raw"]:
        print()
        # Say which of the three very different causes actually applies. "No
        # results" and "every provider refused to answer" demand opposite
        # responses, and guessing wrong sends you off rewriting a fine audience.
        if res.get("stopped"):
            print("Nothing fetched because %d provider(s) STOPPED mid-run, not because the"
                  % len(res["stopped"]))
            print("audience is empty:")
            for name, why in res["stopped"].items():
                print("  %-11s %s" % (name, why))
        elif res.get("skipped") and len(res["skipped"]) == len(providers):
            print("Nothing fetched: every provider was skipped.")
            print("Run `./li-search providers` to see why each one cannot run.")
        else:
            print("Nothing fetched and every provider answered — the audience is genuinely")
            print("returning nothing. Widen it: fewer excluded titles, more cities,")
            print("or a broader --industry. `./li-search show %s`" % a["slug"])
    return 0


def cmd_bakeoff(args) -> int:
    """Stage 1 of the brief, made runnable: the same audience through every
    provider separately, so coverage is measured on YOUR market before a vendor
    is chosen. No published benchmark answers the MENA question for you."""
    cfg = load_config()
    a = aud.load(args.slug)
    print("BAKE-OFF  audience=%s  sample=%d per provider\n" % (a["slug"], args.sample))
    rows = []
    for cls in ALL:
        ad = cls(cfg.get(cls.name, {}))
        if not ad.available():
            rows.append((ad.name, "-", "-", "-", "-", ad.why_unavailable()))
            continue
        try:
            got = ad.search(a, limit=args.sample, ttl=0 if args.no_cache else args.ttl * 86400)
        except SystemExit as e:
            rows.append((ad.name, "-", "-", "-", "-", "error: %s" % e))
            continue
        except Exception as e:
            rows.append((ad.name, "-", "-", "-", "-", "error: %s" % e))
            continue
        n = len(got) or 1
        rows.append((
            ad.name,
            str(len(got)),
            "%d%%" % (100 * sum(1 for l in got if l.get("full_name")) / n),
            "%d%%" % (100 * sum(1 for l in got if l.get("company")) / n),
            "%d%%" % (100 * sum(1 for l in got if l.get("email")) / n),
            "mean score %.2f" % (sum(score(l, a) for l in got) / n),
        ))
    print("%-12s %-8s %-8s %-9s %-8s %s" %
          ("PROVIDER", "ROWS", "NAME%", "COMPANY%", "EMAIL%", "NOTE"))
    for r in rows:
        print("%-12s %-8s %-8s %-9s %-8s %s" % r)
    print()
    print("Brief's threshold: drop any provider under ~50%% usable recall on YOUR")
    print("market, whatever its published benchmark says.")
    return 0


def cmd_suppress(args) -> int:
    acct = canonical_account(args.who) or args.who.strip().lower()
    f = ROOT / "suppression.txt"
    cur = f.read_text(encoding="utf-8") if f.exists() else \
        "# Never emit these. One per line: LinkedIn account name, profile URL, or email.\n"
    if acct in cur:
        print("%s already suppressed." % acct)
        return 0
    f.write_text(cur.rstrip("\n") + "\n" + acct + "\n", encoding="utf-8")
    print("Suppressed %s — it will be dropped from every future fire." % acct)
    return 0


def cmd_export(args) -> int:
    res = export(args.name, csvlist(args.audiences), args.take)
    print("EXPORT %s: batch %d, %d delivered, %d total in owners.csv, %d qualified still undelivered"
          % (args.name, res["batch"], res["delivered"], res["total"], res["remaining"]))
    print("dir: %s" % res["dir"])
    if args.table:
        print("| # | Audience | LinkedIn account | Name | Title | Company |")
        print("|---|---|---|---|---|---|")
        for r in res["rows"]:
            print("| %s | %s | %s | %s | %s | %s |" % (
                r["n"], r["audience"].replace("lb-", "")[:22], r["linkedin_account"],
                r["full_name"], (r["title"] or "—")[:38], (r["company"] or "—")[:30]))
    return 0


def cmd_cache(args) -> int:
    if args.clear:
        print("Cleared %d cached response(s)." % cache_mod.clear())
    else:
        n = sum(1 for _ in (ROOT / "cache").rglob("*.json"))
        print("%d cached response(s). Clear with: ./li-search cache --clear" % n)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="li-search",
        description="Standalone LinkedIn people search. Separate from project/. "
                    "Sources people from licensed and indexed providers; never "
                    "scrapes linkedin.com, never uses a logged-in session.")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("define", help="store a target audience")
    d.add_argument("name")
    d.add_argument("--industry", help='e.g. "boutique hotels"')
    d.add_argument("--titles", help="comma list; overrides the owner preset")
    d.add_argument("--seniority", default="", help="owner|clevel|director|manager (comma list)")
    d.add_argument("--countries", help="ISO-2 comma list, e.g. AE,SA,QA")
    d.add_argument("--cities", help="comma list — far more selective than countries")
    d.add_argument("--keywords", help="comma list")
    d.add_argument("--exclude-titles", dest="exclude_titles")
    d.add_argument("--exclude-keywords", dest="exclude_keywords")
    d.add_argument("--headcount", help="min,max")
    d.add_argument("--companies", help="comma list of firm names the brief NAMES — one seed query each")
    d.add_argument("--people", help="comma list of named decision-makers — one seed query each; a name match qualifies")
    d.add_argument("--description")
    d.add_argument("--notes")
    d.add_argument("--force", action="store_true")
    d.set_defaults(fn=cmd_define)

    l = sub.add_parser("list", help="list stored audiences"); l.set_defaults(fn=cmd_list)
    s = sub.add_parser("show", help="print one audience"); s.add_argument("slug"); s.set_defaults(fn=cmd_show)
    pr = sub.add_parser("providers", help="provider readiness and cost"); pr.set_defaults(fn=cmd_providers)

    f = sub.add_parser("fire", help="run a stored audience and output leads")
    f.add_argument("slug")
    f.add_argument("--max", type=int, default=2000,
                   help="max leads kept AND the point each provider stops fetching (default 2000). "
                        "The operator wants as many as possible; a low cap silently truncates the matrix.")
    f.add_argument("--providers", help="comma list; default = every provider that can run "
                                       "(priority %s)" % ",".join(DEFAULT_ENABLED))
    f.add_argument("--top", type=int, default=10,
                   help="preview rows to print (default 10; the files hold all of them)")
    f.add_argument("--quiet", "-q", action="store_true",
                   help="print only the DONE line and the run dir — the mode a Claude session should use")
    f.add_argument("--ttl", type=int, default=30, help="cache TTL in days (default 30)")
    f.add_argument("--no-cache", action="store_true")
    f.add_argument("--pages", type=int, default=0,
                   help="result pages per query for ddgs (default 3); deepening only fetches the new pages")
    f.add_argument("--queries", type=int, default=0,
                   help="query budget per web-index provider for this fire (default: provider config, ddgs 150)")
    f.add_argument("--dry-run", action="store_true", help="plan only, spend nothing")
    f.set_defaults(fn=cmd_fire)

    b = sub.add_parser("bakeoff", help="compare providers on YOUR market")
    b.add_argument("slug")
    b.add_argument("--sample", type=int, default=50)
    b.add_argument("--ttl", type=int, default=30)
    b.add_argument("--no-cache", action="store_true")
    b.set_defaults(fn=cmd_bakeoff)

    sup = sub.add_parser("suppress", help="never emit this person again")
    sup.add_argument("who", help="account name, profile URL, or email")
    sup.set_defaults(fn=cmd_suppress)

    e = sub.add_parser("export", help="deliver the next batch of qualified accounts into results/<name>/")
    e.add_argument("name", help="brief name, e.g. shughol-lebanon")
    e.add_argument("--audiences", required=True, help="comma list of audience slugs, priority order")
    e.add_argument("--take", type=int, default=50)
    e.add_argument("--table", action="store_true", help="also print the batch as a markdown table")
    e.set_defaults(fn=cmd_export)

    c = sub.add_parser("cache", help="inspect or clear the cache")
    c.add_argument("--clear", action="store_true")
    c.set_defaults(fn=cmd_cache)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
