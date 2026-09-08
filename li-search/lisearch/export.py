"""EXPORT — the one place delivered leads live, and the one place spend happens.

`fire` writes per-audience run folders; that is provenance, not a deliverable.
`export <name>` merges the QUALIFIED rows of one or more audiences into
results/<name>/ — a cumulative master file plus one file per batch — so the
operator always finds every account ever delivered for a brief in one folder,
numbered, never duplicated across batches.

`--verify` puts [verify.py]'s gate in front of that hand-over. It is off by
default (this tool defaults to zero spend), and it changes only which rows are
delivered — never how they were found, never what a row's deterministic
`title` / `company` / `match` say. Sourcing stays free and deterministic;
the gate buys certainty about the ~50 rows a batch actually hands over, at
~$0.0056 each. See verify.py for the measurement that chose this position.

Delivery is where it belongs because delivery is what the LinkedIn channel
consumes: 8→18 invites/day, 90/week. Precision on those is the scarce good,
not the size of the pool.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import audience as aud
from . import verify as verify_mod

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
RESULTS = ROOT / "results"

# The verification columns are APPENDED, never inserted. A column landing in the
# middle of this list once already (2026-09-03, `strength`) left rows appended
# against the older header one field out of step, and cost a handoff
# (project/tools/scripts/import_lisearch.py::_num). New columns go on the end,
# and `_migrate_master` rewrites the header rather than appending under it.
FIELDS = ["n", "batch", "delivered_at", "audience", "linkedin_account", "linkedin_url",
          "full_name", "title", "company", "location", "strength", "match", "score", "sources", "status",
          "verified", "verified_role", "verified_sector", "verify_source"]

VERIFY_FIELDS = ["verified", "verified_role", "verified_sector", "verify_source"]


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
    out.sort(key=lambda l: (not l["_seeded"], l.get("strength") != "strong", l["_order"], -(l.get("score") or 0)))
    return out


def _migrate_master(master: Path) -> int:
    """Rewrite owners.csv under the current FIELDS header when columns were added.

    Appending rows with new columns UNDER an older header is the exact defect
    that shifted every field after `strength` on 2026-09-03 and aborted a
    handoff. So the header is migrated first: existing rows keep every value
    they had (matched by name, not position), new columns arrive empty, and any
    legacy column this version no longer writes is preserved on the end rather
    than silently dropped. Idempotent — a no-op once the header is current."""
    if not master.exists():
        return 0
    with master.open(encoding="utf-8", newline="") as fh:
        rdr = csv.DictReader(fh)
        old = list(rdr.fieldnames or [])
        rows = list(rdr)
    if old == FIELDS:
        return 0
    fields = FIELDS + [c for c in old if c and c not in FIELDS]
    tmp = master.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items() if k in fields})
    tmp.replace(master)
    return len(rows)


def _verify_stats() -> Dict[str, Any]:
    return {"ran": False, "calls": 0, "cached": 0, "confirmed": 0,
            "unverified": 0, "rejected": 0, "cost": 0.0, "target": ""}


def _audiences(slugs: List[str]) -> List[Any]:
    out = []
    for s in slugs:
        try:
            out.append(aud.load(s))
        except SystemExit:
            continue
    return out


def _spec_picker(slugs: List[str]):
    """-> callable(row) -> the specification that row's own audience defines,
    falling back to the union for a row whose audience is missing or unknown
    (a hand-added line in owners.csv, or an audience since renamed)."""
    loaded = _audiences(slugs)
    by_slug = verify_mod.specs(loaded)
    fallback = verify_mod.target_sentence(loaded)
    return lambda row: verify_mod.spec_for(row, by_slug, fallback)


def _rejected_row(l: Dict[str, Any], r: Dict[str, Any]) -> Dict[str, Any]:
    return {"rejected_at": time.strftime("%Y-%m-%d"), "audience": l.get("audience") or "",
            "linkedin_account": l.get("linkedin_account") or "",
            "linkedin_url": l.get("linkedin_url") or "",
            "full_name": l.get("full_name") or "", "title": l.get("title") or "",
            "company": l.get("company") or "", "verified_role": r.get("role") or "",
            "verified_sector": r.get("sector") or "", "confidence": r.get("confidence") or "",
            "failed": ("" if not r.get("identified") else
                       "role %r is not owner-equivalent" % (r.get("role") or "")
                       if not r.get("role_qualifies") else "wrong sector"),
            "verify_source": r.get("source") or "", "reason": r.get("reason") or ""}


REJECT_FIELDS = ["rejected_at", "audience", "linkedin_account", "linkedin_url", "full_name",
                 "title", "company", "verified_role", "verified_sector", "failed",
                 "confidence", "verify_source", "reason"]


def _append_csv(path: Path, fields: List[str], rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="", extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def gate(candidates: List[Dict[str, Any]], take: int, target: Any, *, key: str,
         model: str, ttl: int, workers: int, budget: int,
         ask: Optional[Callable] = None,
         on_done: Optional[Callable] = None) -> tuple:
    """Walk the ranked candidates, verifying in parallel blocks, until `take`
    rows survive or the call budget runs out -> (kept, rejected, stats).

    Blocks rather than one-shot because most rows pass: verifying all of
    `candidates` to deliver 50 would pay for hundreds of answers nobody reads.
    A block may over-shoot slightly — those answers are cached, so the next
    export spends nothing on them. Budget counts UNCACHED calls only, since a
    cache hit is free.

    Running out of budget delivers fewer rows rather than delivering unchecked
    ones: an operator who asked for the gate asked for the gate."""
    kept: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    st = _verify_stats()
    st["ran"] = True
    st["target"] = "per audience" if callable(target) else target
    pending = list(candidates)
    while pending and len(kept) < take and st["calls"] < budget:
        room = min(max(1, workers), max(1, take - len(kept) + 2), budget - st["calls"] + workers)
        chunk, pending = pending[:room], pending[room:]
        res = verify_mod.verify_rows(chunk, target, key=key, model=model, ttl=ttl,
                                     workers=workers, ask=ask, on_done=on_done)
        for l in chunk:
            r = res.get((l.get("linkedin_account") or "").strip().lower()) or {}
            st["cost"] += r.get("cost") or 0.0
            if r.get("cached"):
                st["cached"] += 1
            else:
                st["calls"] += 1
            v = r.get("verdict") or verify_mod.UNVERIFIED
            st[v if v in ("confirmed", "rejected", "unverified") else "unverified"] += 1
            if v == verify_mod.REJECTED:
                rejected.append(_rejected_row(l, r))
                continue
            if len(kept) < take:
                kept.append(dict(l, _verify=r))
    return kept, rejected, st


def export(name: str, audiences: List[str], take: int, min_strength: str = "", *,
           verify: bool = False, verify_max: int = 0, verify_model: str = "",
           verify_ttl: int = -1, verify_workers: int = 8,
           ask: Optional[Callable] = None, on_verify: Optional[Callable] = None) -> Dict[str, Any]:
    d = RESULTS / name
    d.mkdir(parents=True, exist_ok=True)
    master = d / "owners.csv"
    _migrate_master(master)
    have: Dict[str, Dict[str, str]] = {}
    if master.exists():
        with master.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                have[row["linkedin_account"]] = row
    batch = 1 + max([int(r["batch"]) for r in have.values()] or [0])
    start = 1 + max([int(r["n"]) for r in have.values()] or [0])
    candidates = [l for l in pool(audiences) if l["linkedin_account"] not in have
                  and (not min_strength or l.get("strength") == min_strength)]

    st = _verify_stats()
    rejected: List[Dict[str, Any]] = []
    if verify:
        key = verify_mod.api_key()
        if not key and ask is None:
            raise SystemExit(
                "--verify needs OPEN_ROUTER_API_KEY (li-search/.env, project/.env or the "
                "environment). The gate is an explicit paid opt-in — roughly $%.3f per row "
                "checked — so it fails loudly rather than delivering unchecked rows."
                % verify_mod.MEASURED_COST_PER_ROW)
        target = _spec_picker(audiences)
        fresh, rejected, st = gate(
            candidates, take, target, key=key,
            model=verify_model or verify_mod.DEFAULT_MODEL,
            ttl=verify_mod.DEFAULT_TTL if verify_ttl < 0 else verify_ttl,
            workers=verify_workers, budget=verify_max or take * 3,
            ask=ask, on_done=on_verify)
    else:
        fresh = candidates[:take]

    now = time.strftime("%Y-%m-%d")
    rows: List[Dict[str, Any]] = []
    for k, l in enumerate(fresh):
        v = l.get("_verify") or {}
        rows.append({
            "n": start + k, "batch": batch, "delivered_at": now, "audience": l["audience"],
            "linkedin_account": l["linkedin_account"], "linkedin_url": l["linkedin_url"],
            "full_name": l.get("full_name") or "", "title": l.get("title") or "",
            "company": l.get("company") or "", "location": l.get("location") or "",
            "match": l.get("match") or "", "score": l.get("score") or 0,
            "sources": "+".join(l.get("sources") or []),
            "strength": l.get("strength") or "",
            "status": "",      # set to 'withdrawn' by hand if a row is later found off-spec
            # The gate's answer sits BESIDE the deterministic fields, never over
            # them: `title`/`company`/`match` remain exactly what the index
            # showed, so a row stays auditable against its own provenance.
            "verified": v.get("verdict") or "",
            "verified_role": v.get("role") or "",
            "verified_sector": v.get("sector") or "",
            "verify_source": v.get("source") or "",
        })
    if rows:
        new_file = not master.exists()
        with master.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, restval="", extrasaction="ignore")
            if new_file:
                w.writeheader()
            w.writerows(rows)
        with (d / ("batch-%02d.csv" % batch)).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, restval="", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        (d / "accounts.txt").write_text(
            "\n".join(list(have) + [r["linkedin_account"] for r in rows]) + "\n", encoding="utf-8")
    # Rejections are RECORDED, never silently dropped: the gate is a paid
    # judgement about a real person and the operator must be able to read it,
    # disagree with it, and re-deliver by hand.
    _append_csv(d / "rejected.csv", REJECT_FIELDS, rejected)
    readme = d / "README.md"
    if not readme.exists():
        readme.write_text(
            "# %s — delivered LinkedIn owner accounts\n\n"
            "- `owners.csv`   every account ever delivered for this brief, numbered, never repeated\n"
            "- `batch-NN.csv` each delivery as it was handed over\n"
            "- `accounts.txt` just the LinkedIn account names, one per line\n"
            "- `rejected.csv` rows the `--verify` gate refused, with the reason\n\n"
            "Rebuilt by: `./li-search export %s --audiences %s --take 50`\n"
            "Source runs: `runs/<date>-<audience>/` (provenance, all rows incl. unqualified).\n"
            % (name, name, ",".join(audiences)), encoding="utf-8")
    return {"dir": str(d), "batch": batch, "delivered": len(rows), "total": len(have) + len(rows),
            # Pre-gate: what is still undelivered in the pool. The gate does not
            # remove a rejected row from the pool, it declines to deliver it —
            # so a later export re-reads the same cached verdict for free.
            "remaining": max(0, len(candidates) - len(rows)),
            "rejected": rejected, "verify": st, "rows": rows}


def recheck(name: str, audiences: List[str], *, limit: int = 50, apply: bool = False,
            recheck_all: bool = False, batch: str = "", verify_model: str = "", verify_ttl: int = -1,
            verify_workers: int = 8, ask: Optional[Callable] = None,
            on_verify: Optional[Callable] = None) -> Dict[str, Any]:
    """Run the same gate over rows ALREADY in owners.csv.

    The gate landed after 278 accounts had been delivered, and the probe that
    justified it found an off-spec row inside the delivered set on its first
    four-row control sample (ENERGIA sarl — renewables, not an agency). Those
    rows feed the invite drip, so they are worth the same cents.

    Dry run by default: it prints what it would change and writes nothing.
    `apply=True` stamps the verification columns and sets `status=withdrawn` on
    a confident rejection — the column that already exists for exactly this
    ("set to 'withdrawn' by hand if a row is later found off-spec"), and which
    project/tools/scripts/import_lisearch.py already honours as a drop reason."""
    d = RESULTS / name
    master = d / "owners.csv"
    if not master.exists():
        raise SystemExit("no delivered file at %s — export a batch first" % master)
    _migrate_master(master)
    with master.open(encoding="utf-8", newline="") as fh:
        rdr = csv.DictReader(fh)
        fields = list(rdr.fieldnames or FIELDS)
        rows = list(rdr)

    def pending(r: Dict[str, Any]) -> bool:
        if (r.get("status") or "").strip().lower() == "withdrawn":
            return False
        # `batch` targets ONE delivery. Without it the walk starts at row 1, so
        # checking "the batch I just delivered" would silently spend on the
        # oldest rows in the file instead.
        if batch and (r.get("batch") or "").strip() != str(batch).strip():
            return False
        return recheck_all or not (r.get("verified") or "").strip()

    todo = [r for r in rows if pending(r)][:limit]
    key = verify_mod.api_key()
    if not key and ask is None:
        raise SystemExit("recheck needs OPEN_ROUTER_API_KEY — see `./li-search export --verify`.")
    target = _spec_picker(audiences)
    res = verify_mod.verify_rows(
        todo, target, key=key, model=verify_model or verify_mod.DEFAULT_MODEL,
        ttl=verify_mod.DEFAULT_TTL if verify_ttl < 0 else verify_ttl,
        workers=verify_workers, ask=ask, on_done=on_verify)

    st = _verify_stats()
    st["ran"] = True
    st["target"] = "per audience" if callable(target) else target
    findings: List[Dict[str, Any]] = []
    for r in todo:
        v = res.get((r.get("linkedin_account") or "").strip().lower()) or {}
        verdict = v.get("verdict") or verify_mod.UNVERIFIED
        st["cost"] += v.get("cost") or 0.0
        st["cached" if v.get("cached") else "calls"] += 1
        st[verdict if verdict in ("confirmed", "rejected", "unverified") else "unverified"] += 1
        r["verified"] = verdict
        r["verified_role"] = v.get("role") or ""
        r["verified_sector"] = v.get("sector") or ""
        r["verify_source"] = v.get("source") or ""
        if verdict == verify_mod.REJECTED:
            r["status"] = "withdrawn"
            findings.append(dict(_rejected_row(
                {**r, "audience": r.get("audience"), "summary": ""}, v), n=r.get("n")))
    if apply:
        tmp = master.with_suffix(".csv.tmp")
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, restval="", extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("" if v is None else v) for k, v in r.items() if k in fields})
        tmp.replace(master)
        _append_csv(d / "rejected.csv", REJECT_FIELDS, findings)
    return {"dir": str(d), "checked": len(todo), "pending": max(0, len([r for r in rows if pending(r)]) - len(todo)),
            "withdrawn": findings, "applied": apply, "verify": st, "total": len(rows)}
