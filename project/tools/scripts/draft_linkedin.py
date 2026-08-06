#!/usr/bin/env python3
"""Stage 8.6: the LinkedIn arm of a fire run (bridge to project/linkedin).

A LinkedIn run is the SAME chain as every other run — same sourcing, same
merge/dedup/qualify — and diverges only at the outreach step: instead of an
email to a company mailbox, it sends a DM to a person at that company.

    phase prep    leads-qualified.json  -> li-companies.json   (walker input)
    phase batch   people-qualified.json -> li-batch-NNN.txt    (one per person,
                                                                for li-writer)
    phase merge   li-out/*.json         -> people-with-notes.json

Between `prep` and `batch`, run_fire calls two things this script does not own:
  linkedin/scripts/walk_companies.js   company -> people      (browser)
  tools/scripts/qualify_people.py      the four person gates

WHY THE MESSAGE IS WRITTEN PER PERSON, NEVER RENDERED FROM A TEMPLATE
The fixed-copy pattern that works for email dies here. A templated email from a
scrape is tolerated; a templated DM from someone who just connected is read as
automation instantly, because the medium implies a human wrote it. So there is
no `body_template` path in this file at all — pitch.json supplies the ANGLE
(what we sell, the proof link, the CTA) and li-writer composes each message
around that person's harvested hooks. `linkedin/queue/generate-dm.js`
independently rejects the batch if two messages come out near-identical.
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from li_url import canonical_profile_url  # noqa: E402


def prep(run: Path) -> int:
    """Qualified company leads -> the walker's input list."""
    src = run / "leads-qualified.json"
    if not src.exists():
        sys.exit("ABORT: leads-qualified.json missing")
    leads = json.loads(src.read_text())
    out = []
    for l in leads:
        name = l.get("name") or l.get("business") or l.get("company")
        if not name:
            continue
        out.append({
            "name": name,
            "domain": l.get("domain"),
            # Geo anchor comes from OUR sourcing, not from LinkedIn's header.
            "country": l.get("country_code") or l.get("country"),
            "city": l.get("city"),
            "vertical": l.get("vertical"),
            "headcount": l.get("headcount"),
        })
    (run / "li-companies.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"Stage 8.6 prep: {len(out)} companies -> li-companies.json")
    return len(out)


def batch(run: Path, pitch: dict) -> int:
    """One li-batch file per qualified person: everything the writer needs."""
    src = run / "people-qualified.json"
    if not src.exists():
        sys.exit("ABORT: people-qualified.json missing (run qualify_people.py first)")
    people = json.loads(src.read_text())
    outdir = run / "li-out"
    outdir.mkdir(exist_ok=True)
    for f in run.glob("li-batch-*.txt"):
        f.unlink()

    angle = pitch.get("linkedin_angle") or pitch.get("_comment") or ""
    proof = pitch.get("proof_link") or ""
    cta = pitch.get("linkedin_cta") or "Ask for a short call."
    for i, p in enumerate(people, 1):
        hooks = []
        if p.get("school"):
            hooks.append(f"shared school: {p['school']}")
        if p.get("mutual_connections"):
            hooks.append(f"{p['mutual_connections']} mutual connections")
        for post in (p.get("recent_posts") or [])[:2]:
            hooks.append(f"recent post: {post}")
        if p.get("company_signal"):
            hooks.append(f"company signal: {p['company_signal']}")
        payload = (
            f"OutputFile: {(outdir / f'{i:03d}.json').resolve()}\n"
            f"Person: {p.get('full_name')}\n"
            f"Title: {p.get('title')}\n"
            f"Company: {p.get('company')}\n"
            f"Country: {p.get('company_country')}\n"
            f"ProfileUrl: {p.get('profile_url')}\n"
            f"Hooks:\n" + ("\n".join(f"  - {h}" for h in hooks) if hooks else "  - (none found)") + "\n"
            f"\nAngle (what we sell — do NOT paste this verbatim):\n{angle}\n"
            f"ProofLink: {proof}\n"
            f"CTA: {cta}\n"
        )
        (run / f"li-batch-{i:03d}.txt").write_text(payload)
    print(f"Stage 8.6 batch: {len(people)} li-batch files")
    return len(people)


def merge(run: Path) -> int:
    """Collect li-writer output back onto the person records.

    MATCHED ON THE CANONICAL URL, NEVER THE RAW STRING. The writer is a language
    model echoing back a URL it was handed, and the two person-routes spell the
    same profile differently to begin with (trailing slash, `ae.` subdomain,
    case). An exact match therefore dropped real messages silently, and the lead
    went on to be rejected downstream as "no follow-up DM composed" — a written
    message discarded and reported as a gate. `NNN.json` also maps 1:1 onto
    `people[NNN-1]` by construction in batch(), so the file index is a reliable
    second key when the echoed URL is unusable.
    """
    src = run / "people-qualified.json"
    people = json.loads(src.read_text()) if src.exists() else []
    outdir = run / "li-out"
    notes: dict[str, str] = {}
    by_index: dict[int, str] = {}
    for f in sorted(outdir.glob("*.json")) if outdir.exists() else []:
        try:
            d = json.loads(f.read_text())
        except Exception:
            print(f"  WARN: {f.name} is not valid JSON — skipped")
            continue
        msg = (d.get("message") or "").strip()
        if not msg:
            continue
        url = canonical_profile_url(d.get("profile_url") or d.get("ProfileUrl"))
        if url:
            notes[url] = msg
        if f.stem.isdigit():
            by_index[int(f.stem)] = msg

    merged, missing, by_idx_used = [], [], 0
    for i, p in enumerate(people, 1):
        m = notes.get(canonical_profile_url(p.get("profile_url")) or "")
        if not m and i in by_index:
            m = by_index[i]
            by_idx_used += 1
        if m:
            merged.append({**p, "message": m})
        else:
            missing.append(p.get("full_name") or p.get("profile_url") or "?")

    (run / "people-with-notes.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    print(f"Stage 8.6 merge: {len(merged)} messages written, {len(missing)} people had none")
    if by_idx_used:
        print(f"  ({by_idx_used} matched by batch index — li-writer echoed an unusable profile_url)")
    if missing:
        print(f"  no message for: {', '.join(missing[:8])}"
              + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
    return len(merged)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["prep", "batch", "merge"])
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    run = Path(a.run_dir)
    pitch = {}
    pf = run / "pitch.json"
    if pf.exists():
        try:
            pitch = json.loads(pf.read_text())
        except Exception:
            pass
    n = {"prep": lambda: prep(run),
         "batch": lambda: batch(run, pitch),
         "merge": lambda: merge(run)}[a.phase]()
    if a.phase == "merge" and n == 0:
        sys.exit(7)      # nothing composed — run_fire decides if that is terminal


if __name__ == "__main__":
    main()
