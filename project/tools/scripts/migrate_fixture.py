#!/usr/bin/env python3
"""Convert a legacy per-file fixture into the campaign.json contract.

    python3 tools/scripts/migrate_fixture.py gcc-receptionist lb-enterprise
    python3 tools/scripts/migrate_fixture.py --all [--dry-run]

Per fixture:
  1. load() the knob files exactly as fire_campaign.sh always read them
  2. move every `_note`/`_comment` string in sourcing.json (any depth, in
     order) and linkedin.json into `notes`; the three paragraphs that were
     copy-pasted into every fixture (see GENERIC_NOTES) are dropped -- they
     live once in templates/README.md now
  3. write campaign.json (indent 2, SCHEMA key order)
  4. REFUSE unless materialize() of that campaign.json reproduces the original
     legacy files byte-for-byte -- JSON files compared after stripping notes
     and re-serialising in the fixture's own format, fetch_pages.txt after
     dropping its comment lines, and files that held the default
     (draft_mode.txt = template, enrich_cap.txt = 400, channels.json = ["email"],
     li_max_per_company.txt = 3) are allowed to disappear
  5. delete the legacy knob files (content files stay)

A refusal leaves the fixture exactly as it was.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import campaign_config as cc  # noqa: E402

PROJECT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT / "templates"
NOT_FIXTURES = {"li-handoff"}

LEGACY_KNOB_FILES = [spec["file"] for spec in cc.SCHEMA.values() if spec["file"]]
LEGACY_EXTRA = ["overpass_places.txt"]      # source_overpass.py fallback, only read when places.txt is absent

# Prose that was identical in 15-19 fixtures. It is general documentation of
# the source ladder, not a fact about any one campaign, so it moves to
# templates/README.md ("Why the ladder is ordered this way") once.
GENERIC_NOTES = (
    "Google Maps enumerated directly via the gosom scraper binary (free, already installed by "
    "tools/install.sh). FIRST because it is the highest-yield source measured on this stack: one "
    "Wollongong sweep over 5 trade terms returned 124 candidates ALREADY carrying a domain in ~2 "
    "minutes, against 111 for the whole 2026-08-17-au-trades-1 run from every other source plus 73 "
    "minutes of Stage 2.5. Terms and the category denylist come from tools/scripts/gmaps_presets.json "
    "via each target's vertical, so precision is shared by every campaign rather than restated per "
    "fixture. TRADEOFF, on purpose: scraping Maps is contrary to Google's ToS, unlike the paid places "
    "source. See source_gmaps.py.",
    "Overture Maps bulk POI (free, CDLA/Apache, no request ceiling). Added 2026-08-12. Categories come "
    "from tools/scripts/overture_presets.json via the target's vertical, so precision is shared by "
    "every campaign rather than re-derived per fixture. Listed FIRST because it is the highest-yield "
    "free source: ~half its rows already carry the business's own domain, so they enter the chain as "
    "candidates without Stage 2.5. See templates/README.md 'Overture sources'.",
    "map = this campaign's original OSM config, unchanged. places = added 2026-08-10 so a swept OSM "
    "ledger no longer means a zero-candidate fire. See templates/README.md 'Places sources' for the "
    "Table A / budget / ledger-scoping rules that govern every fixture's places block.",
)


def _degeneric(note: str) -> tuple[str | None, bool]:
    """Drop a generic paragraph, whole or as a ' — ' suffix. -> (kept text, dropped?)"""
    body = note.split(": ", 1)[1] if note.startswith(("sources[", "linkedin.")) and ": " in note else note
    prefix = note[: len(note) - len(body)]
    for g in GENERIC_NOTES:
        if body == g:
            return None, True
        for sep in (" — ", " - ", " "):
            if body.endswith(sep + g):
                return prefix + body[: -len(sep + g)].rstrip(), True
    return note, False


def expected_bytes(fixture: Path, fname: str) -> bytes | None:
    """What materialize() must produce for a legacy file, or None = may vanish."""
    p = fixture / fname
    raw = p.read_bytes()
    if fname in ("sourcing.json", "linkedin.json"):
        return cc.dumps_pretty(cc.strip_notes(json.loads(raw))).encode()
    if fname == "channels.json":
        return (json.dumps(json.loads(raw)) + "\n").encode()
    if fname == "fetch_pages.txt":
        rows = [ln.strip() for ln in raw.decode().splitlines() if ln.strip() and not ln.strip().startswith("#")]
        return ("\n".join(rows) + "\n").encode()
    return raw


def verify(fixture: Path, cfg: dict) -> list[str]:
    """Materialise cfg into a temp dir and diff it against the legacy files."""
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="migrate-") as td:
        run = Path(td)
        cc.materialize(cfg, run, fixture)
        for fname in LEGACY_KNOB_FILES:
            legacy = fixture / fname
            made = run / fname
            if legacy.exists():
                want = expected_bytes(fixture, fname)
                if made.exists():
                    if made.read_bytes() != want:
                        problems.append(f"{fname}: materialised bytes differ from the legacy file")
                else:
                    key = next(k for k, s in cc.SCHEMA.items() if s["file"] == fname)
                    if cc.render(key, cfg.get(key)) is not None:
                        problems.append(f"{fname}: legacy file exists but nothing was materialised")
                    # else: it held the default -- allowed to drop out
            elif made.exists():
                problems.append(f"{fname}: materialised but the fixture never had it")
        for fname in cc.CONTENT_FILES:
            src = fixture / fname
            if src.exists() and (not (run / fname).exists() or (run / fname).read_bytes() != src.read_bytes()):
                problems.append(f"{fname}: content file not copied verbatim")
    return problems


def migrate(fixture: Path, dry_run: bool) -> bool:
    name = fixture.name
    if (fixture / cc.CAMPAIGN_FILE).exists():
        print(f"== {name}: already has {cc.CAMPAIGN_FILE}, skipped")
        return True
    cfg = cc.load_legacy(fixture)
    errs = cc.validate(cfg, fixture)
    if errs:
        print(f"== {name}: REFUSED, the legacy fixture does not validate:")
        for e in errs:
            print(f"     - {e}")
        return False

    kept: list[str] = []
    dropped = 0
    for n in cfg["notes"]:
        text, was_generic = _degeneric(n)
        dropped += was_generic
        if text:
            kept.append(text)
    cfg["notes"] = kept

    problems = verify(fixture, cfg)
    if problems:
        print(f"== {name}: REFUSED, materialize() does not reproduce the legacy files:")
        for p in problems:
            print(f"     - {p}")
        return False

    to_delete = [f for f in LEGACY_KNOB_FILES if (fixture / f).exists()]
    extra_msgs: list[str] = []
    for f in LEGACY_EXTRA:
        p = fixture / f
        if p.exists():
            places = fixture / "places.txt"
            if places.exists():
                same = p.read_bytes() == places.read_bytes()
                extra_msgs.append(f"{f}: deleted ({'identical to' if same else 'DIFFERED from'} places.txt; "
                                  f"source_overpass.py only reads it when places.txt is absent)")
                to_delete.append(f)
            else:
                extra_msgs.append(f"{f}: kept -- no places.txt to supersede it")
    defaults_dropped = [f for f in to_delete
                        if f in LEGACY_KNOB_FILES
                        and cc.render(next(k for k, s in cc.SCHEMA.items() if s["file"] == f),
                                      cfg.get(next(k for k, s in cc.SCHEMA.items() if s["file"] == f))) is None]

    print(f"== {name}{' (dry run)' if dry_run else ''}")
    print(f"   + {cc.CAMPAIGN_FILE}  ({len(kept)} note(s) kept, {dropped} generic paragraph(s) dropped)")
    print(f"   - {', '.join(to_delete)}")
    if defaults_dropped:
        print(f"   default-valued, no longer materialised: {', '.join(defaults_dropped)}")
    for m in extra_msgs:
        print(f"   {m}")
    if dry_run:
        return True

    (fixture / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    # Re-verify from the file on disk (load() path), then delete.
    problems = verify(fixture, cc.load(fixture))
    if problems:
        (fixture / cc.CAMPAIGN_FILE).unlink()
        print(f"   REFUSED after writing: {problems} -- {cc.CAMPAIGN_FILE} removed again")
        return False
    for f in to_delete:
        (fixture / f).unlink()
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bases", nargs="*", help="fixture base names under templates/")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    a = ap.parse_args()
    if a.all:
        bases = sorted(d.name for d in TEMPLATES.iterdir() if d.is_dir() and d.name not in NOT_FIXTURES)
    else:
        bases = a.bases
    if not bases:
        ap.error("name fixtures or pass --all")
    ok = True
    for b in bases:
        fx = TEMPLATES / b
        if not fx.is_dir():
            print(f"== {b}: no such fixture")
            ok = False
            continue
        ok &= migrate(fx, a.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
