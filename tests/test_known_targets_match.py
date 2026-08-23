from __future__ import annotations

from seestar import known_targets as kt
from seestar.core.types import Category


def _record(
    entry_id: str,
    category: Category,
    ra_deg: float,
    dec_deg: float,
    extent_major_arcmin: float | None,
    lookup_role: str = "primary",
) -> kt.LocalTargetRecord:
    """Minimal LocalTargetRecord factory for geometry tests (identity/footprint/
    morphology fields not exercised here are filled with harmless defaults)."""
    return kt.LocalTargetRecord(
        entry_id=entry_id,
        canonical_id=entry_id,
        common_name=None,
        aliases=(),
        equivalent_ids=(),
        category=category,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        extent_major_arcmin=extent_major_arcmin,
        extent_minor_arcmin=extent_major_arcmin,
        search_footprints=(),
        morphology_traits=(),
        associated=(),
        provenance={},
        lookup_role=lookup_role,
    )


# A representative S30 frame: ~130' long-axis FOV centered at (10.0, 0.0).
FRAME_CENTER = (10.0, 0.0)
FOV_ARCMIN = 130.0


def test_match_target_overlapping_record_is_hit():
    # centroid 3' from frame center, extent 90' (radius 45') -- comfortably overlaps
    # the ~65'-radius frame.
    near = _record("A", Category.NEBULA, ra_deg=10.0, dec_deg=0.05, extent_major_arcmin=90.0)
    result = kt.match_target([near], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "hit"
    assert result.record is near


def test_match_target_far_off_record_is_spatially_rejected():
    # centroid 5 deg (300') away, small extent (10', radius 5') -- combined radius
    # (5 + 65 = 70') is far short of the 300' separation: no overlap.
    far = _record("B", Category.GALAXY, ra_deg=10.0, dec_deg=5.0, extent_major_arcmin=10.0)
    result = kt.match_target([far], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "spatially_rejected"
    assert result.record is None


def test_match_target_two_overlapping_records_is_ambiguous():
    a = _record("A", Category.NEBULA, ra_deg=10.0, dec_deg=0.05, extent_major_arcmin=90.0)
    b = _record("B", Category.NEBULA, ra_deg=10.02, dec_deg=-0.03, extent_major_arcmin=60.0)
    result = kt.match_target([a, b], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "ambiguous"
    assert result.record is None


def test_match_target_no_records_is_miss():
    result = kt.match_target([], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "miss"
    assert result.record is None


def test_match_target_off_center_composition_still_hits():
    """Guard against centroid-distance-only matching: the catalog centroid sits 90'
    from the frame center -- outside the frame's own ~65' radius, so a naive
    centroid-vs-FOV-radius test would (wrongly) reject it -- but its large 200'
    extent (radius 100') still overlaps the frame once the catalog extent is
    included in the combined radius (100 + 65 = 165' > 90')."""
    wide = _record("C", Category.NEBULA, ra_deg=10.0, dec_deg=1.5, extent_major_arcmin=200.0)
    separation_arcmin = 90.0
    fov_radius_only = FOV_ARCMIN / 2.0
    assert separation_arcmin > fov_radius_only, "test setup must defeat naive centroid-only matching"

    result = kt.match_target([wide], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "hit"
    assert result.record is wide


def test_match_target_image_footprint_override_extends_reach():
    # separation of 80' exceeds fov_arcmin/2 (65') alone but is covered once an
    # explicit, larger image_footprint radius (90') is supplied by the caller.
    record = _record("D", Category.NEBULA, ra_deg=10.0, dec_deg=80.0 / 60.0, extent_major_arcmin=10.0)
    rejected = kt.match_target([record], FRAME_CENTER, FOV_ARCMIN, None)
    assert rejected.status == "spatially_rejected"

    accepted = kt.match_target([record], FRAME_CENTER, FOV_ARCMIN, 90.0)
    assert accepted.status == "hit"


def test_classify_conflict_type_disagreement():
    plausible_galaxy = _record("E", Category.GALAXY, ra_deg=10.0, dec_deg=0.0, extent_major_arcmin=20.0)
    assert kt.classify_conflict(plausible_galaxy, Category.NEBULA) == "type_disagreement"


def test_classify_conflict_spatially_impossible():
    # a record whose own stored declination is outside the physically valid range
    # (-90..90 deg) cannot correspond to any real position on the sky -- impossible
    # independent of any particular frame.
    impossible = _record("F", Category.GALAXY, ra_deg=10.0, dec_deg=95.0, extent_major_arcmin=20.0)
    assert kt.classify_conflict(impossible, Category.GALAXY) == "spatially_impossible"


def test_classify_conflict_none_when_plausible_and_agreeing():
    agreeing = _record("G", Category.STARFIELD, ra_deg=10.0, dec_deg=0.0, extent_major_arcmin=5.0)
    assert kt.classify_conflict(agreeing, Category.STARFIELD) == "none"


# --- lookup_role candidacy (explicit schema, not verb/extent inference) ----------


def test_match_target_associated_only_record_alone_is_miss_not_hit():
    """An `associated_only` record (e.g. M8's embedded cluster NGC6530) overlapping
    the frame with NO primary present must never independently produce a hit --
    it's relational metadata, not a top-level lookup target."""
    embedded = _record(
        "NGC6530", Category.STARFIELD, ra_deg=10.02, dec_deg=0.0, extent_major_arcmin=6.0, lookup_role="associated_only"
    )
    result = kt.match_target([embedded], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "miss"
    assert result.record is None


def test_match_target_associated_only_excluded_when_primary_also_overlaps():
    """The M8/NGC6530 regression case in miniature: a primary (M8-like) and its
    embedded, overlapping `associated_only` cluster both spatially overlap the
    frame -- the associated_only record must be filtered out of candidacy BEFORE
    the ambiguity check, so the primary still resolves to a clean "hit", not
    "ambiguous"."""
    primary = _record("M8", Category.NEBULA, ra_deg=10.0, dec_deg=0.0, extent_major_arcmin=45.0, lookup_role="primary")
    embedded = _record(
        "NGC6530",
        Category.STARFIELD,
        ra_deg=10.03,
        dec_deg=0.0,
        extent_major_arcmin=6.0,
        lookup_role="associated_only",
    )
    result = kt.match_target([primary, embedded], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "hit"
    assert result.record is primary


def test_match_target_two_overlapping_primaries_still_ambiguous():
    """Control: lookup_role filtering must not touch the genuine multi-primary
    ambiguity case -- two different intended top-level targets overlapping the same
    frame is still "ambiguous", unchanged."""
    a = _record("A", Category.NEBULA, ra_deg=10.0, dec_deg=0.05, extent_major_arcmin=90.0, lookup_role="primary")
    b = _record("B", Category.NEBULA, ra_deg=10.02, dec_deg=-0.03, extent_major_arcmin=60.0, lookup_role="primary")
    result = kt.match_target([a, b], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "ambiguous"
    assert result.record is None


def test_match_target_only_associated_only_records_present_is_miss():
    """`records` non-empty but every entry is associated_only -> still "miss", not
    "spatially_rejected" (there is no matchable candidate at all, per the spec)."""
    a = _record(
        "X", Category.STARFIELD, ra_deg=10.0, dec_deg=0.0, extent_major_arcmin=5.0, lookup_role="associated_only"
    )
    b = _record(
        "Y", Category.STARFIELD, ra_deg=50.0, dec_deg=20.0, extent_major_arcmin=5.0, lookup_role="associated_only"
    )
    result = kt.match_target([a, b], FRAME_CENTER, FOV_ARCMIN, None)
    assert result.status == "miss"
    assert result.record is None


def test_load_catalog_parses_lookup_role_default_and_explicit(tmp_path, monkeypatch):
    """Loader default: a record missing `lookup_role` in the JSON must default to
    "primary" (never raise -- load-time is lenient, spec sec5.7). An explicit
    "associated_only" value must parse through unchanged."""
    payload = {
        "schema_version": 1,
        "provenance": {},
        "records": [
            {
                "entry_id": "a",
                "canonical_id": "A",
                "ra_deg": 10.0,
                "dec_deg": 0.0,
                "classification": {"family": "nebula"},
                # lookup_role deliberately omitted
            },
            {
                "entry_id": "b",
                "canonical_id": "B",
                "ra_deg": 11.0,
                "dec_deg": 0.0,
                "classification": {"family": "starfield"},
                "lookup_role": "associated_only",
            },
        ],
    }
    text = kt.json.dumps(payload)
    monkeypatch.setattr(kt, "_read_catalog_text", lambda name: text)
    recs = kt.load_catalog()
    by_id = {r.canonical_id: r for r in recs}
    assert by_id["A"].lookup_role == "primary"
    assert by_id["B"].lookup_role == "associated_only"


def test_real_shipped_catalog_m8_hit_not_ambiguous():
    """The regression this task fixes: loading the REAL shipped catalog
    (seestar/data/known_targets.json) and matching an M8-centered S30 frame must
    resolve to a clean "hit" on M8 -- not "ambiguous" against its own embedded
    cluster NGC6530 (no prior test covered this end-to-end through the real
    catalog + the real loader)."""
    records = kt.load_catalog()
    assert records, "the real shipped catalog must load"

    # M8 (Lagoon Nebula) coords; a realistic S30 long-axis FOV (~130').
    frame_center = (270.9, -24.38)
    fov_arcmin = 130.0
    result = kt.match_target(records, frame_center, fov_arcmin, None)
    assert result.status == "hit"
    assert result.record is not None
    assert result.record.canonical_id == "M8"
