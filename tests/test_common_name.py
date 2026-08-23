"""The exported photo's common name comes from the offline catalog, matched by
WCS footprint -- never from a hardcoded name table.

`metadata.py` used to carry a six-entry `_COMMON` dict, which happened to be
exactly the reference set: M8 got "Lagoon Nebula" and M42 got nothing. The name
is cosmetic (XMP:Title, keywords, the documentation profile's annotation), but
the mechanism was the last object-name literal table in the codebase, and it
made the deliverable's title depend on whether the maintainer had personally
shot the object.

The catalog match is spatial (`known_targets.match_target`), so identity still
comes from where the telescope was pointed, never from the OBJECT header string
-- the target-agnostic law is untouched (AGENTS.md §13).
"""

from __future__ import annotations

import seestar.metadata as M
import seestar.orchestrate as orch


def _fixtures():
    inspection = {
        "meta": {
            "target": "X1",
            "frames": 51,
            "sub_exposure_s": 10.0,
            "total_integration_min": 8.5,
            "filter": "LP",
            "datetime": "2026-06-15-221349",
        },
        "header": {"has_wcs": False},
        "classification": {"category": "nebula"},
    }
    params = {
        "bge_smoothing": 0.5,
        "bge_correction": "Subtraction",
        "denoise_strength": 0.5,
        "asinh_d": 75,
        "blackpoint": 0.0145,
        "mtf_mid": 0.08,
        "saturation": 0.6,
        "pcc": False,
        "profile": "showcase",
    }
    cfg = {
        "defaults": {"telescope": "scope", "artist": ""},
        "graxpert": {"bge_model": "1.0.1", "denoise_model": "3.0.2"},
    }
    return inspection, params, cfg


# ---------------------------------------------------------------------------
# the metadata card
# ---------------------------------------------------------------------------


def test_supplied_common_name_reaches_the_title_and_keywords():
    inspection, params, cfg = _fixtures()

    card = M._build_card(inspection, params, "final", cfg, common_name="Lagoon Nebula")

    assert card.title == "X1 — Lagoon Nebula"
    assert "Lagoon Nebula" in card.keywords
    assert card.object_name == "X1", "IPTC:ObjectName stays the bare target label"


def test_without_a_common_name_the_title_is_the_bare_target():
    """The honest default. Most of the sky has no common name, and a catalog miss
    (no WCS, no match, an ambiguous field) must read as "no name known" rather
    than inventing one."""
    inspection, params, cfg = _fixtures()

    card = M._build_card(inspection, params, "final", cfg)

    assert card.title == "X1"
    assert "" not in card.keywords, "an empty common name must not become an empty keyword"


def test_blank_common_name_is_treated_as_absent():
    """The catalog stores `common_name: None` for most entries, but a curated
    entry could carry whitespace; a title ending in an em dash would be worse
    than no name at all."""
    inspection, params, cfg = _fixtures()

    assert M._build_card(inspection, params, "final", cfg, common_name="   ").title == "X1"


def test_metadata_module_has_no_hardcoded_common_name_table():
    """The point of the change: no name literals in code. If a future edit
    reintroduces a lookup table, this fails."""
    assert not hasattr(M, "_COMMON")


# ---------------------------------------------------------------------------
# where the name comes from in a real run
# ---------------------------------------------------------------------------


def test_catalog_hit_supplies_the_common_name():
    record = _record(common_name="Lagoon Nebula")
    side = {"local_prior": {"status": "hit", "record": record}}

    assert orch._catalog_common_name(side) == "Lagoon Nebula"


def test_catalog_miss_supplies_no_common_name():
    for side in (
        {},
        {"local_prior": {"status": "miss"}},
        {"local_prior": {"status": "ambiguous"}},
        {"local_prior": {"status": "local_catalog_unavailable"}},
    ):
        assert orch._catalog_common_name(side) is None


def test_spatially_impossible_record_is_never_used_for_a_name():
    """That status means the record's OWN stored position is invalid (a
    data-integrity guard). It carries a record, but nothing about it is
    trustworthy enough to print on a photo."""
    record = _record(common_name="Should Not Appear")
    side = {"local_prior": {"status": "spatially_impossible", "record": record}}

    assert orch._catalog_common_name(side) is None


def test_contradicted_hit_still_supplies_its_name():
    """A contradicted hit is one whose asserted CATEGORY was rejected by the final
    classification; its identity is still kept and reported (see
    `_prior_outcome_note`). The catalog said where this is, and measurement
    overrode what it is -- the name stays."""
    record = _record(common_name="Lagoon Nebula")
    side = {"local_prior": {"status": "hit", "record": record, "conflict": "category"}}

    assert orch._catalog_common_name(side) == "Lagoon Nebula"


def test_hit_without_a_common_name_yields_none():
    side = {"local_prior": {"status": "hit", "record": _record(common_name=None)}}

    assert orch._catalog_common_name(side) is None


def _record(common_name):
    from seestar.core.types import Category
    from seestar.known_targets import LocalTargetRecord

    return LocalTargetRecord(
        entry_id="ngc6523",
        canonical_id="M8",
        common_name=common_name,
        aliases=(),
        equivalent_ids=(),
        category=Category.NEBULA,
        ra_deg=270.9,
        dec_deg=-24.38,
        extent_major_arcmin=90.0,
        extent_minor_arcmin=40.0,
        search_footprints=(),
        morphology_traits=(),
        associated=(),
        provenance={},
        lookup_role="primary",
    )


# ---------------------------------------------------------------------------
# the shipped catalog actually carries the names (a data check, not a mapping)
# ---------------------------------------------------------------------------


def test_shipped_catalog_covers_far_more_than_the_old_six_entry_table():
    """The user-visible win: objects nobody on this project has photographed now
    get their names too. Asserted as a floor, not an exact list -- the catalog is
    a regenerable lockfile, not a hand-maintained mapping."""
    from seestar.known_targets import load_catalog

    named = {r.canonical_id: r.common_name for r in load_catalog() if r.common_name}

    assert len(named) > 50
    assert named.get("M8") == "Lagoon Nebula"  # was in the old table
    assert named.get("M42"), "M42 had no common name before; the catalog has one"
    assert named.get("M31"), "nor did M31"
