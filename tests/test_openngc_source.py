from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    not (REPO_ROOT / "docs" / "RELEASE_READINESS.md").exists(),
    reason="docs/RELEASE_READINESS.md is maintainer-only and excluded from the public export",
)
def test_release_readiness_reports_catalog_expansion_in_progress():
    """While Plan A is in flight, RELEASE_READINESS must both flag the reopen AND
    carry the hard tag-blocking condition. Flips to a re-freeze assertion at Task 9."""
    text = (REPO_ROOT / "docs" / "RELEASE_READINESS.md").read_text(encoding="utf-8").lower()
    assert "reopen" in text, "must flag the reopened catalog freeze"
    assert "cannot be tagged until the expanded catalog" in text, "must carry the hard tag-block"


SRC_DIR = REPO_ROOT / "tools" / "data" / "openngc-v20260501"
EXPECTED_SCHEMA = [
    "Name",
    "Type",
    "RA",
    "Dec",
    "Const",
    "MajAx",
    "MinAx",
    "PosAng",
    "B-Mag",
    "V-Mag",
    "J-Mag",
    "H-Mag",
    "K-Mag",
    "SurfBr",
    "Hubble",
    "Pax",
    "Pm-RA",
    "Pm-Dec",
    "RadVel",
    "Redshift",
    "Cstar U-Mag",
    "Cstar B-Mag",
    "Cstar V-Mag",
    "M",
    "NGC",
    "IC",
    "Cstar Names",
    "Identifiers",
    "Common names",
    "NED notes",
    "OpenNGC notes",
    "Sources",
]


def _bkt():
    spec = importlib.util.spec_from_file_location("build_known_targets", REPO_ROOT / "tools" / "build_known_targets.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["build_known_targets"] = m
    spec.loader.exec_module(m)
    return m


def _manifest():
    return json.loads((SRC_DIR / "SOURCE.json").read_text(encoding="utf-8"))


def test_source_pin_identity():
    m = _manifest()
    assert m["tag"] == "v20260501"
    assert m["commit"] == "36cb178a0f69dba8bfc03a99c10512831edf1c6b"


@pytest.mark.parametrize("fname,rows", [("NGC.csv", 13969), ("addendum.csv", 64)])
def test_source_row_counts(fname, rows):
    n = sum(1 for _ in csv.DictReader(open(SRC_DIR / fname, encoding="utf-8"), delimiter=";"))
    assert n == rows and _manifest()["row_counts"][fname] == rows


@pytest.mark.parametrize("fname", ["NGC.csv", "addendum.csv"])
def test_source_schema_exact(fname):
    header = next(csv.reader(open(SRC_DIR / fname, encoding="utf-8"), delimiter=";"))
    assert header == EXPECTED_SCHEMA


def test_source_sha256_matches_manifest():
    for rel, expected in _manifest()["sha256"].items():
        assert hashlib.sha256((SRC_DIR / rel).read_bytes()).hexdigest() == expected


def test_loader_fails_closed_on_corrupt_source(tmp_path):
    """The BUILDER (not just this test) must reject a source whose bytes don't match
    the pinned sha256 — copy the tree, corrupt one file, expect BuildError."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    (dst / "NGC.csv").write_text("Name;Type\nNGC1;G\n", encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="integrity failure"):
        bkt.load_openngc_source(str(dst))


def test_loader_merges_and_counts():
    bkt = _bkt()
    version, index = bkt.load_openngc_source(str(SRC_DIR))
    assert version == "v20260501"
    assert len(index) == 13969 + 64


# --- strengthened source-pin enforcement (COMPLETE reviewed set at BUILD time) ----


def test_license_blob_id_recorded_and_matches():
    """The CC-BY-SA license is now blob-anchored: SOURCE.json carries its git blob
    id, and the vendored file hashes to it (pure-python git blob sha1)."""
    bkt = _bkt()
    blob = _manifest()["git_blob_ids"]
    rel = "LICENSES/CC-BY-SA-4.0.txt"
    assert rel in blob
    data = (SRC_DIR / rel).read_bytes()
    assert bkt._git_blob_sha1(data) == blob[rel]


def test_loader_fails_closed_on_tampered_license(tmp_path):
    """The license file is part of the COMPLETE reviewed set enforced at BUILD time
    (blob + sha). Tamper its bytes and the loader must raise BuildError. (The two
    tests below cover the NEW blob-completeness enforcement specifically: a missing
    blob id, and a mismatched blob id whose sha256 would still pass.)"""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    (dst / "LICENSES" / "CC-BY-SA-4.0.txt").write_text("not the license\n", encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="integrity failure"):
        bkt.load_openngc_source(str(dst))


def test_loader_fails_closed_on_missing_blob_id(tmp_path):
    """Non-vacuous: a file pinned by sha256 but MISSING its git blob id is an
    incomplete pin and must fail closed (the COMPLETE reviewed set is enforced)."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    del src["git_blob_ids"]["NGC.csv"]
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="incomplete"):
        bkt.load_openngc_source(str(dst))


def test_loader_fails_closed_on_wrong_blob_id(tmp_path):
    """Non-vacuous: a recorded git blob id that doesn't match the file's actual blob
    (even when sha256 would pass) fails closed. Corrupt only the pinned blob id."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    src["git_blob_ids"]["NGC.csv"] = "0" * 40
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="integrity failure"):
        bkt.load_openngc_source(str(dst))


# --- source-pin is enforced against an INDEPENDENT constant, not SOURCE.json ------
# These three attacks all keep SOURCE.json internally consistent (the circular check
# passed them before the fix); each MUST now fail closed because verification is
# against `EXPECTED_OPENNGC_PIN`, never SOURCE.json.


def test_pin_rejects_deleted_license(tmp_path):
    """Attack (a): delete the license file AND scrub it from SOURCE.json's blob+sha
    maps. Under the old self-referential check the license was simply no longer in the
    reviewed union, so the build passed. The constant REQUIRES the license file to
    exist regardless of SOURCE.json -> BuildError."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    (dst / "LICENSES" / "CC-BY-SA-4.0.txt").unlink()
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    rel = "LICENSES/CC-BY-SA-4.0.txt"
    src["git_blob_ids"].pop(rel, None)
    src["sha256"].pop(rel, None)
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="pinned source file missing"):
        bkt.load_openngc_source(str(dst))


def test_pin_rejects_zeroed_commit(tmp_path):
    """Attack (b): replace the recorded commit with forty zeros. The old check only
    asserted the commit was truthy, so this passed. The constant pins the exact commit
    -> BuildError."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    src["commit"] = "0" * 40
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    bkt = _bkt()
    with pytest.raises(bkt.BuildError, match="commit"):
        bkt.load_openngc_source(str(dst))


def test_pin_rejects_bytes_changed_with_updated_source_json(tmp_path):
    """Attack (c): change a source file's bytes AND update BOTH its hashes in
    SOURCE.json so the file still agrees with SOURCE.json. The old self-referential
    check compared the file to SOURCE.json and passed. The constant is independent, so
    the file's live hashes no longer match it -> BuildError."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    bkt = _bkt()
    rel = "NGC_guide.txt"
    tampered = b"tampered guide bytes\n"
    (dst / rel).write_bytes(tampered)
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    src["sha256"][rel] = hashlib.sha256(tampered).hexdigest()
    src["git_blob_ids"][rel] = bkt._git_blob_sha1(tampered)
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    with pytest.raises(bkt.BuildError, match="integrity failure"):
        bkt.load_openngc_source(str(dst))


def test_pin_rejects_extra_source_json_entry(tmp_path):
    """Attack (d): add an EXTRA file to the source dir AND register it in SOURCE.json's
    blob + sha256 maps with valid self-declared hashes. All four pinned files still
    verify, but the exact-four-file-set contract forbids out-of-contract entries, so the
    build must fail closed (else a rogue file could ride along in the vendored tree)."""
    import shutil

    dst = tmp_path / "openngc-v20260501"
    shutil.copytree(SRC_DIR, dst)
    bkt = _bkt()
    extra = b"not part of the pinned OpenNGC release\n"
    (dst / "EXTRA.txt").write_bytes(extra)
    src = json.loads((dst / "SOURCE.json").read_text(encoding="utf-8"))
    src["sha256"]["EXTRA.txt"] = hashlib.sha256(extra).hexdigest()
    src["git_blob_ids"]["EXTRA.txt"] = bkt._git_blob_sha1(extra)
    (dst / "SOURCE.json").write_text(json.dumps(src), encoding="utf-8")
    with pytest.raises(bkt.BuildError, match="out-of-contract"):
        bkt.load_openngc_source(str(dst))
