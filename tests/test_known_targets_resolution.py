from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "tools" / "data" / "openngc-v20260501"


def _bkt():
    spec = importlib.util.spec_from_file_location("build_known_targets", REPO_ROOT / "tools" / "build_known_targets.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_known_targets"] = m
    spec.loader.exec_module(m)
    return m


bkt = _bkt()
VERSION, INDEX = bkt.load_openngc_source(str(SRC_DIR))


def test_messier_resolves_via_m_column():
    assert bkt.resolve_designation("M8", INDEX, {}) == bkt.normalize_id("NGC6523")


def test_caldwell_resolves_via_identifiers_token():
    assert bkt.resolve_designation("C77", INDEX, {}) == bkt.normalize_id("NGC5128")


def test_m102_abstention_returns_none():
    assert bkt.resolve_designation("M102", INDEX, {"M102": {"reason": "controversial"}}) is None


def test_undeclared_ambiguous_resolution_is_fatal():
    fake = {"ngc1": {"Name": "NGC1", "M": "999", "Type": "G"}, "ngc2": {"Name": "NGC2", "M": "999", "Type": "G"}}
    with pytest.raises(bkt.BuildError, match="ambiguous"):
        bkt.resolve_designation("M999", fake, {})


def test_dup_row_never_becomes_primary():
    fake = {"ngcx": {"Name": "NGCX", "M": "500", "Type": "Dup"}}
    with pytest.raises(bkt.BuildError, match="no primary"):
        bkt.resolve_designation("M500", fake, {})


def test_selection_all_expands_to_219_with_m102_abstained():
    curation = {
        "schema_version": 2,
        "selection": {"messier": "all", "caldwell": "all", "include": ["ngc6530"]},
        "resolution": {"abstain": {"M102": {"reason": "controversial"}}},
        "annotations": {},
    }
    selected, resolution = bkt.expand_selection(curation, INDEX)
    assert len(selected) == 219
    assert "M102" in resolution["abstain"]


def test_undeclared_collision_requires_abstention():
    curation = {
        "schema_version": 2,
        "selection": {"messier": "all", "caldwell": "all", "include": ["ngc6530"]},
        "resolution": {"abstain": {}},
        "annotations": {},
    }
    with pytest.raises(bkt.BuildError, match="M102"):
        bkt.expand_selection(curation, INDEX)


def test_unknown_top_level_and_selection_keys_rejected():
    for bad in (
        {"schema_version": 2, "selection": {}, "bogus": 1},
        {"schema_version": 2, "selection": {"messier": "all", "junk": 1}},
    ):
        with pytest.raises(bkt.BuildError, match="unknown"):
            bkt._validate_curation_shape(bad)


def test_canonical_id_policy_caldwell_only_keeps_ngc_name():
    curation = {
        "schema_version": 2,
        "selection": {"messier": "all", "caldwell": "all", "include": ["ngc6530"]},
        "resolution": {"abstain": {"M102": {"reason": "x"}}},
        "annotations": {},
    }
    payload = bkt.build_v2(curation, INDEX, VERSION, None)
    by_norm = {r["entry_id"]: r for r in payload["records"]}
    ngc7000 = by_norm[bkt.normalize_id("NGC7000")]
    assert ngc7000["canonical_id"] == "NGC7000"  # NOT "C20"
    assert "C20" in ngc7000["equivalent_ids"]


def test_build_v2_entry_ids_unique_and_deterministic():
    import copy

    curation = {
        "schema_version": 2,
        "selection": {"messier": "all", "caldwell": "all", "include": ["ngc6530"]},
        "resolution": {"abstain": {"M102": {"reason": "x"}}},
        "annotations": {},
    }
    p1 = bkt.build_v2(curation, INDEX, VERSION, None)
    ids = [r["entry_id"] for r in p1["records"]]
    assert len(ids) == len(set(ids)), "entry_id collision"
    shuffled = {k: INDEX[k] for k in sorted(INDEX, reverse=True)}
    p2 = bkt.build_v2(copy.deepcopy(curation), shuffled, VERSION, None)
    assert p1["provenance"]["content_hash"] == p2["provenance"]["content_hash"]
