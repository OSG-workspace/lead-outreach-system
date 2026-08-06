#!/usr/bin/env python3
"""Generate a per-run README.md (structure + scoring manifest) for ANY run.

One uniform manifest format for every run folder. Reads the run's control files,
detects its path (template / combined-custom / custom+WhatsApp; email/whatsapp),
and writes a README.md documenting:
  - every control file and what the chain reads from it,
  - the pipeline stages FOR THIS PATH: what each task reads, writes, and SCORES on,
  - the qualified-lead definition (what "a proper score" means for this run),
  - the artifacts produced.

Usage:
  python3 tools/scripts/gen_run_readme.py --run-dir runs/<slug>
  python3 tools/scripts/gen_run_readme.py --all            # every real run folder
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]   # .../lead-outreach-system/project
RUNS = PROJECT / "runs"


def head1(p: Path) -> str:
    try:
        return p.read_text().splitlines()[0].strip()
    except Exception:
        return ""


def detect(run: Path) -> dict:
    # Sourcing comes from the fixture's single sourcing.json contract. Since
    # 2026-08-05 the only method is deterministic map/places enumeration; the
    # agent-per-query "search" method and its source-agent definitions are gone.
    # Legacy run folders fall back to source_agent.txt ("overpass" == map).
    source_desc = "map enumeration (source_overpass.py)"
    sj = run / "sourcing.json"
    if sj.exists():
        try:
            cfg = json.loads(sj.read_text())
            if cfg.get("method") == "map":
                source_desc = f"map enumeration ({cfg.get('vertical', 'hotel')}, source_overpass.py)"
            else:
                source_desc = cfg.get("agent", "map enumeration")
        except Exception:
            pass
    else:
        legacy = head1(run / "source_agent.txt")
        if legacy == "overpass":
            source_desc = "map enumeration (source_overpass.py)"
        elif legacy:
            source_desc = legacy
    draft_mode = head1(run / "draft_mode.txt") or "template"
    vertical = head1(run / "vertical.txt")
    email, wa = True, False
    ch = run / "channels.json"
    if ch.exists():
        try:
            chans = json.loads(ch.read_text())
            email = "email" in chans
            wa = "whatsapp" in chans
        except Exception:
            pass
    countries = ""
    if (run / "countries.txt").exists():
        codes = [c.strip() for c in (run / "countries.txt").read_text().splitlines() if c.strip() and not c.startswith("#")]
        countries = ", ".join(codes) if codes else ""
    hotel_gate = False
    q = run / "qualify.json"
    if q.exists():
        try:
            hotel_gate = bool(json.loads(q.read_text()).get("require_hotel_size_volume"))
        except Exception:
            pass
    combined_custom = (draft_mode == "custom" and email and not wa)
    return dict(source_agent=source_desc, draft_mode=draft_mode, vertical=vertical,
                email=email, wa=wa, countries=countries, hotel_gate=hotel_gate,
                combined_custom=combined_custom)


def control_rows(run: Path, d: dict) -> str:
    def has(name): return "✅" if (run / name).exists() else "—"
    rows = [
        ("README.md", "this manifest (generated)", "—"),
        ("icp.yaml", "ICP intent + qualification doc", has("icp.yaml")),
        ("sourcing.json", "Stage-2 sourcing contract (method + params)", d["source_agent"]),
        ("countries.txt", "ISO-2 country filter (absent → GCC default)", d["countries"] or "—"),
        ("draft_mode.txt", "template | custom", d["draft_mode"]),
        ("channels.json", "channels that send", ("email " if d["email"] else "") + ("whatsapp" if d["wa"] else "") or "email (default)"),
        ("pitch.json", "fixed copy overlay (template mode)", has("pitch.json")),
        ("vertical.txt", "required for custom runs", d["vertical"] or has("vertical.txt")),
        ("qualify.json", "per-run qualify gate", ("hotel size/volume" if d["hotel_gate"] else has("qualify.json"))),
    ]
    out = ["| File | Sets | This run |", "|---|---|---|"]
    out += [f"| `{n}` | {desc} | {val} |" for n, desc, val in rows]
    return "\n".join(out)


def pipeline_rows(d: dict) -> str:
    R = [("| Stage | Runs | Reads | Writes | Scores / gates on |"),
         ("|---|---|---|---|---|")]
    R.append(f"| 2. Source | `{d['source_agent']}` | sourcing.json contract | `candidates-batch-*.txt` | exhaustive enumeration |")
    cf = d["countries"] or "GCC default"
    R.append(f"| 3. Merge+dedup | `merge_candidates.py` | candidate `ISO2`; `countries.txt`; sent-log | `candidates-all.txt` | keep if country ∈ {{{cf}}} AND not already contacted |")
    R.append("| 4. Fetch | `fetch_html.sh` (6 pages) | candidate domains | `raw_html/` | — |")
    hv = ", **hotel_volume**" if d["hotel_gate"] else ""
    R.append(f"| 5. Extract | `extract_leads.py` | page HTML → emails, signal{hv} | `leads-extracted.json` | `score` (person 90 / role 82 / personal 78 + bonuses) |")
    hgate = " **DROP hotel `hotel_volume<medium`**;" if d["hotel_gate"] else ""
    hrank = "`hotel_volume` ↓, then " if d["hotel_gate"] else ""
    R.append(f"| 5.3 Qualify+cap | `qualify_leads.py` | `email_class`,`signal`,`score`,`vertical`{hv} | `leads-qualified.json` | DROP `personal`/`score<82`/`modern_booking`;{hgate} RANK {hrank}`score` ↓, person>role; CAP top-80 |")

    if d["combined_custom"]:
        R.append("| 5.5+6 Find+Write | `lead-writer` (1 per qualified lead) + `draft_lead_custom.py` | scraped pages, harvested emails | `emails-drafted.json` | KEEP only if at least one confirmed name component (Mr./Mrs.+surname, or first name alone) AND **non-generic direct email** AND a concrete per-business gap; salutation/no-money/link/no-dash contract |")
    else:
        R.append("| 5.5 Enrich | `enrich_contact_person.py` + `name-finder` (1 per qualified lead) | business, country, site emails/pages | `leads-with-contact.json` | KEEP only if real Mr./Mrs.+surname AND **non-generic direct email** |")
        if d["email"] and d["draft_mode"] == "custom":
            R.append("| 6. Draft (email) | `draft_custom.py` + `gap-writer` (1 per lead) | scraped pages; `voice-us.md` | `emails-drafted.json` | per-business gap; salutation/no-money/link/no-dash contract |")
        elif d["email"]:
            R.append("| 6. Draft (email) | `draft_emails.py` + `pitch.json` | `contact_title`+`contact_last_name`→`{salutation}`, `name`,`score` | `emails-drafted.json` | role-class ≥85 send gate; salutation contract |")

    if d["email"]:
        R.append("| 7. Send (email) | `send_batch_brevo.py` + `email_template.py` | `to_email`,`subject`,`body_text` | `emails-sent.jsonl` | renders professional HTML; cap `MAX_EMAILS_PER_RUN` |")
    if d["wa"]:
        wa_drafter = "`draft_whatsapp_custom.py` + `wa-writer`" if d["draft_mode"] == "custom" else "`draft_whatsapp.py`"
        R.append(f"| 8.5 WhatsApp | {wa_drafter} → `bridge/send_campaign.js` | `leads-with-contact.json` (needs mobile) | `whatsapp-sent.jsonl` | valid mobile; per-run contract |")
    R.append("| 8. Persist | `persist_sent_log.py` | sent rows (`result=sent`) | appends `vault/lead-outreach/sent-log.md` | — |")
    return "\n".join(R)


def qualified_def(d: dict) -> str:
    bullets = []
    if d["hotel_gate"]:
        bullets.append("1. **Big / high-volume property** — `hotel_volume ∈ {high, medium}` "
                       "(room count, 4-5 star, resort/spa/conference scale, derived at Stage 5). "
                       "Small/low-volume → dropped at Stage 5.3.")
    bullets.append(f"{len(bullets)+1}. **Score ≥ 82** and **not freemail-only** (`email_class ≠ personal`).")
    bullets.append(f"{len(bullets)+1}. **Non-generic decision-maker email** resolved at Stage 5.5 "
                   "(`info@`/`reservations@`/role mailboxes dropped, never used).")
    return "\n".join(bullets)


def render(run: Path, d: dict) -> str:
    chan = "email" + (" + whatsapp" if d["wa"] else "") if d["email"] else ("whatsapp" if d["wa"] else "none")
    path = ("combined-custom email (lead-writer)" if d["combined_custom"]
            else f"{d['draft_mode']} {chan}")
    vline = f" · vertical **{d['vertical']}**" if d["vertical"] else ""
    return f"""# Run: {run.name} — file structure & scoring manifest

Path: **{path}** · source `{d['source_agent']}`{vline} · countries {d['countries'] or 'GCC default'}.

_Generated by `tools/scripts/gen_run_readme.py` — regenerate after changing control files._

## A. Control files (inputs)

{control_rows(run, d)}

## B. Pipeline — each task: reads → writes → SCORES on

{pipeline_rows(d)}

## C. Qualified-lead definition (what "a proper score" means here)

A lead reaches outreach only if all hold:

{qualified_def(d)}

Ranking within the top-80 cap puts the best-fit leads first, so they are
enriched and contacted before weaker ones.

## D. Artifacts (regenerated each /fire; safe to delete)

```
candidates-batch-*.txt  candidates-all.txt  raw_html/
leads-extracted.json    leads-qualified.json
{'lead-batch-*.txt  lead-out-*.json' if d['combined_custom'] else 'enrich-batch-*.txt  enrich-out-*.json  leads-with-contact.json'}
emails-drafted.json     emails-sent.jsonl   send-log.txt
```

Fire with: `/fire {run.name}`
"""


def is_run(run: Path) -> bool:
    return run.is_dir() and ((run / "icp.yaml").exists()
                             or (run / "sourcing.json").exists()
                             or (run / "source_agent.txt").exists())


def write_for(run: Path) -> None:
    d = detect(run)
    (run / "README.md").write_text(render(run, d))
    print(f"  wrote {run.name}/README.md  [{('combined-custom' if d['combined_custom'] else d['draft_mode'])}, "
          f"{'email' if d['email'] else ''}{'+wa' if d['wa'] else ''}{', hotel-gate' if d['hotel_gate'] else ''}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.all:
        runs = sorted(p for p in RUNS.iterdir() if is_run(p))
        print(f"Generating README for {len(runs)} run folders:")
        for r in runs:
            write_for(r)
    elif a.run_dir:
        write_for(Path(a.run_dir).resolve())
    else:
        raise SystemExit("Pass --run-dir <path> or --all")


if __name__ == "__main__":
    main()
