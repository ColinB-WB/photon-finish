"""Tests for tools/build_known_targets.py -- the fail-closed, deterministic
maintainer build tool that regenerates seestar/data/known_targets.json from a
pinned OpenNGC source directory + a schema-v2 target_curation.yaml.

MAINTAINER TOOLING (tools/): does not run at processing time, does not touch
seestar.core. See docs/history/superpowers/specs/2026-07-25-known-targets-prototype-design.md
sec1/sec4 and .superpowers/sdd/planA-*.

Migrated to schema v2 + the directory source (Task 3). Every build test constructs
an IN-MEMORY v2 curation dict and calls `build_v2(curation, index, version, out)`
with index/version from `load_openngc_source(<dir>)` -- no test builds from the real
target_curation.yaml (still v1 on disk until a later task) or from the old single-CSV
`build()` (retired in Task 3). The committed-catalog / lockfile assertion is
recreated in the later curation-population task against the regenerated v2 catalog.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = REPO_ROOT / "tools" / "data" / "openngc-v20260501"


def _load_module():
    """Import tools/build_known_targets.py by path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location("build_known_targets", REPO_ROOT / "tools" / "build_known_targets.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_known_targets"] = module
    spec.loader.exec_module(module)
    return module


bkt = _load_module()
VERSION, INDEX = bkt.load_openngc_source(str(SOURCE_DIR))


def _base_curation() -> dict:
    """A fresh, mutable schema-v2 curation dict mirroring the prototype's five
    objects (M8/NGC6523, NGC6530 associated_only, NGC7000, NGC5128, NGC5139) via an
    explicit `include`, with exception-only annotations. Safe for tests to mutate."""
    return {
        "schema_version": 2,
        "selection": {"include": ["ngc6523", "ngc6530", "ngc7000", "ngc5128", "ngc5139"]},
        "resolution": {"abstain": {}},
        "annotations": {
            "ngc6523": {
                "canonical_id": "M8",
                "aliases_add": [],
                "morphology_traits": ["emission_nebula", "star_forming_region"],
                "search_footprints_override": {
                    "reason": "prototype curated footprint",
                    "footprints": [{"shape": "ellipse", "major_arcmin": 90.0, "minor_arcmin": 40.0, "pa_deg": 0.0}],
                },
                "associated": [{"canonical_id": "NGC6530", "relationship": "embedded_cluster"}],
            },
            "ngc6530": {
                "lookup_role": "associated_only",
                "classification_override": {"family": "starfield", "reason": "embedded cluster inside M8"},
                "morphology_traits": ["open_cluster"],
                "search_footprints_override": {
                    "reason": "prototype curated footprint",
                    "footprints": [{"shape": "circle", "radius_arcmin": 7.5}],
                },
                "associated": [{"canonical_id": "M8", "relationship": "embedded_in"}],
            },
            "ngc7000": {
                "aliases_add": [],
                "morphology_traits": ["emission_nebula", "frame_filling"],
                "search_footprints_override": {
                    "reason": "prototype curated footprint",
                    "footprints": [{"shape": "ellipse", "major_arcmin": 120.0, "minor_arcmin": 100.0, "pa_deg": 0.0}],
                },
            },
            "ngc5128": {
                "aliases_add": ["Cen A"],
                "morphology_traits": ["peculiar_galaxy", "dust_lane"],
                "search_footprints_override": {
                    "reason": "prototype curated footprint",
                    "footprints": [{"shape": "ellipse", "major_arcmin": 25.7, "minor_arcmin": 20.0, "pa_deg": 35.0}],
                },
            },
            "ngc5139": {
                "aliases_add": [],
                "morphology_traits": ["globular_cluster", "dense_core"],
                "search_footprints_override": {
                    "reason": "prototype curated footprint",
                    "footprints": [{"shape": "circle", "radius_arcmin": 18.15}],
                },
            },
        },
    }


def _build(curation: dict, out_path=None) -> dict:
    return bkt.build_v2(copy.deepcopy(curation), INDEX, VERSION, str(out_path) if out_path else None)


# --- basic build shape -----------------------------------------------------------


def test_build_v2_produces_expected_prototype_records(tmp_path):
    result = _build(_base_curation(), tmp_path / "out.json")
    by_canonical = {r["canonical_id"]: r for r in result["records"]}
    assert set(by_canonical) == {"M8", "NGC6530", "NGC7000", "NGC5128", "NGC5139"}

    m8 = by_canonical["M8"]
    assert m8["entry_id"] == "ngc6523"
    assert m8["classification"]["family"] == "nebula"
    assert "NGC6523" in m8["equivalent_ids"]
    assert any(a["canonical_id"] == "NGC6530" and a["relationship"] == "embedded_cluster" for a in m8["associated"])
    assert "NGC6530" not in m8["aliases"]

    ngc6530 = by_canonical["NGC6530"]
    assert ngc6530["classification"]["family"] == "starfield"
    assert any(a["canonical_id"] == "M8" for a in ngc6530["associated"])

    assert result["schema_version"] == bkt.OUTPUT_SCHEMA_VERSION
    assert result["provenance"]["content_hash"]  # filled, not null
    assert result["provenance"]["curation_schema_version"] == 2


def test_build_writes_valid_json_to_out_path(tmp_path):
    out_path = tmp_path / "known_targets.json"
    result = _build(_base_curation(), out_path)
    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk == result


# --- determinism -------------------------------------------------------------


def test_build_is_deterministic_content_hash(tmp_path):
    r1 = _build(_base_curation(), tmp_path / "run1.json")
    r2 = _build(_base_curation(), tmp_path / "run2.json")
    assert r1["provenance"]["content_hash"] == r2["provenance"]["content_hash"]
    assert r1 == r2


def test_build_is_order_independent_of_source_index(tmp_path):
    """Shuffling the source-index iteration order must not change the output."""
    shuffled = {k: INDEX[k] for k in sorted(INDEX, reverse=True)}
    r1 = bkt.build_v2(_base_curation(), INDEX, VERSION, None)
    r2 = bkt.build_v2(_base_curation(), shuffled, VERSION, None)
    assert r1["provenance"]["content_hash"] == r2["provenance"]["content_hash"]
    assert r1 == r2


# --- fail-closed conditions ---------------------------------------------------


def test_orphan_annotation_key_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc1234"] = {"morphology_traits": ["ghost"]}
    with pytest.raises(bkt.BuildError, match="ngc1234"):
        _build(curation, tmp_path / "out.json")


def test_orphan_annotation_key_normalizes_id_forms_before_rejecting(tmp_path):
    """An annotation key that DOES match a selected id under normalization (e.g.
    "NGC 7000" for a selection entry "ngc7000") must NOT be treated as an orphan."""
    curation = _base_curation()
    curation["annotations"]["NGC 7000"] = curation["annotations"].pop("ngc7000")
    result = _build(curation, tmp_path / "out.json")
    by_canonical = {r["canonical_id"]: r for r in result["records"]}
    assert by_canonical["NGC7000"]["morphology_traits"] == ["emission_nebula", "frame_filling"]


def test_unknown_type_without_override_raises_build_error(tmp_path):
    """A selected row whose OpenNGC Type is unmapped and has no curated
    classification_override is fail-closed (IC0067 is Type 'NonEx' -- a code with
    no slot in TYPE_TO_FAMILY, unlike '*'/'**'/'Cl+N'/'Other' which are all mapped)."""
    curation = _base_curation()
    curation["selection"]["include"].append("ic0067")
    with pytest.raises(bkt.BuildError, match="unmapped OpenNGC Type"):
        _build(curation, tmp_path / "out.json")


def test_degenerate_footprint_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc7000"]["search_footprints_override"] = {
        "reason": "test",
        "footprints": [{"shape": "ellipse", "major_arcmin": 0.0, "minor_arcmin": 10.0, "pa_deg": 0.0}],
    }
    with pytest.raises(bkt.BuildError, match="degenerate"):
        _build(curation, tmp_path / "out.json")


def test_duplicate_footprint_raises_build_error(tmp_path):
    curation = _base_curation()
    fp = {"shape": "circle", "radius_arcmin": 18.15}
    curation["annotations"]["ngc5139"]["search_footprints_override"] = {
        "reason": "test",
        "footprints": [fp, dict(fp)],
    }
    with pytest.raises(bkt.BuildError, match="duplicate"):
        _build(curation, tmp_path / "out.json")


def test_degenerate_circle_footprint_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc5139"]["search_footprints_override"] = {
        "reason": "test",
        "footprints": [{"shape": "circle", "radius_arcmin": 0.0}],
    }
    with pytest.raises(bkt.BuildError, match="degenerate"):
        _build(curation, tmp_path / "out.json")


def test_malformed_circle_footprint_raises_build_error(tmp_path):
    curation = _base_curation()
    # missing the required 'radius_arcmin' field
    curation["annotations"]["ngc5139"]["search_footprints_override"] = {
        "reason": "test",
        "footprints": [{"shape": "circle"}],
    }
    with pytest.raises(bkt.BuildError, match="malformed"):
        _build(curation, tmp_path / "out.json")


def test_malformed_footprint_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc5139"]["search_footprints_override"] = {
        "reason": "test",
        "footprints": [{"shape": "hexagon", "radius_arcmin": 5.0}],
    }
    with pytest.raises(bkt.BuildError, match="malformed"):
        _build(curation, tmp_path / "out.json")


def test_schema_version_mismatch_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["schema_version"] = 999
    with pytest.raises(bkt.BuildError, match="schema_version"):
        _build(curation, tmp_path / "out.json")


def test_unknown_top_level_key_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["bogus_top_level"] = 1
    with pytest.raises(bkt.BuildError, match="unknown top-level"):
        _build(curation, tmp_path / "out.json")


def test_unresolved_association_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc6523"]["associated"] = [{"canonical_id": "NGC9999", "relationship": "embedded_cluster"}]
    with pytest.raises(bkt.BuildError, match="NGC9999"):
        _build(curation, tmp_path / "out.json")


# --- lookup_role: emission + fail-closed validation (P1 M8/NGC6530 fix) --------


def test_lookup_role_emitted_for_every_record(tmp_path):
    result = _build(_base_curation(), tmp_path / "out.json")
    by_canonical = {r["canonical_id"]: r for r in result["records"]}
    assert by_canonical["NGC6530"]["lookup_role"] == "associated_only"
    for canonical_id in ("M8", "NGC7000", "NGC5128", "NGC5139"):
        assert by_canonical[canonical_id]["lookup_role"] == "primary"


def test_invalid_lookup_role_value_raises_build_error(tmp_path):
    curation = _base_curation()
    curation["annotations"]["ngc7000"]["lookup_role"] = "bogus_role"
    with pytest.raises(bkt.BuildError, match="lookup_role"):
        _build(curation, tmp_path / "out.json")


def test_orphaned_associated_only_record_raises_build_error(tmp_path):
    """An associated_only record referenced by NO primary's associated[].canonical_id
    is orphaned relational metadata -- fail-closed, not a silent no-op."""
    curation = _base_curation()
    # NGC5139 has no incoming association from any primary -- marking it
    # associated_only with nothing pointing at it is a clean orphan case.
    curation["annotations"]["ngc5139"]["lookup_role"] = "associated_only"
    with pytest.raises(bkt.BuildError, match="orphaned"):
        _build(curation, tmp_path / "out.json")


def test_inconsistently_referenced_associated_only_record_raises_build_error(tmp_path):
    """M8 (primary) references NGC6530 (associated_only) as its embedded cluster,
    but if NGC6530's own `associated` list is edited to no longer point back at M8,
    the relationship is asymmetric -- fail-closed, not silently accepted."""
    curation = _base_curation()
    curation["annotations"]["ngc6530"]["associated"] = [{"canonical_id": "NGC7000", "relationship": "unrelated"}]
    with pytest.raises(bkt.BuildError, match="inconsistently referenced"):
        _build(curation, tmp_path / "out.json")


def test_lookup_role_defaults_to_primary_when_annotation_omits_it(tmp_path):
    curation = _base_curation()
    assert "lookup_role" not in curation["annotations"]["ngc7000"]
    result = _build(curation, tmp_path / "out.json")
    by_canonical = {r["canonical_id"]: r for r in result["records"]}
    assert by_canonical["NGC7000"]["lookup_role"] == "primary"


# --- provenance: openngc fact vs curated override -----------------------------


def test_provenance_distinguishes_openngc_facts_from_curated_overrides(tmp_path):
    result = _build(_base_curation(), tmp_path / "out.json")
    by_canonical = {r["canonical_id"]: r for r in result["records"]}

    ngc5128 = by_canonical["NGC5128"]
    assert "OpenNGC" in ngc5128["provenance"]["classification"]
    assert "OpenNGC" in ngc5128["provenance"]["coordinates"]
    assert ngc5128["provenance"]["footprint"] == "curated"
    assert ngc5128["provenance"]["morphology"] == "curated"

    # NGC6530's family is a curated override (OpenNGC's raw "Cl+N" type code has an
    # ambiguous mapping) -- its classification provenance must say so, distinctly
    # from every OpenNGC-derived sibling record.
    ngc6530 = by_canonical["NGC6530"]
    assert ngc6530["provenance"]["classification"] == "curated"
    assert ngc6530["classification"]["family"] == "starfield"


# --- frozen entry-id rule ------------------------------------------------------


def test_mint_entry_id_matches_hand_authored_convention():
    assert bkt.mint_entry_id("NGC6523") == "ngc6523"
    assert bkt.mint_entry_id("NGC 6523") == "ngc6523"
    assert bkt.mint_entry_id("NGC5128") == "ngc5128"


def test_normalize_id_treats_equivalent_forms_as_equal():
    assert bkt.normalize_id("NGC7000") == bkt.normalize_id("ngc:7000") == bkt.normalize_id("NGC 7000")


# --- Task 4: type-policy + footprint-derivation policy -------------------------


def test_type_star_and_double_map_to_starfield():
    assert bkt._resolve_classification("X", {"Type": "**"}, {})[0] == "starfield"
    assert bkt._resolve_classification("X", {"Type": "*"}, {})[0] == "starfield"


def test_type_cln_and_other_map_to_unknown():
    assert bkt._resolve_classification("X", {"Type": "Cl+N"}, {})[0] == "unknown"
    assert bkt._resolve_classification("X", {"Type": "Other"}, {})[0] == "unknown"


def test_unmapped_future_type_is_fatal():
    with pytest.raises(bkt.BuildError, match="unmapped"):
        bkt._resolve_classification("X", {"Type": "ZZTOP"}, {})


def test_footprint_absent_extent_is_unavailable():
    fp, prov = bkt.derive_footprint({"MajAx": "", "MinAx": "", "PosAng": ""})
    assert fp == [] and prov == "unavailable"


def test_footprint_ellipse_and_circle_provenance():
    e, pe = bkt.derive_footprint({"MajAx": "25.7", "MinAx": "20.0", "PosAng": "35"})
    assert e[0]["shape"] == "ellipse" and pe == "derived-ellipse"
    c, pc = bkt.derive_footprint({"MajAx": "27.0", "MinAx": "", "PosAng": ""})
    assert c[0]["shape"] == "circle" and pc == "derived-circle"


def test_footprint_malformed_geometry_is_fatal_not_circle():
    with pytest.raises(bkt.BuildError, match="malformed"):
        bkt.derive_footprint({"MajAx": "-5", "MinAx": "", "PosAng": ""})
    with pytest.raises(bkt.BuildError, match="malformed"):
        bkt.derive_footprint({"MajAx": "abc", "MinAx": "", "PosAng": ""})


def test_curated_footprint_override_requires_reason():
    ann_ok = {
        "search_footprints_override": {
            "reason": "device FOV",
            "footprints": [{"shape": "circle", "radius_arcmin": 7.5}],
        }
    }
    fp, prov = bkt._resolve_footprints({"MajAx": "1"}, ann_ok)
    assert prov == "curated" and fp[0]["radius_arcmin"] == 7.5
    ann_bad = {"search_footprints_override": {"footprints": [{"shape": "circle", "radius_arcmin": 7.5}]}}
    with pytest.raises(bkt.BuildError, match="reason"):
        bkt._resolve_footprints({"MajAx": "1"}, ann_bad)


# --- Task 6: the committed catalog is exactly what the real curation produces -----


def test_build_v2_matches_committed_catalog():
    """The committed, bundled seestar/data/known_targets.json is byte-for-byte the
    deterministic build of the REAL schema-v2 target_curation.yaml against the pinned
    OpenNGC source -- the re-freeze contract. 219 records (218 primary + associated
    NGC6530); content_hash must match the committed lockfile."""
    import yaml

    _, index = bkt.load_openngc_source(str(REPO_ROOT / "tools" / "data" / "openngc-v20260501"))
    curation = yaml.safe_load((REPO_ROOT / "target_curation.yaml").read_text(encoding="utf-8"))
    built = bkt.build_v2(curation, index, "v20260501", None)
    committed = json.loads((REPO_ROOT / "seestar" / "data" / "known_targets.json").read_text(encoding="utf-8"))
    assert built["provenance"]["content_hash"] == committed["provenance"]["content_hash"]
    assert len(built["records"]) == 219
