"""Tests for the WCS catalog prior. All network-free: the SIMBAD CSV fixtures are
captured verbatim from real cone searches at the targets' field centers."""

import numpy as np
import pytest
from astropy.io import fits

from seestar import prior as PR

# Real SIMBAD TAP CSV responses (TOP rows) captured during the build de-risk.
CSV_NGC5128 = (  # galaxy: nearest hit is the BLL blazar nucleus, NOT a galaxy code
    "main_id,otype,otype_txt,ra,dec,galdim_majaxis,galdim_minaxis,dist\n"
    "NAME Centaurus A,BLL,BLL,201.36506,-43.01911,25.7,17.78,2.9e-5\n"
    "NAME Cen A Core,PoG,PoG,201.36501,-43.01930,,,2.1e-4\n"
    "JCMTSF J132527.7-430105,X,X,201.36616,-43.01844,,,1.0e-3\n"
    "1RXS J132527.3-430105,X,X,201.36374,-43.01805,,,1.4e-3\n"
)
CSV_M5 = (  # globular: M5 GlC first, then member stars
    "main_id,otype,otype_txt,ra,dec,galdim_majaxis,galdim_minaxis,dist\n"
    "M   5,GlC,GlC,229.63841,2.08102,17.4,17.4,3.2e-5\n"
    "Cl* NGC 5904 SB 2-399,RG*,RG*,229.63837,2.08052,,,4.7e-4\n"
)
CSV_M8 = (  # Lagoon: primary otype is its embedded OPEN CLUSTER, not the nebula
    "main_id,otype,otype_txt,ra,dec,galdim_majaxis,galdim_minaxis,dist\n"
    "M   8,OpC,OpC,270.90416,-24.38666,,,3.6e-4\n"
    "Cl* NGC 6530 SCB 113,*,*,270.90217,-24.38635,,,1.7e-3\n"
)


def test_classify_otype_families():
    assert PR.classify_otype("GlC") == "starfield"
    assert PR.classify_otype("OpC") == "starfield"
    assert PR.classify_otype("Cl*") == "starfield"
    assert PR.classify_otype("G") == "galaxy"
    assert PR.classify_otype("BLL") == "galaxy"  # active nucleus -> galaxy family
    assert PR.classify_otype("ClG") == "galaxy"  # cluster of GALAXIES, not stars
    assert PR.classify_otype("rG") == "galaxy"
    assert PR.classify_otype("HII") == "nebula"
    assert PR.classify_otype("PN") == "nebula"
    assert PR.classify_otype("*") is None  # a star is not a deep-sky object
    assert PR.classify_otype("X") is None  # X-ray source
    assert PR.classify_otype("RG*") is None  # cluster-member red giant


def test_classify_otype_txt_fallback():
    # Unknown code, but the text disambiguates.
    assert PR.classify_otype("ZZZ", "Galaxy in Cluster") == "galaxy"
    assert PR.classify_otype("ZZZ", "Globular Cluster") == "starfield"
    assert PR.classify_otype("ZZZ", "Emission Nebula") == "nebula"


def test_dominant_object_skips_pointsource_picks_galaxy():
    cand = PR.parse_simbad_csv(CSV_NGC5128)
    dom = PR.dominant_object(cand)
    assert dom["main_id"] == "NAME Centaurus A"
    assert dom["catalog_category"] == "galaxy"  # despite otype BLL
    assert dom["galdim_majaxis"] == 25.7


def test_dominant_object_globular():
    dom = PR.dominant_object(PR.parse_simbad_csv(CSV_M5))
    assert dom["main_id"].startswith("M")
    assert dom["catalog_category"] == "starfield"


def test_dominant_object_open_cluster():
    dom = PR.dominant_object(PR.parse_simbad_csv(CSV_M8))
    assert dom["catalog_category"] == "starfield"  # OpC -- the embedded cluster
    assert dom["otype"] == "OpC"


def test_dominant_object_none_when_only_stars():
    only_stars = (
        "main_id,otype,otype_txt,ra,dec,galdim_majaxis,galdim_minaxis,dist\n"
        "HD 1,*,*,10.0,10.0,,,1e-4\n"
        "HD 2,RG*,RG*,10.0,10.0,,,2e-4\n"
    )
    assert PR.dominant_object(PR.parse_simbad_csv(only_stars)) is None


# --- reconcile: authority order (filter law > catalog > measured) ----------------


def _prior(cat, obj="OBJ", ot="?"):
    return {"available": True, "source": "live", "catalog_category": cat, "object": obj, "otype": ot}


def test_reconcile_lp_filter_law_overrides_catalog():
    # M8: catalog says open cluster, but LP -> nebula stays. Disagreement recorded.
    cat, note = PR.reconcile("LP", "nebula", _prior("starfield", "M 8", "OpC"))
    assert cat == "nebula"
    assert note["used"] is False
    assert note["disagreement"] is True


def test_reconcile_ircut_catalog_sets_galaxy():
    # NGC5128: measured default starfield -> catalog flips to galaxy.
    cat, note = PR.reconcile("IRCUT", "starfield", _prior("galaxy", "Cen A", "BLL"))
    assert cat == "galaxy"
    assert note["used"] is True and note["changed"] is True


def test_reconcile_ircut_catalog_confirms_starfield():
    cat, note = PR.reconcile("IRCUT", "starfield", _prior("starfield", "M 5", "GlC"))
    assert cat == "starfield"
    assert note["used"] is True and note["changed"] is False


def test_reconcile_no_prior_keeps_measured():
    cat, note = PR.reconcile("IRCUT", "starfield", None)
    assert cat == "starfield" and note["used"] is False
    cat, note = PR.reconcile("IRCUT", "starfield", {"available": False, "source": "offline"})
    assert cat == "starfield" and note["used"] is False


def test_prior_for_offline_degrades(monkeypatch, tmp_path):
    # No WCS center -> unavailable, never raises.
    monkeypatch.setattr(PR, "field_center", lambda p: None)
    out = PR.prior_for("x.fit", {}, {"prior": {"cache_dir": str(tmp_path)}})
    assert out["available"] is False and out["source"] == "no-wcs"

    # WCS present but network down and no cache -> offline, still no raise.
    monkeypatch.setattr(PR, "field_center", lambda p: (201.39, -43.0))

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(PR, "query_simbad", boom)
    out = PR.prior_for("x.fit", {}, {"prior": {"cache_dir": str(tmp_path)}})
    assert out["available"] is False and out["source"] == "offline"


# --- P1 bug fix: _fov_long_arcmin_from_wcs -- derive FOV from the FULL WCS when the
# header's convenience `pixscale_arcsec` field is absent (e.g. a PC+CDELT header that
# `inspect.read_header` only reduces `pixscale_arcsec` for a CD1_1/CD1_2 matrix). -----


def _write_wcs_fits(
    path,
    ra_deg=150.0,
    dec_deg=-10.0,
    naxis1=64,
    naxis2=64,
    scale_deg=0.0011,
    convention="cd",  # "cd" | "pc" | "none"
    ctype2=True,
):
    """A real, minimal FITS with NAXIS1/2 + a celestial WCS, in one of three pixel-
    scale conventions astropy's WCS understands (CD matrix / PC+CDELT / none at
    all) -- so `_fov_long_arcmin_from_wcs` can be exercised against the real
    astropy.wcs machinery, not a mock."""
    data = np.zeros((3, naxis2, naxis1), dtype="uint16")
    h = fits.Header()
    h["NAXIS1"] = naxis1
    h["NAXIS2"] = naxis2
    h["CTYPE1"] = "RA---TAN"
    if ctype2:
        h["CTYPE2"] = "DEC--TAN"
    h["CRVAL1"] = ra_deg
    h["CRVAL2"] = dec_deg
    h["CRPIX1"] = (naxis1 + 1) / 2.0
    h["CRPIX2"] = (naxis2 + 1) / 2.0
    if convention == "cd":
        h["CD1_1"] = -scale_deg
        h["CD1_2"] = 0.0
        h["CD2_1"] = 0.0
        h["CD2_2"] = scale_deg
    elif convention == "pc":
        h["CDELT1"] = -scale_deg
        h["CDELT2"] = scale_deg
        h["PC1_1"] = 1.0
        h["PC1_2"] = 0.0
        h["PC2_1"] = 0.0
        h["PC2_2"] = 1.0
    # convention == "none": no CD/PC/CDELT keys at all.
    fits.PrimaryHDU(data=data, header=h).writeto(str(path), overwrite=True)
    return str(path)


def test_fov_long_arcmin_from_wcs_derives_scale_from_pc_cdelt_matrix(tmp_path):
    """The exact Bug-1 scenario: a header reduced to a PC+CDELT convention has NO
    `pixscale_arcsec` (only `inspect.read_header` special-cases CD1_1/CD1_2), but the
    full WCS still carries a usable pixel scale via `proj_plane_pixel_scales`."""
    path = _write_wcs_fits(tmp_path / "pc.fit", convention="pc", naxis1=64, naxis2=64, scale_deg=0.0011)
    fov = PR._fov_long_arcmin_from_wcs(path)
    assert fov is not None
    assert fov == pytest.approx(64 * 0.0011 * 3600.0 / 60.0, rel=1e-6)


def test_fov_long_arcmin_from_wcs_derives_scale_from_cd_matrix(tmp_path):
    """Sanity check against the existing CD-matrix convention (matches
    `_fov_long_arcmin`'s own header-derived value for the same header)."""
    path = _write_wcs_fits(tmp_path / "cd.fit", convention="cd", naxis1=64, naxis2=64, scale_deg=0.0011)
    fov = PR._fov_long_arcmin_from_wcs(path)
    assert fov is not None
    assert fov == pytest.approx(64 * 0.0011 * 3600.0 / 60.0, rel=1e-6)


def test_fov_long_arcmin_from_wcs_none_when_no_pixel_scale_convention_present(tmp_path):
    """A header with CTYPE/CRVAL (has_wcs is True) but no CD, PC, or CDELT at all is
    genuinely unusable for a pixel scale -- astropy's WCS silently defaults to a
    degenerate 1 deg/pixel identity transform rather than raising, so this must be
    detected explicitly rather than returning a wildly wrong FOV."""
    path = _write_wcs_fits(tmp_path / "noscale.fit", convention="none")
    assert PR._fov_long_arcmin_from_wcs(path) is None


def test_fov_long_arcmin_from_wcs_none_on_malformed_wcs(tmp_path):
    """CTYPE2 missing entirely (a malformed/incomplete plate solve) makes
    `WCS(hdr, naxis=2)` itself raise -- must degrade to None, never propagate."""
    path = _write_wcs_fits(tmp_path / "broken.fit", convention="cd", ctype2=False)
    assert PR._fov_long_arcmin_from_wcs(path) is None


def test_fov_long_arcmin_from_wcs_none_for_missing_file():
    assert PR._fov_long_arcmin_from_wcs("/nonexistent/path/does_not_exist.fit") is None
