#!/usr/bin/env python3
"""THE per-campaign fixture contract: one `campaign.json` per `templates/<base>/`.

A fixture is five files:

    campaign.json   every knob (this module's SCHEMA)   -- REQUIRED
    icp.yaml        the human-readable target            -- REQUIRED
    pitch.json      the user-approved copy               -- template mode + email/linkedin
    places.txt      City|ISO2|lat|lon|half_width rows    -- any gmaps/overture/places source
    qualify.json    optional per-campaign qualify gate

The stage scripts never read campaign.json. They keep reading the per-file
layout they always read (`sourcing.json`, `channels.json`, `draft_mode.txt`,
`countries.txt`, ...) from the RUN folder, and `fire_campaign.sh` calls
`materialize()` here to write that layout into the freshly cloned
`runs/<slug>/` from campaign.json. So every stage is untouched, every run folder
stays self-documenting, and a knob has exactly one home in the fixture.

A fixture WITHOUT campaign.json but with the legacy knob files still loads
(`load()` reads them) -- that is also how `migrate_fixture.py` converts one.

Stdlib only: fire_campaign.sh runs this before the venv matters.

CLI:
    campaign_config.py --fixture templates/<base> --validate
    campaign_config.py --fixture templates/<base> --materialize runs/<slug>
    campaign_config.py --fixture templates/<base> --show
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

CAMPAIGN_FILE = "campaign.json"

# Content files: copied verbatim into the run folder, never re-serialised.
CONTENT_FILES = ("icp.yaml", "pitch.json", "places.txt", "qualify.json")

# What run_fire.py / the stages fall back to when the materialised file is
# absent. A knob equal to its chain default is NOT materialised (the run
# behaves identically) and a legacy file holding the chain default loads as
# the schema default, so `enrich_cap.txt` = 400 simply drops out.
CHAIN_DEFAULTS = {
    "enrich_cap": 400,           # qualify_leads.py --cap / ENRICH_MAX_LEADS
    "li_max_per_company": 3,     # run_fire.py Stage 8.6 walk
    "li_max_people": 60,         # run_fire.py Stage 8.6 walk + 8.6b
}

# key -> (type, default, materialised file, reader). Order = campaign.json order.
SCHEMA: dict[str, dict[str, Any]] = {
    "channels":           {"type": "list[str]",  "default": ["email"],   "file": "channels.json",
                           "reader": "run_fire.py (channel arms)"},
    "draft_mode":         {"type": "template|custom", "default": "template", "file": "draft_mode.txt",
                           "reader": "run_fire.py (Stage 6 drafter choice)"},
    "vertical":           {"type": "str|null",   "default": None,        "file": "vertical.txt",
                           "reader": "fire validation only (custom mode needs an explicit vertical)"},
    "countries":          {"type": "list[ISO2]", "default": [],          "file": "countries.txt",
                           "reader": "merge_candidates.py country filter; source_overpass.py built-in table"},
    "enrich_cap":         {"type": "int|null",   "default": None,        "file": "enrich_cap.txt",
                           "reader": "run_fire.py -> ENRICH_MAX_LEADS for qualify_leads.py"},
    "target_roles":       {"type": "list[str]",  "default": [],          "file": "target_roles.txt",
                           "reader": "enrich_contact_person.py (TargetRoles: line per name-finder batch)"},
    "fetch_pages":        {"type": "list[str]",  "default": [],          "file": "fetch_pages.txt",
                           "reader": "fetch_html.py load_pages() (slug|path per line)"},
    "linkedin":           {"type": "object|null", "default": None,       "file": "linkedin.json",
                           "reader": "run_fire.py -> qualify_people.py --config; li_handoff.py"},
    "li_max_per_company": {"type": "int|null",   "default": None,        "file": "li_max_per_company.txt",
                           "reader": "run_fire.py Stage 8.6 walk_companies --max-per-company"},
    "li_max_people":      {"type": "int|null",   "default": None,        "file": "li_max_people.txt",
                           "reader": "run_fire.py Stage 8.6/8.6b --max-people"},
    "sourcing":           {"type": "object",     "default": None,        "file": "sourcing.json",
                           "reader": "run_fire.py resolve_sourcing() -> every source_*.py, resolve_domains.py"},
    "notes":              {"type": "list[str]",  "default": [],          "file": None,
                           "reader": "nobody -- free prose, never materialised"},
}

KNOWN_CHANNELS = ("email", "whatsapp", "whatsapp-fallback", "linkedin", "linkedin-email-lookup")
KNOWN_SOURCE_TYPES = ("gmaps", "overture", "map", "places", "directory")
# Sources whose script sys.exit()s without a places.txt in the run folder.
PLACES_BOUND_SOURCES = ("gmaps", "overture", "places")
ISO2_RE = re.compile(r"^[A-Z]{2}$")
PLACE_ROW_RE = re.compile(r"^[^#].*\|.*\|.*\|.*\|")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def strip_notes(obj: Any) -> Any:
    """Return a deep copy of `obj` without any `_`-prefixed key, at any depth."""
    if isinstance(obj, dict):
        return {k: strip_notes(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [strip_notes(v) for v in obj]
    return obj


def collect_notes(obj: Any, path: str = "") -> list[tuple[str, str]]:
    """Every `_`-prefixed string value in document order, as (locator, text)."""
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).startswith("_") and isinstance(v, str):
                out.append((here, v))
            else:
                out.extend(collect_notes(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(collect_notes(v, f"{path}[{i}]"))
    return out


def _read_text(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _lines(p: Path) -> list[str]:
    """Non-empty, non-comment lines, stripped -- the way every reader parses them."""
    return [ln.strip() for ln in _read_text(p).splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _int_or_none(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _norm_chain_default(key: str, value: Any) -> Any:
    if key in CHAIN_DEFAULTS and value == CHAIN_DEFAULTS[key]:
        return None
    return value


def defaults() -> dict[str, Any]:
    return {k: json.loads(json.dumps(v["default"])) for k, v in SCHEMA.items()}


# --------------------------------------------------------------------------
# byte formats -- exactly what the stage scripts read today
# --------------------------------------------------------------------------
def dumps_pretty(obj: Any) -> str:
    """sourcing.json / linkedin.json / campaign.json: indent 2, unicode kept."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def render(key: str, value: Any) -> str | None:
    """The bytes of the materialised file for `key`, or None when the value is
    at its default and the file must not exist."""
    default = SCHEMA[key]["default"]
    if key == "notes" or value is None or value == default:
        return None
    if key == "channels":
        return json.dumps(value) + "\n"
    if key in ("draft_mode", "vertical"):
        return f"{value}\n"
    if key == "countries":
        return "\n".join(value) + "\n"
    if key in ("enrich_cap", "li_max_per_company", "li_max_people"):
        if value == CHAIN_DEFAULTS.get(key):
            return None
        return f"{value}\n"
    if key == "target_roles":
        return ", ".join(value) + "\n"
    if key == "fetch_pages":
        return "\n".join(value) + "\n"
    if key in ("linkedin", "sourcing"):
        return dumps_pretty(strip_notes(value))
    raise KeyError(key)


# --------------------------------------------------------------------------
# load
# --------------------------------------------------------------------------
def load_legacy(fixture_dir: Path) -> dict[str, Any]:
    """The same dict `campaign.json` would give, read from the per-file layout."""
    fx = Path(fixture_dir)
    cfg = defaults()
    notes: list[str] = []

    p = fx / "channels.json"
    if p.exists():
        try:
            chans = json.loads(p.read_text())
            if isinstance(chans, list) and chans:
                cfg["channels"] = [str(c) for c in chans]
        except Exception:
            cfg["channels"] = "<invalid channels.json>"      # validate() reports it
    p = fx / "draft_mode.txt"
    if p.exists():
        first = (p.read_text().splitlines() or [""])[0].replace(" ", "").strip()
        cfg["draft_mode"] = first or "template"
    p = fx / "vertical.txt"
    if p.exists() and p.read_text().strip():
        cfg["vertical"] = p.read_text().strip()
    cfg["countries"] = [c.upper() for c in _lines(fx / "countries.txt")]
    p = fx / "enrich_cap.txt"
    if p.exists():
        cfg["enrich_cap"] = _norm_chain_default("enrich_cap", _int_or_none(p.read_text()))
    p = fx / "target_roles.txt"
    if p.exists():
        cfg["target_roles"] = [r.strip() for r in p.read_text().strip().split(",") if r.strip()]
    p = fx / "fetch_pages.txt"
    if p.exists():
        for ln in p.read_text().splitlines():
            s = ln.strip()
            if s.startswith("#"):
                notes.append(f"fetch_pages: {s.lstrip('#').strip()}")
        cfg["fetch_pages"] = _lines(p)
    p = fx / "linkedin.json"
    if p.exists():
        try:
            li = json.loads(p.read_text())
        except Exception:
            li = "<invalid linkedin.json>"
        if isinstance(li, dict):
            notes += [f"linkedin.{loc}: {txt}" for loc, txt in collect_notes(li)]
            li = strip_notes(li)
        cfg["linkedin"] = li
    for key in ("li_max_per_company", "li_max_people"):
        p = fx / f"{key}.txt"
        if p.exists():
            cfg[key] = _norm_chain_default(key, _int_or_none(p.read_text()))
    p = fx / "sourcing.json"
    if p.exists():
        try:
            src = json.loads(p.read_text())
        except Exception:
            src = "<invalid sourcing.json>"
        if isinstance(src, dict):
            for loc, txt in collect_notes(src):
                notes.append(txt if loc in ("_comment", "_note") else f"{loc}: {txt}")
            src = strip_notes(src)
        cfg["sourcing"] = src
    cfg["notes"] = notes
    return cfg


def load(fixture_dir: Path | str) -> dict[str, Any]:
    """campaign.json with defaults applied, or the same dict from legacy files."""
    fx = Path(fixture_dir)
    cj = fx / CAMPAIGN_FILE
    if not cj.exists():
        return load_legacy(fx)
    raw = json.loads(cj.read_text())          # a JSON error is the caller's ABORT
    if not isinstance(raw, dict):
        raise ValueError(f"{CAMPAIGN_FILE} must be a JSON object")
    cfg = defaults()
    cfg.update(raw)
    for key in CHAIN_DEFAULTS:
        cfg[key] = _norm_chain_default(key, cfg.get(key))
    return cfg


# --------------------------------------------------------------------------
# validate -- every check fire_campaign.sh used to do in bash, and the types
# --------------------------------------------------------------------------
def _sourcing_errors(src: Any) -> list[str]:
    """Mirror of run_fire.py resolve_sourcing()'s pre-flight: a fixture that
    passes here is one the orchestrator will accept."""
    where = "campaign.json sourcing"
    if not isinstance(src, dict):
        return [f"{where}: must be a JSON object with a \"selector\"+\"vertical\", "
                f"a \"targets\" list, or a \"sources\" list"]
    errs: list[str] = []
    srcs = src.get("sources")

    def check_targets(ts: Any, label: str, need: tuple[str, ...]) -> None:
        if not isinstance(ts, list) or not ts:
            errs.append(f"{where}: {label} has no targets")
            return
        for j, t in enumerate(ts, 1):
            if not isinstance(t, dict) or any(not t.get(k) for k in need):
                errs.append(f"{where}: {label} target {j} needs {'+'.join(need)}")

    if isinstance(srcs, list) and srcs:
        for i, s in enumerate(srcs, 1):
            if not isinstance(s, dict) or not s.get("type"):
                errs.append(f"{where}: sources[{i}] needs a \"type\"")
                continue
            t = s["type"]
            if t not in KNOWN_SOURCE_TYPES:
                errs.append(f"{where}: sources[{i}] has unknown type {t!r} "
                            f"(known: {', '.join(KNOWN_SOURCE_TYPES)})")
            elif t == "map":
                ts = s.get("targets") or src.get("targets") or ([src] if src.get("selector") else [])
                check_targets(ts, f"map source {i}", ("selector", "vertical"))
            elif t == "places":
                check_targets(s.get("targets"), f"places source {i}", ("included_type", "vertical"))
            elif t in ("gmaps", "overture"):
                # terms / categories are optional: shared presets resolve them
                check_targets(s.get("targets"), f"{t} source {i}", ("vertical",))
            # directory: url/name/link validated by source_directory.py itself
    else:
        ts = src.get("targets") or ([src] if src.get("selector") else [])
        if not ts:
            errs.append(f"{where}: needs a \"selector\"+\"vertical\" (the OSM tag filter naming "
                        f"this run's target audience, e.g. '\"office\"=\"lawyer\"'), a \"targets\" "
                        f"list for a multi-vertical campaign, or a \"sources\" list")
        else:
            check_targets(ts, "targets", ("selector", "vertical"))
    return errs


def source_types(cfg: dict[str, Any]) -> set[str]:
    src = cfg.get("sourcing")
    if not isinstance(src, dict):
        return set()
    srcs = src.get("sources")
    if isinstance(srcs, list) and srcs:
        return {str(s.get("type")) for s in srcs if isinstance(s, dict)}
    return {"map"}


def validate(cfg: dict[str, Any], fixture_dir: Path | str) -> list[str]:
    fx = Path(fixture_dir)
    name = fx.name
    errs: list[str] = []

    # --- shape -------------------------------------------------------------
    for k in cfg:
        if k not in SCHEMA:
            errs.append(f"campaign.json: unknown key {k!r} (known: {', '.join(SCHEMA)})")
    chans = cfg.get("channels")
    if not isinstance(chans, list) or not chans or not all(isinstance(c, str) for c in chans):
        errs.append("campaign.json: channels must be a non-empty list of strings")
        chans = []
    for c in chans:
        if c not in KNOWN_CHANNELS:
            errs.append(f"campaign.json: unknown channel {c!r} (known: {', '.join(KNOWN_CHANNELS)})")
    mode = cfg.get("draft_mode")
    if mode not in ("template", "custom"):
        errs.append(f"campaign.json: draft_mode must be \"template\" or \"custom\", got {mode!r}")
    if cfg.get("vertical") is not None and not (isinstance(cfg["vertical"], str) and cfg["vertical"].strip()):
        errs.append("campaign.json: vertical must be a non-empty string or null")
    countries = cfg.get("countries")
    if not isinstance(countries, list) or not all(isinstance(c, str) for c in countries):
        errs.append("campaign.json: countries must be a list of ISO-2 codes")
        countries = []
    for c in countries:
        if c not in ("*", "ALL") and not ISO2_RE.match(c):
            errs.append(f"campaign.json: countries entry {c!r} is not an ISO-2 code (or \"*\")")
    for k in ("enrich_cap", "li_max_per_company", "li_max_people"):
        v = cfg.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v <= 0):
            errs.append(f"campaign.json: {k} must be a positive integer or null")
    for k in ("target_roles", "fetch_pages", "notes"):
        v = cfg.get(k)
        if not isinstance(v, list) or not all(isinstance(s, str) and s.strip() for s in v):
            errs.append(f"campaign.json: {k} must be a list of non-empty strings")
    for s in cfg.get("fetch_pages") or []:
        if isinstance(s, str) and len([p for p in s.split("|", 1) if p.strip()]) != 2:
            errs.append(f"campaign.json: fetch_pages entry {s!r} must be `slug|path`")
    li = cfg.get("linkedin")
    if li is not None and not isinstance(li, dict):
        errs.append("campaign.json: linkedin must be an object or null")
    if cfg.get("sourcing") is None:
        errs.append("campaign.json: sourcing is required -- the Stage-2 TARGET AUDIENCE, e.g. "
                    "{\"selector\":\"\\\"office\\\"=\\\"lawyer\\\"\",\"vertical\":\"law\"}")
    else:
        errs.extend(_sourcing_errors(cfg["sourcing"]))

    # --- content files -----------------------------------------------------
    if not (fx / "icp.yaml").exists():
        errs.append(f"missing icp.yaml (templates/{name}/icp.yaml -- the human-readable target)")
    for jf in ("pitch.json", "qualify.json"):
        p = fx / jf
        if p.exists():
            try:
                json.loads(p.read_text())
            except Exception as e:
                errs.append(f"{jf} is not valid JSON: {e}")

    # WHERE: gmaps/overture/places abort at run time without places.txt; map
    # falls back to the built-in EU table filtered by countries.txt.
    places = fx / "places.txt"
    has_places = places.exists() and places.stat().st_size > 0
    types = source_types(cfg)
    bound = sorted(types & set(PLACES_BOUND_SOURCES))
    if has_places:
        if not any(PLACE_ROW_RE.match(ln) for ln in places.read_text().splitlines()):
            errs.append("places.txt has no real place rows (comments only) -- "
                        "`City|ISO2|lat|lon|half_width_deg`, one per line")
    elif bound:
        errs.append(f"missing places.txt -- the {', '.join(bound)} source(s) sweep places "
                    f"(`City|ISO2|lat|lon|half_width_deg`, one per line) and abort without it")
    elif not countries:
        errs.append("missing places.txt (`City|ISO2|lat|lon|half_width_deg`, one per line) "
                    "and campaign.json countries is empty -- Stage 2 sweeps places, so a "
                    "fixture must declare WHERE it looks")

    # --- copy / channel gates ----------------------------------------------
    email_on = "email" in chans
    li_on = "linkedin" in chans
    pitch = fx / "pitch.json"
    if mode == "template" and (email_on or li_on) and not pitch.exists():
        errs.append("missing pitch.json (template mode needs the approved copy -- see the "
                    "TEMPLATE-FIRST contract in templates/README.md: ask the user for their "
                    "email template, or propose examples and confirm)")
    if mode == "custom" and not (isinstance(cfg.get("vertical"), str) and cfg["vertical"].strip()):
        errs.append("campaign.json: draft_mode=custom requires vertical")
    if li_on:
        if not isinstance(li, dict):
            errs.append("campaign.json: channels has \"linkedin\" but linkedin is null -- a LinkedIn "
                        "campaign must declare its person gates: {\"countries\":[\"AE\"],"
                        "\"accept_tiers\":[\"T1\"],\"alive_days\":90,\"title_tiers\":{...}} "
                        "(see templates/gcc-outreach-li/campaign.json)")
        if pitch.exists() and "linkedin_angle" not in pitch.read_text():
            errs.append("pitch.json has no \"linkedin_angle\" -- li-writer needs the ANGLE (what we "
                        "sell) to compose each DM. LinkedIn never renders body_template")
    return errs


# --------------------------------------------------------------------------
# materialize
# --------------------------------------------------------------------------
def materialize(cfg: dict[str, Any], run_dir: Path | str, fixture_dir: Path | str | None = None) -> list[str]:
    """Write the legacy per-file layout into `run_dir` (created if needed).
    With `fixture_dir`, also copy the content files and campaign.json itself so
    the run records its source. Returns the file names written."""
    run = Path(run_dir)
    run.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for key, spec in SCHEMA.items():
        if spec["file"] is None:
            continue
        text = render(key, cfg.get(key, spec["default"]))
        if text is None:
            continue
        (run / spec["file"]).write_text(text)
        written.append(spec["file"])
    if fixture_dir is not None:
        fx = Path(fixture_dir)
        for name in CONTENT_FILES + (CAMPAIGN_FILE,):
            src = fx / name
            if src.exists():
                shutil.copyfile(src, run / name)
                written.append(name)
    return written


def ordered(cfg: dict[str, Any]) -> dict[str, Any]:
    """campaign.json with keys in SCHEMA order (unknown keys last, for validate to name)."""
    out = {k: cfg.get(k, spec["default"]) for k, spec in SCHEMA.items()}
    out.update({k: v for k, v in cfg.items() if k not in SCHEMA})
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", required=True, help="templates/<base>")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--validate", action="store_true", help="print every error; exit 1 if any")
    g.add_argument("--materialize", metavar="RUN_DIR", help="validate, then write the per-file layout")
    g.add_argument("--show", action="store_true", help="print the resolved config as JSON")
    a = ap.parse_args(argv)

    fx = Path(a.fixture)
    if not fx.is_dir():
        print(f"ABORT: no fixture at {fx}")
        return 1
    try:
        cfg = load(fx)
    except Exception as e:
        print(f"ABORT: {fx}/{CAMPAIGN_FILE} is not valid JSON: {e}")
        return 1
    if a.show:
        print(json.dumps(ordered(cfg), indent=2, ensure_ascii=False))
        return 0
    errs = validate(cfg, fx)
    if errs:
        for e in errs:
            print(f"ABORT: {fx}: {e}")
        return 1
    if a.validate:
        src = CAMPAIGN_FILE if (fx / CAMPAIGN_FILE).exists() else "legacy files"
        print(f"{fx}: fire-ready ({src}; draft_mode={cfg['draft_mode']}, "
              f"channels={','.join(cfg['channels'])}, sources={','.join(sorted(source_types(cfg)))})")
        return 0
    written = materialize(cfg, a.materialize, fx)
    print(f"Materialised {a.materialize} from {fx}: {', '.join(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
