"""EXPORT — the one place delivered leads live.

`fire` writes per-audience run folders; that is provenance, not a deliverable.
`export <name>` merges the QUALIFIED rows of one or more audiences into
results/<name>/ — a cumulative master file plus one file per batch — so the
operator always finds every account ever delivered for a brief in one folder,
numbered, never duplicated across batches.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"

FIELDS = ["n", "batch", "delivered_at", "audience", "linkedin_account", "linkedin_url",
          "full_name", "title", "company", "location", "match", "score", "sources", "status"]


def latest_run(slug: str) -> Path:
    runs = sorted(RUNS.glob("*-%s" % slug))
    if not runs:
        raise SystemExit("no run folder for audience %r — fire it first" % slug)
    return runs[-1]


def pool(audiences: List[str]) -> List[Dict[str, Any]]:
    """Qualified rows across the audiences, in audience order, best first,
    seeded (brief-named) rows ahead of keyword-qualified ones."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for i, slug in enumerate(audiences):
        f = latest_run(slug) / "leads.json"
        for l in json.loads(f.read_text(encoding="utf-8")).get("leads") or []:
            if not l.get("qualified") or l["linkedin_account"] in seen:
                continue
            seen.add(l["linkedin_account"])
            m = l.get("match") or ""
            l = dict(l, audience=slug, _seeded=m.startswith("person=") or "company:" in m, _order=i)
            out.append(l)
    out.sort(key=lambda l: (not l["_seeded"], l["_order"], -(l.get("score") or 0)))
    return out


def export(name: str, audiences: List[str], take: int) -> Dict[str, Any]:
    d = RESULTS / name
    d.mkdir(parents=True, exist_ok=True)
    master = d / "owners.csv"
    have: Dict[str, Dict[str, str]] = {}
    if master.exists():
        for row in csv.DictReader(master.open(encoding="utf-8")):
            have[row["linkedin_account"]] = row
    batch = 1 + max([int(r["batch"]) for r in have.values()] or [0])
    start = 1 + max([int(r["n"]) for r in have.values()] or [0])
    fresh = [l for l in pool(audiences) if l["linkedin_account"] not in have][:take]
    now = time.strftime("%Y-%m-%d")
    rows: List[Dict[str, Any]] = []
    for k, l in enumerate(fresh):
        rows.append({
            "n": start + k, "batch": batch, "delivered_at": now, "audience": l["audience"],
            "linkedin_account": l["linkedin_account"], "linkedin_url": l["linkedin_url"],
            "full_name": l.get("full_name") or "", "title": l.get("title") or "",
            "company": l.get("company") or "", "location": l.get("location") or "",
            "match": l.get("match") or "", "score": l.get("score") or 0,
            "sources": "+".join(l.get("sources") or []),
            "status": "",      # set to 'withdrawn' by hand if a row is later found off-spec
        })
    if rows:
        new_file = not master.exists()
        with master.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(rows)
        with (d / ("batch-%02d.csv" % batch)).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        (d / "accounts.txt").write_text(
            "\n".join(list(have) + [r["linkedin_account"] for r in rows]) + "\n", encoding="utf-8")
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# %s — delivered LinkedIn owner accounts\n\n"
            "- `owners.csv`   every account ever delivered for this brief, numbered, never repeated\n"
            "- `batch-NN.csv` each delivery as it was handed over\n"
            "- `accounts.txt` just the LinkedIn account names, one per line\n\n"
            "Rebuilt by: `./li-search export %s --audiences %s --take 50`\n"
            "Source runs: `runs/<date>-<audience>/` (provenance, all rows incl. unqualified).\n"
            % (name, name, ",".join(audiences)), encoding="utf-8")
    return {"dir": str(d), "batch": batch, "delivered": len(rows), "total": len(have) + len(rows),
            "remaining": max(0, len([l for l in pool(audiences) if l["linkedin_account"] not in have]) - len(rows)),
            "rows": rows}
