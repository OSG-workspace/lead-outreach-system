"""campaign.json is the fixture contract; the stages still read the per-file
layout from the RUN folder. These tests pin the bridge between the two:

  * legacy files -> load() -> materialize() reproduces the legacy files
    byte-for-byte (three real fixtures: a template/email one, the
    custom+whatsapp one, the linkedin one), modulo notes and default-valued
    files that are allowed to drop out;
  * validate() names each missing content file and each bad knob;
  * a fixture with only legacy files (no campaign.json) still loads and fires;
  * migrate_fixture converts a legacy copy and refuses nothing it should accept.

No network, no venv deps: campaign_config is stdlib only.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

import campaign_config as cc      # noqa: E402
import migrate_fixture as mf      # noqa: E402

TEMPLATES = PROJECT / "templates"
ROUND_TRIP_FIXTURES = ["gcc-receptionist", "lb-enterprise", "gcc-outreach-li"]

KNOB_FILES = [s["file"] for s in cc.SCHEMA.values() if s["file"]]


def legacy_copy(base: str, dest: Path) -> Path:
    """A tmp copy of a real fixture in the LEGACY layout: materialise its
    campaign.json into the per-file layout, then drop campaign.json."""
    fx = TEMPLATES / base
    cc.materialize(cc.load(fx), dest, fx)
    (dest / cc.CAMPAIGN_FILE).unlink()
    assert not (dest / cc.CAMPAIGN_FILE).exists()
    return dest


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


# --------------------------------------------------------------------------
# legacy -> load -> materialize round trip
# --------------------------------------------------------------------------
@pytest.mark.parametrize("base", ROUND_TRIP_FIXTURES)
def test_legacy_round_trip_is_byte_identical(tmp_path, base):
    legacy = legacy_copy(base, tmp_path / "legacy")
    cfg = cc.load(legacy)                       # no campaign.json -> legacy path
    assert cc.validate(cfg, legacy) == []
    again = tmp_path / "again"
    cc.materialize(cfg, again, legacy)
    for f in KNOB_FILES + list(cc.CONTENT_FILES):
        a, b = legacy / f, again / f
        assert a.exists() == b.exists(), f"{base}: {f} presence differs"
        if a.exists():
            assert a.read_bytes() == b.read_bytes(), f"{base}: {f} bytes differ"
    assert not (again / cc.CAMPAIGN_FILE).exists()


@pytest.mark.parametrize("base", ROUND_TRIP_FIXTURES)
def test_legacy_load_equals_campaign_json_load_modulo_notes(tmp_path, base):
    legacy = legacy_copy(base, tmp_path / "legacy")
    from_legacy = cc.load(legacy)
    from_json = cc.load(TEMPLATES / base)
    for key in cc.SCHEMA:
        if key == "notes":
            continue
        assert from_legacy[key] == from_json[key], f"{base}: {key} differs"


def test_round_trip_fixtures_cover_the_three_shapes():
    """The parametrised fixtures really are one template/email, one
    custom+whatsapp and one linkedin campaign, or the round trip proves less
    than it claims."""
    modes = {b: cc.load(TEMPLATES / b) for b in ROUND_TRIP_FIXTURES}
    assert modes["gcc-receptionist"]["draft_mode"] == "template"
    assert "email" in modes["gcc-receptionist"]["channels"]
    assert modes["lb-enterprise"]["draft_mode"] == "custom"
    assert modes["lb-enterprise"]["channels"] == ["whatsapp"]
    assert modes["lb-enterprise"]["vertical"]
    assert modes["gcc-outreach-li"]["channels"] == ["linkedin"]
    assert isinstance(modes["gcc-outreach-li"]["linkedin"], dict)


@pytest.mark.parametrize("base", sorted(d.name for d in TEMPLATES.iterdir()
                                        if d.is_dir() and d.name not in mf.NOT_FIXTURES))
def test_every_real_fixture_validates(base):
    fx = TEMPLATES / base
    assert (fx / cc.CAMPAIGN_FILE).exists(), f"{base} has no campaign.json"
    assert cc.validate(cc.load(fx), fx) == []


# --------------------------------------------------------------------------
# materialised byte formats -- exactly what the stage scripts parse
# --------------------------------------------------------------------------
def test_materialised_files_have_no_note_keys_and_the_readers_format(tmp_path):
    fx = TEMPLATES / "gcc-outreach-li"
    cfg = cc.load(fx)
    cfg["notes"] = ["free prose", "never materialised"]
    written = cc.materialize(cfg, tmp_path, fx)
    assert cc.CAMPAIGN_FILE in written                     # the run records its source
    for jf in ("sourcing.json", "linkedin.json"):
        text = (tmp_path / jf).read_text()
        assert text.startswith('{\n  "') and text.endswith("\n}\n")   # indent 2 + newline
        assert not any(str(k).startswith("_") for k in walk_keys(json.loads(text)))
    assert (tmp_path / "channels.json").read_bytes() == b'["linkedin"]\n'
    assert (tmp_path / "countries.txt").read_bytes() == b"AE\nSA\nQA\n"
    assert not (tmp_path / "notes.txt").exists()
    assert "free prose" not in "".join(p.read_text() for p in tmp_path.iterdir()
                                       if p.name != cc.CAMPAIGN_FILE)


def test_defaults_are_not_materialised(tmp_path):
    cfg = cc.defaults()
    cfg.update({"sourcing": {"selector": '"office"="lawyer"', "vertical": "law"},
                "enrich_cap": 400, "li_max_per_company": 3, "li_max_people": 60})
    written = cc.materialize(cfg, tmp_path)
    assert written == ["sourcing.json"]
    for f in ("channels.json", "draft_mode.txt", "countries.txt", "enrich_cap.txt",
              "li_max_per_company.txt", "li_max_people.txt", "vertical.txt",
              "target_roles.txt", "fetch_pages.txt", "linkedin.json"):
        assert not (tmp_path / f).exists(), f


def test_knob_byte_formats():
    assert cc.render("channels", ["email", "whatsapp"]) == '["email", "whatsapp"]\n'
    assert cc.render("draft_mode", "custom") == "custom\n"
    assert cc.render("vertical", "enterprise") == "enterprise\n"
    assert cc.render("countries", ["LB"]) == "LB\n"
    assert cc.render("countries", ["*"]) == "*\n"
    assert cc.render("enrich_cap", 250) == "250\n"
    assert cc.render("target_roles", ["Owner", "CEO"]) == "Owner, CEO\n"
    assert cc.render("fetch_pages", ["contact|/contact", "about|/about"]) == "contact|/contact\nabout|/about\n"
    assert cc.render("draft_mode", "template") is None
    assert cc.render("enrich_cap", 400) is None


# --------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------
def make_fixture(tmp_path: Path, base: str = "gcc-receptionist") -> Path:
    dst = tmp_path / base
    shutil.copytree(TEMPLATES / base, dst)
    return dst


def errors_of(fx: Path) -> list[str]:
    return cc.validate(cc.load(fx), fx)


def test_valid_fixture_has_no_errors(tmp_path):
    assert errors_of(make_fixture(tmp_path)) == []


def test_missing_icp_yaml(tmp_path):
    fx = make_fixture(tmp_path)
    (fx / "icp.yaml").unlink()
    assert any(e.startswith("missing icp.yaml") for e in errors_of(fx))


def test_missing_pitch_json_in_template_mode(tmp_path):
    fx = make_fixture(tmp_path)
    (fx / "pitch.json").unlink()
    errs = errors_of(fx)
    assert any(e.startswith("missing pitch.json") and "TEMPLATE-FIRST" in e for e in errs)


def test_pitch_json_not_required_for_custom_whatsapp(tmp_path):
    fx = make_fixture(tmp_path, "lb-enterprise")
    assert not (fx / "pitch.json").exists()
    assert errors_of(fx) == []


def test_missing_places_txt_when_a_source_needs_it(tmp_path):
    fx = make_fixture(tmp_path)
    (fx / "places.txt").unlink()
    errs = errors_of(fx)
    assert any(e.startswith("missing places.txt") and "gmaps" in e for e in errs)


def test_places_txt_with_only_comments(tmp_path):
    fx = make_fixture(tmp_path)
    (fx / "places.txt").write_text("# City|ISO2|lat|lon|half\n")
    assert any("no real place rows" in e for e in errors_of(fx))


def test_map_only_fixture_may_rely_on_countries(tmp_path):
    fx = make_fixture(tmp_path)
    cfg = cc.load(fx)
    cfg["sourcing"] = {"selector": '"amenity"="clinic"', "vertical": "clinic"}
    cfg["countries"] = ["DE"]
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    (fx / "places.txt").unlink()
    assert errors_of(fx) == []
    cfg["countries"] = []
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    assert any(e.startswith("missing places.txt") and "countries is empty" in e for e in errors_of(fx))


def test_custom_mode_requires_vertical(tmp_path):
    fx = make_fixture(tmp_path)
    cfg = cc.load(fx)
    cfg["draft_mode"] = "custom"
    cfg["vertical"] = None
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    assert "campaign.json: draft_mode=custom requires vertical" in errors_of(fx)


def test_linkedin_channel_requires_gates_and_angle(tmp_path):
    fx = make_fixture(tmp_path, "gcc-outreach-li")
    cfg = cc.load(fx)
    cfg["linkedin"] = None
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    assert any("linkedin is null" in e for e in errors_of(fx))
    cfg = cc.load(TEMPLATES / "gcc-outreach-li")
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    pitch = json.loads((fx / "pitch.json").read_text())
    pitch.pop("linkedin_angle", None)
    (fx / "pitch.json").write_text(json.dumps(pitch))
    assert any("linkedin_angle" in e for e in errors_of(fx))


def test_sourcing_shape_errors(tmp_path):
    fx = make_fixture(tmp_path)
    cfg = cc.load(fx)
    cfg["sourcing"] = {"sources": [{"type": "teleport", "targets": [{"vertical": "x"}]},
                                   {"type": "places", "targets": [{"vertical": "x"}]},
                                   {"targets": []}]}
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    errs = errors_of(fx)
    assert any("unknown type 'teleport'" in e for e in errs)
    assert any("places source 2 target 1 needs included_type+vertical" in e for e in errs)
    assert any('sources[3] needs a "type"' in e for e in errs)
    cfg["sourcing"] = {"max_per_run": 10}
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    assert any('needs a "selector"+"vertical"' in e for e in errors_of(fx))


def test_unknown_key_and_bad_types_are_named(tmp_path):
    fx = make_fixture(tmp_path)
    cfg = cc.load(fx)
    cfg["draft_mod"] = "custom"
    cfg["channels"] = ["email", "pigeon"]
    cfg["countries"] = ["Saudi"]
    cfg["enrich_cap"] = "lots"
    (fx / cc.CAMPAIGN_FILE).write_text(cc.dumps_pretty(cc.ordered(cfg)))
    errs = errors_of(fx)
    assert any("unknown key 'draft_mod'" in e for e in errs)
    assert any("unknown channel 'pigeon'" in e for e in errs)
    assert any("'Saudi' is not an ISO-2 code" in e for e in errs)
    assert any("enrich_cap must be a positive integer" in e for e in errs)


# --------------------------------------------------------------------------
# legacy fixtures keep working; the migrator converts them
# --------------------------------------------------------------------------
def test_fixture_with_only_legacy_files_still_loads_and_validates(tmp_path):
    legacy = legacy_copy("lb-enterprise", tmp_path / "lb-enterprise")
    assert sorted(p.name for p in legacy.iterdir()) == sorted(
        ["channels.json", "draft_mode.txt", "vertical.txt", "countries.txt",
         "sourcing.json", "icp.yaml", "places.txt"])
    cfg = cc.load(legacy)
    assert cfg["channels"] == ["whatsapp"]
    assert cfg["draft_mode"] == "custom"
    assert cfg["vertical"] == "enterprise"
    assert cfg["countries"] == ["LB"]
    assert cc.validate(cfg, legacy) == []


def test_legacy_default_valued_files_load_as_defaults(tmp_path):
    fx = tmp_path / "x"
    fx.mkdir()
    (fx / "draft_mode.txt").write_text("template\n")
    (fx / "enrich_cap.txt").write_text("400\n")
    (fx / "channels.json").write_text('["email"]\n')
    (fx / "li_max_per_company.txt").write_text("3\n")
    (fx / "sourcing.json").write_text(json.dumps({"_comment": "hi", "selector": "a", "vertical": "b"}))
    cfg = cc.load(fx)
    assert cfg["draft_mode"] == "template" and cfg["enrich_cap"] is None
    assert cfg["channels"] == ["email"] and cfg["li_max_per_company"] is None
    assert cfg["sourcing"] == {"selector": "a", "vertical": "b"}
    assert cfg["notes"] == ["hi"]


def test_migrator_converts_a_legacy_copy(tmp_path):
    legacy = legacy_copy("gcc-outreach-li", tmp_path / "gcc-outreach-li")
    # give it the notes the real fixture used to carry, so the move is exercised
    src = json.loads((legacy / "sourcing.json").read_text())
    src["_comment"] = "campaign specific fact"
    src["sources"][0]["_note"] = mf.GENERIC_NOTES[0]
    (legacy / "sourcing.json").write_text(cc.dumps_pretty(src))
    assert mf.migrate(legacy, dry_run=False) is True
    names = sorted(p.name for p in legacy.iterdir())
    assert names == ["campaign.json", "icp.yaml", "pitch.json", "places.txt"]
    cfg = cc.load(legacy)
    assert cfg == cc.load(legacy)  # stable
    assert cfg["notes"] == ["campaign specific fact"]            # generic paragraph dropped
    assert cfg["channels"] == ["linkedin"] and isinstance(cfg["linkedin"], dict)
    assert cc.validate(cfg, legacy) == []
    for key in cc.SCHEMA:
        if key != "notes":
            assert cfg[key] == cc.load(TEMPLATES / "gcc-outreach-li")[key], key


def test_migrator_refuses_when_round_trip_breaks(tmp_path, monkeypatch):
    legacy = legacy_copy("gcc-receptionist", tmp_path / "gcc-receptionist")
    monkeypatch.setattr(mf, "expected_bytes", lambda fixture, fname: b"not what was written")
    assert mf.migrate(legacy, dry_run=False) is False
    assert not (legacy / cc.CAMPAIGN_FILE).exists()
    assert (legacy / "sourcing.json").exists()


def test_cli_validate_and_materialize(tmp_path):
    import subprocess
    py = sys.executable
    script = PROJECT / "tools" / "scripts" / "campaign_config.py"
    r = subprocess.run([py, str(script), "--fixture", str(TEMPLATES / "lb-enterprise"), "--validate"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "fire-ready" in r.stdout
    fx = make_fixture(tmp_path)
    (fx / "icp.yaml").unlink()
    r = subprocess.run([py, str(script), "--fixture", str(fx), "--validate"], capture_output=True, text=True)
    assert r.returncode == 1 and "ABORT" in r.stdout and "missing icp.yaml" in r.stdout
    run = tmp_path / "run"
    r = subprocess.run([py, str(script), "--fixture", str(TEMPLATES / "lb-enterprise"),
                        "--materialize", str(run)], capture_output=True, text=True)
    assert r.returncode == 0
    assert sorted(p.name for p in run.iterdir()) == sorted(
        ["campaign.json", "channels.json", "countries.txt", "draft_mode.txt", "icp.yaml",
         "places.txt", "sourcing.json", "vertical.txt"])
