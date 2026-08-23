"""Content-first ingestion (spec 2026-07-13 §4): label sanitization, header
identity, header-first metadata. Real synthetic FITS, no mocks (repo mock guide)."""

from __future__ import annotations

import os

import pytest

from seestar.inspect import is_seestar_stack, sanitize_label
from seestar.run import discover_targets


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("M 8", "M8"),  # firmware spacing
        ("NGC 5139", "NGC5139"),  # non-Messier
        ("M8 (Lagoon Nebula)", "M8"),  # trailing parenthetical stripped
        ("M8", "M8"),  # case PRESERVED (no lowercasing)
        ('A/B\\C:D*E?F"G<H>I|J', "ABCDEFGHIJ"),  # filesystem-unsafe chars removed
        ("  ", None),  # whitespace-only -> None
        (None, None),
        ("(all parens)", None),  # nothing left -> None
    ],
)
def test_sanitize_label(raw, expected):
    assert sanitize_label(raw) == expected


def _hdr(**over):
    base = {"CREATOR": "ZWO Seestar S30", "NAXIS3": 3, "STACKCNT": 51}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def test_is_seestar_stack_accepts_s30_and_pro():
    assert is_seestar_stack(_hdr()) == (True, None)
    assert is_seestar_stack(_hdr(CREATOR="ZWO Seestar S30 Pro")) == (True, None)


def test_is_seestar_stack_rejects_non_seestar():
    ok, reason = is_seestar_stack(_hdr(CREATOR="MaxIm DL"))
    assert not ok and "Seestar" in reason


def test_is_seestar_stack_rejects_2d_sub():
    ok, reason = is_seestar_stack(_hdr(NAXIS3=None))
    assert not ok and "3-channel" in reason


def test_is_seestar_stack_rejects_unstacked():
    ok, reason = is_seestar_stack(_hdr(STACKCNT=None))
    assert not ok and "STACKCNT" in reason


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.mark.parametrize(
    "name, creator_contains",
    [
        ("M8_S30_LP_nebula.fit", "Seestar S30"),
        ("M81_S30Pro_IRCUT_galaxy.fit", "S30 Pro"),
        ("NGC5139_S30_IRCUT_starfield.fit", "Seestar S30"),
    ],
)
def test_real_fixtures_pass_identity_check(name, creator_contains):
    from seestar.inspect import is_seestar_stack, read_header

    hdr = read_header(os.path.join(FIXTURES, name))
    assert is_seestar_stack(hdr) == (True, None)
    assert creator_contains in hdr["CREATOR"]


def test_file_meta_header_first(tmp_path, fake_stack):
    from seestar.inspect import file_meta

    p = fake_stack(tmp_path / "renamed_anything.fits", obj="M 81", filt="IRCUT", stackcnt=60)
    m = file_meta(p)
    assert m["target"] == "M81"  # OBJECT, sanitized — not the filename
    assert m["frames"] == 60  # STACKCNT
    assert m["filter"] == "IRCUT"
    assert m["sub_exposure_s"] == 10.0  # EXPOSURE
    assert m["datetime"] == "2026-06-16 08:13:23"  # DATE-OBS, seconds precision


def test_file_meta_filename_fallback_per_field(tmp_path, fake_stack):
    from seestar.inspect import file_meta

    # OBJECT and FILTER missing from the header -> those two fall back to the
    # filename; header still wins for the fields it has.
    name = "Stacked_51_M 8_10.0s_LP_20260615-221349.fit"
    p = fake_stack(tmp_path / name, obj=None, filt=None, stackcnt=99)
    m = file_meta(p)
    assert m["target"] == "M8"  # from filename
    assert m["filter"] == "LP"  # from filename
    assert m["frames"] == 99  # header still wins where present


def test_file_meta_real_fixture():
    from seestar.inspect import file_meta

    m = file_meta(os.path.join(FIXTURES, "M81_S30Pro_IRCUT_galaxy.fit"))
    assert m == {
        "target": "M81",
        "frames": 60,
        "filter": "IRCUT",
        "sub_exposure_s": 10.0,
        "datetime": "2026-07-04 04:04:28",
    }


def test_discovery_finds_renamed_stack(tmp_path, fake_stack):
    d = tmp_path / "lib" / "somewhere"
    d.mkdir(parents=True)
    fake_stack(d / "totally_renamed.fits", obj="M 81")
    out = discover_targets(str(tmp_path / "lib"))
    assert [t["target"] for t in out["targets"]] == ["M81"]
    assert out["skipped"] == []


def test_discovery_reports_non_stack_fits(tmp_path, fake_stack):
    d = tmp_path / "t"
    d.mkdir()
    fake_stack(d / "good.fit", obj="M 8")
    fake_stack(d / "sub.fit", shape=(64, 64))  # 2-D raw sub -> skipped
    fake_stack(d / "alien.fits", creator="MaxIm DL")  # non-Seestar -> skipped
    (d / "note.txt").write_text("not fits")  # ignored entirely
    out = discover_targets(str(d))
    assert len(out["targets"]) == 1 and out["targets"][0]["target"] == "M8"
    reasons = {os.path.basename(s["path"]): s["reason"] for s in out["skipped"]}
    assert "3-channel" in reasons["sub.fit"]
    assert "Seestar" in reasons["alien.fits"]


def test_discovery_latest_session_by_date_obs(tmp_path, fake_stack):
    d = tmp_path / "m13"
    d.mkdir()
    fake_stack(d / "a.fit", obj="M 13", date_obs="2025-08-01T02:00:00")
    fake_stack(d / "b.fit", obj="M 13", date_obs="2026-07-01T02:00:00")
    out = discover_targets(str(d))
    t = out["targets"][0]
    assert t["sessions"] == 2
    assert t["latest"] == "b.fit"


def test_old_raw_layout_names_target_from_header(tmp_path, fake_stack):
    # The Google-Drive library uses the OLD M8/raw/ layout: pointed at M8/, the
    # target folder is "raw" — the OBJECT header must name it, not the folder.
    d = tmp_path / "M8" / "raw"
    d.mkdir(parents=True)
    fake_stack(d / "Stacked_51_M 8_10.0s_LP_20260615-221349.fit", obj="M 8")
    out = discover_targets(str(tmp_path / "M8"))
    assert out["targets"][0]["target"] == "M8"


def test_discovery_falls_back_to_folder_name_when_object_and_filename_unresolved(tmp_path, fake_stack):
    # No OBJECT header and a filename that doesn't match the Stacked_* pattern -> the
    # folder name is the last resort (spec §4: OBJECT, then filename, then folder).
    d = tmp_path / "X99"
    d.mkdir()
    fake_stack(d / "not_a_parseable_name.fit", obj=None)
    out = discover_targets(str(tmp_path / "X99"))
    assert out["targets"][0]["target"] == "X99"


def test_discovery_folder_name_fallback_is_sanitized(tmp_path, fake_stack):
    # Same as above, but the folder name itself needs sanitizing (firmware-style spacing).
    d = tmp_path / "X 99"
    d.mkdir()
    fake_stack(d / "not_a_parseable_name.fit", obj=None)
    out = discover_targets(str(tmp_path / "X 99"))
    assert out["targets"][0]["target"] == "X99"
