from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

import seestar.known_targets as kt

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = str(REPO_ROOT / "tools" / "data" / "openngc-v20260501")
PROTOTYPE_HITS = {"M8", "NGC7000", "NGC5128", "NGC5139"}

# NOTE: with EMPTY annotations, NGC6530 would default to lookup_role="primary" and
# become a spatial candidate inside M8's footprint (an M8-centered frame would then
# return "ambiguous", or build_v2 would raise an orphaned-associated_only error). The
# fixture below carries the NGC6530 associated_only annotation + reciprocal M8
# association so NGC6530 is excluded from candidacy and the no-regression test is
# meaningful.
_V2_FIXTURE = {
    "schema_version": 2,
    "selection": {"messier": "all", "caldwell": "all", "include": ["ngc6530"]},
    "resolution": {"abstain": {"M102": {"reason": "controversial identification"}}},
    "annotations": {
        "ngc6523": {
            "canonical_id": "M8",
            "associated": [{"canonical_id": "NGC6530", "relationship": "embedded_cluster"}],
        },
        "ngc6530": {
            "lookup_role": "associated_only",
            "classification_override": {
                "family": "starfield",
                "reason": "OpenNGC Cl+N embedded open cluster; curated starfield",
            },
            "associated": [{"canonical_id": "M8", "relationship": "embedded_in"}],
        },
    },
}


def _review():
    spec = importlib.util.spec_from_file_location("kt_review", REPO_ROOT / "tools" / "known_targets_review.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["kt_review"] = m
    spec.loader.exec_module(m)
    return m


review = _review()
RECORDS = review.build_records(_V2_FIXTURE, SRC_DIR)


def test_fixture_population_and_ngc6530_associated_only():
    assert len(RECORDS) == 219, len(RECORDS)
    by_id = {r.canonical_id: r for r in RECORDS}
    assert by_id["NGC6530"].lookup_role == "associated_only"


def test_no_prototype_target_regresses_after_expansion():
    by_id = {r.canonical_id: r for r in RECORDS}
    for cid in PROTOTYPE_HITS:
        rec = by_id[cid]
        res = kt.match_target(RECORDS, (rec.ra_deg, rec.dec_deg), review.S30_FOV_ARCMIN, None)
        assert res.status == "hit" and res.record.canonical_id == cid, f"{cid} regressed to {res.status}"


def test_centered_frame_never_bare_miss():
    hist = review.ambiguity_histogram(RECORDS)
    assert hist.get("miss", 0) == 0, hist  # a primary centered on itself is always a candidate
    print("AMBIGUITY_HISTOGRAM", hist)


def test_ambiguous_detail_matches_histogram_count():
    hist = review.ambiguity_histogram(RECORDS)
    detail = review.ambiguous_detail(RECORDS)
    assert len(detail) == hist.get("ambiguous", 0), (len(detail), hist)
    for entry in detail:
        assert entry["canonical_id"]
        assert len(entry["candidates"]) >= 2, entry  # "ambiguous" requires >1 candidate
        assert all(c["canonical_id"] and c["category"] for c in entry["candidates"])
        assert entry["verdict"] in {"UNANIMOUS", "CONFLICTING"}
        assert entry["unanimous"] == (entry["verdict"] == "UNANIMOUS")


# Real, mounted reference frames (config.validation.targets_darwin). Each is expected
# to be an UNAMBIGUOUS single "hit" on its own canonical identity -- these are the
# exact frames the prototype was locked against (CLAUDE.md sec4's reference data set),
# so a strengthened assertion here is a strict regression gate, not just "no crash".
EXPECTED_REAL_FRAME_HITS = {"M8": "M8", "NGC5128": "NGC5128", "NGC7000": "NGC7000"}


def test_real_reference_frames_strict_hit_on_own_identity():
    import seestar.prior as prior
    from seestar.inspect import read_header
    from seestar.io import load_config

    cfg = load_config()
    targets = cfg.get("validation", {}).get("targets_darwin", {})
    tested = 0
    for name, expected_cid in EXPECTED_REAL_FRAME_HITS.items():
        d = targets.get(name)
        if not d or not Path(d).is_dir():
            continue
        fits = next(Path(d).glob("*.fit"), None) or next(Path(d).glob("*.fits"), None)
        if fits is None:
            continue
        header = read_header(str(fits))
        center = prior.field_center(str(fits))
        if center is None:
            continue
        fov = prior._fov_long_arcmin(header)
        res = kt.match_target(RECORDS, center, fov, None)
        assert res.status == "hit", f"{name}: expected hit, got {res.status}"
        assert res.record is not None and res.record.canonical_id == expected_cid, (
            f"{name}: expected hit on {expected_cid!r}, got {res.record.canonical_id if res.record else None!r}"
        )
        tested += 1
    if tested == 0:
        pytest.skip("no reference frames mounted (config.validation.targets_darwin)")


# ---------------------------------------------------------------------------
# FIX-3: drift guard for the committed, reproducible ambiguity report. Fully
# frame-independent (built from target_curation.yaml + the pinned OpenNGC source
# only) -- runs unconditionally, including on CI with no frames mounted at all.
# ---------------------------------------------------------------------------

CURATION_PATH = REPO_ROOT / "target_curation.yaml"
REPORT_PATH = REPO_ROOT / "validation" / "known_targets_ambiguity_impact.md"


@pytest.mark.skipif(
    not REPORT_PATH.exists(),
    reason="validation/ is maintainer-only and excluded from the public export",
)
def test_ambiguity_report_matches_committed():
    import yaml

    with open(CURATION_PATH, encoding="utf-8") as f:
        curation = yaml.safe_load(f)
    records = review.build_records(curation, SRC_DIR)
    expected = review.render_ambiguity_report(records)
    committed = REPORT_PATH.read_text(encoding="utf-8")
    assert committed == expected, (
        "validation/known_targets_ambiguity_impact.md is stale -- regenerate it from "
        "render_ambiguity_report(build_records(target_curation.yaml, tools/data/"
        "openngc-v20260501)) and commit the refreshed file"
    )
