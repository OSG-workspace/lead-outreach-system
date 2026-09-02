#!/usr/bin/env python3
"""Scaffold a NEW campaign type as one small branch of the template tree.

ONE command creates a complete, structurally-valid fixture under
templates/<base>/ — the same five-file contract every campaign has
(campaign.json + icp.yaml + pitch.json + places.txt [+ qualify.json], see
campaign_config.py), so the ONE chain (run_fire.py / fire.md) runs it with
zero code changes:

  python3 tools/scripts/new_campaign.py \\
      --base de-dental --vertical dental --countries DE,AT,CH \\
      --selector '"amenity"="dentist"'

What it does NOT do (on purpose):
  - It never invents email copy. Template-mode fixtures are created WITHOUT
    pitch.json, and fire_campaign.sh refuses to fire until the user's
    approved copy is saved there (template-first contract, templates/README.md).
  - It never invents place rows. places.txt is created with instructions only
    where the built-in OSM city table does not cover the target countries, and
    the fire aborts while it holds nothing but comments.

Sourcing is deterministic OSM/Places enumeration for EVERY campaign; the old
agent-per-query "search" method was retired 2026-08-05 along with its
source-agent definitions, so there is no --method switch any more.
The scaffold therefore always fails LOUDLY (with the exact missing piece) if
fired half-finished — never silently and never differently per campaign.
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT / "templates"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import campaign_config as cc                       # noqa: E402
from email_utils import COUNTRY_NAMES_ISO          # noqa: E402
from source_overpass import CITIES as OSM_CITIES   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="fixture folder name, kebab-case (e.g. us-hvac)")
    ap.add_argument("--vertical", required=True, help="vertical string used in candidate lines / writer prompts")
    ap.add_argument("--countries", required=True, help='comma-separated ISO-2 codes, or "*" for worldwide')
    ap.add_argument("--selector", required=True, help='OSM tag filter, e.g. \'"amenity"="dentist"\'')
    ap.add_argument("--channels", default="email", choices=["email", "whatsapp", "both"])
    ap.add_argument("--draft-mode", default="template", choices=["template", "custom"])
    a = ap.parse_args()

    # --- validate inputs up-front, loudly -------------------------------
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", a.base):
        sys.exit(f"ABORT: --base must be kebab-case ([a-z0-9-]), got: {a.base!r}")
    tpl = TEMPLATES / a.base
    if tpl.exists():
        sys.exit(f"ABORT: {tpl} already exists — edit that fixture, or pick a new base name.")
    countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
    if not countries:
        sys.exit("ABORT: --countries is empty.")
    if countries != ["*"]:
        unknown = [c for c in countries if c != "*" and c not in COUNTRY_NAMES_ISO]
        if unknown:
            print(f"NOTE: country code(s) {unknown} not in the built-in name map — "
                  f"{{country}} will render as the code unless pitch.json adds country_names.")
    if not re.fullmatch(r'"[\w:]+"[=~]"[^"]+"', a.selector):
        sys.exit(f'ABORT: --selector must look like "key"="value" or "key"~"regex", got: {a.selector!r}')

    # --- write the fixture (THE general structure) -----------------------
    tpl.mkdir(parents=True)
    wrote: list[str] = []

    def w(name: str, content: str) -> None:
        (tpl / name).write_text(content)
        wrote.append(name)

    chans = {"email": ["email"], "whatsapp": ["whatsapp"], "both": ["email", "whatsapp"]}[a.channels]
    cfg = cc.defaults()
    cfg.update({
        "channels": chans,
        "draft_mode": a.draft_mode,
        "vertical": a.vertical if a.draft_mode == "custom" else None,
        "countries": countries,
        # `selector`+`vertical` alone is the single-map-source shorthand; add a
        # "sources" ladder (gmaps / overture / places) once the vertical's
        # presets exist — see templates/README.md.
        "sourcing": {"selector": a.selector, "vertical": a.vertical, "max_per_run": 350},
        "notes": [f"scaffolded by new_campaign.py --base {a.base} --vertical {a.vertical} "
                  f"--countries {a.countries} --selector {a.selector}"],
    })
    w(cc.CAMPAIGN_FILE, cc.dumps_pretty(cc.ordered(cfg)))
    covered = {v[0] for v in OSM_CITIES.values()}
    missing = [c for c in countries if c != "*" and c not in covered]
    if missing:
        w("places.txt",
          f"# REQUIRED: the built-in place table covers EU/adjacent only, and this\n"
          f"# campaign targets {','.join(missing)}. One place per line:\n"
          f"# City Name|ISO2|lat|lon|half_width_degrees   (0.03 small city, 0.07 metro)\n"
          f"# The fire ABORTS while this file has no real places.\n")

    where = "worldwide" if countries == ["*"] else ", ".join(
        COUNTRY_NAMES_ISO.get(c, c) for c in countries)
    w("icp.yaml",
      f"# {a.base} — campaign ICP. Fill the TODOs; the chain reads structure from\n"
      f"# campaign.json, this file is the human-readable intent.\n"
      f"name: {a.base}\n"
      f"channel: {'+'.join(chans)}\n"
      f"draft_mode: {a.draft_mode}\n"
      f"countries: [{', '.join(countries)}]\n"
      f"\n"
      f"target:\n"
      f"  vertical: {a.vertical}\n"
      f"  regions: [{where}]\n"
      f"  must_be:\n"
      f"    - TODO: concrete criterion 1 (evidence a fit shows on its own site)\n"
      f"    - TODO: concrete criterion 2\n"
      f"  exclude:\n"
      f"    - TODO: who looks similar but is NOT a fit\n"
      f"\n"
      f"pitch: TODO one-line description of the offer (full copy lives in pitch.json)\n")

    # --- next steps: exactly what still blocks a fire --------------------
    # The SAME validator fire_campaign.sh runs, so this list and the fire's
    # ABORT lines can never disagree.
    print(f"Created templates/{a.base}/ with: {', '.join(sorted(wrote))}\n")
    todo = list(cc.validate(cc.load(tpl), tpl))
    if any("pitch.json" in t for t in todo):
        todo.append("pitch.json needs subject_template + body_template; slots {salutation} "
                    "{name} [{opener} {vertical} {country}] — per-business variation only "
                    "through slots, never by rewriting.")
    todo.append("icp.yaml — replace the TODO criteria.")
    print("Blocks fire until done:" if todo else "Fire-ready.")
    for t in todo:
        print(f"  - {t}")
    print(f"\nThen: bash tools/scripts/fire_campaign.sh {a.base} --plan   (trace)"
          f"\n      bash tools/scripts/fire_campaign.sh {a.base}          (fire)")


if __name__ == "__main__":
    main()
