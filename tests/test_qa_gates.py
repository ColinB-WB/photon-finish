"""Phase C: category-aware absolute QA gates. The 6 passing finals clear every threshold;
a real regression (collapsed star count, bloated stars, blown core, lost faint structure)
HARD-FAILS. Gates are catastrophe catches -- an unknown category, or a missing metric
whose threshold is not configured, is skipped, never a false fail. The exception (M2,
2026-07-31): when `star_preservation`/`fwhm_ceiling` IS active but a required
`findstar` measurement is unavailable (`None`), the gate FAILS closed instead of
skipping -- a hard gate that silently skips because the detector fell over is not a gate.
Thresholds are locked at the S30 reference scale and SCALE with the capture pixel scale /
FOV area (S30 Pro support, 2026-07)."""

import numpy as np
from PIL import Image

from seestar.core.types import FaintEvidence
from seestar.io import load_config
from seestar.qa import (
    absolute_gates,
    apply_fwhm_attribution,
    attribute_fwhm,
    category_from_subject,
    faint_structure_from_render,
    gate_scale_factors,
    pixscale_from_subject,
    profile_from_subject,
    read_run_notes,
)

# Mirrors config.yaml qa.gates (Phase C).
GATES = {
    "enabled": True,
    "reference": {"pixscale_arcsec": 3.99, "dims": [1080, 1920]},
    "star_preservation": {"min_retention": 0.5, "catastrophe_floor": 20},
    "fwhm_ceiling": {"nebula": 3.3, "starfield": 3.6, "galaxy": 3.5},
    "core_sat_ceiling": {"nebula": 0.01, "starfield": 0.01, "galaxy": 0.01},
    "faint_structure": {"retention_min": 0.3, "catastrophe_floor": 0.002},
    "sky_median_ceiling": {"nebula": 25, "starfield": 25, "galaxy": 25},
}

_FAINT_OK = FaintEvidence(retention=0.8, output_frac=0.05, support_state="valid")

# The S30 Pro capture geometry (M81, 2026-07): ~same sampling, ~3.5x the sky area.
PRO_PIXSCALE = 3.74
PRO_DIMS = [2096, 3726]  # rendered dims after the border crop

# The actual session-7 finals -- every one must pass. Fourth element = measured
# INPUT star count (real characterization values where a run exists: M8 1390,
# M5 1121; healthy-retention synthetics elsewhere).
FINALS = [
    ("nebula", {"star_count": 1055, "fwhm": 2.30, "core_sat_frac": 0.0}, _FAINT_OK, 1390),
    ("nebula", {"star_count": 1328, "fwhm": 2.33, "core_sat_frac": 0.0}, _FAINT_OK, 1770),
    ("starfield", {"star_count": 734, "fwhm": 2.48, "core_sat_frac": 3e-5}, None, 1121),
    ("starfield", {"star_count": 810, "fwhm": 2.59, "core_sat_frac": 0.0}, None, 1080),
    ("starfield", {"star_count": 1546, "fwhm": 2.43, "core_sat_frac": 0.0}, None, 2060),
    ("galaxy", {"star_count": 1417, "fwhm": 2.52, "core_sat_frac": 0.0}, _FAINT_OK, 1890),
]


def test_all_six_finals_pass():
    for cat, m, faint, input_stars in FINALS:
        assert absolute_gates(m, faint, cat, GATES, input_star_count=input_stars) == [], f"{cat} {m} should pass"


def test_overcook_gates_fail_the_m8_regression_and_pass_healthy():
    """Ported from tests/test_profiles.py (Task 23: unrelated to policy.py/profiles.py,
    this only exercises qa.absolute_gates -- misplaced there). Uses the REAL config
    (not the GATES mirror above, which predates the near_black/pegged_chroma gates)."""
    gates = load_config()["qa"]["gates"]
    # the measured overcooked M8 showcase final (2026-07 regression)
    overcooked = {
        "star_count": 615,
        "fwhm": 2.6,
        "core_sat_frac": 0.0,
        "near_black_frac_outside_mask": 0.0458,
        "pegged_chroma_frac_body": 0.4726,
    }
    fails = absolute_gates(overcooked, _FAINT_OK, "nebula", gates, input_star_count=900)
    assert any("background crushed" in f for f in fails)
    assert any("neon color" in f for f in fails)
    # the measured healthy pre-GHS state of the same data: passes untouched
    healthy = {
        "star_count": 1475,
        "fwhm": 2.5,
        "core_sat_frac": 0.0003,
        "near_black_frac_outside_mask": 0.0001,
        "pegged_chroma_frac_body": 0.0,
    }
    assert absolute_gates(healthy, _FAINT_OK, "nebula", gates, input_star_count=1390) == []


def test_overcook_gates_clear_all_passing_finals():
    """Ported from tests/test_profiles.py (Task 23)."""
    gates = load_config()["qa"]["gates"]
    # measured on the session-7 passing finals (M5/M13 starfield, NGC5128 galaxy)
    finals = [
        ("starfield", 0.0, 0.0117),
        ("starfield", 0.0, 0.0348),
        ("galaxy", 0.0, 0.0782),
    ]
    for cat, near_black, pegged in finals:
        m = {
            "star_count": 1000,
            "fwhm": 2.5,
            "core_sat_frac": 0.0,
            "near_black_frac_outside_mask": near_black,
            "pegged_chroma_frac_body": pegged,
        }
        assert absolute_gates(m, None, cat, gates, input_star_count=1400) == [], f"{cat} healthy final must pass"


def test_destructive_star_loss_fires():
    # SOURCE-RELATIVE (Diagnosis 2): the M8 black-point catastrophe measured
    # 916 stars in and 26 out -- retention 0.028 fails the cross-category ratio
    fails = absolute_gates(
        {"star_count": 26, "fwhm": 2.5, "core_sat_frac": 0.0}, _FAINT_OK, "nebula", GATES, input_star_count=916
    )
    assert any("star_preservation" in f for f in fails)


def test_sparse_source_passes_source_relative():
    # the S50 M82 evidence run: 294 in, 216 out (retention 0.735) must PASS --
    # the old absolute galaxy floor (600) could never be met by this source
    fails = absolute_gates(
        {"star_count": 216, "fwhm": 2.53, "core_sat_frac": 0.0}, _FAINT_OK, "galaxy", GATES, input_star_count=294
    )
    assert fails == []


def test_bloated_fwhm_fires():
    fails = absolute_gates(
        {"star_count": 800, "fwhm": 4.2, "core_sat_frac": 0.0}, None, "starfield", GATES, input_star_count=1080
    )
    assert any("fwhm" in f for f in fails)


def test_blown_core_fires():
    fails = absolute_gates(
        {"star_count": 1400, "fwhm": 2.5, "core_sat_frac": 0.05}, _FAINT_OK, "galaxy", GATES, input_star_count=1890
    )
    assert any("core_sat" in f for f in fails)


def test_lost_faint_structure_fires_source_relative():
    # extended target that crushed its measured structure -> retention fail
    crushed = FaintEvidence(retention=0.1, output_frac=0.02, support_state="valid")
    fails = absolute_gates(
        {"star_count": 1100, "fwhm": 2.3, "core_sat_frac": 0.0}, crushed, "nebula", GATES, input_star_count=1470
    )
    assert any("faint_structure retained" in f for f in fails)
    # star field has no faint metric (None) -> faint gate skipped, not a false fail
    assert (
        absolute_gates(
            {"star_count": 700, "fwhm": 2.5, "core_sat_frac": 0.0}, None, "starfield", GATES, input_star_count=1000
        )
        == []
    )


def test_star_preservation_and_fwhm_fail_closed_when_unavailable_through_absolute_gates():
    """M2: the deprecated dict-in/list-out `absolute_gates` wrapper must surface the
    same fail-closed policy as `core.qa_rules.evaluate_gates` -- an active threshold
    with a missing (`None`) measurement is a FAIL, not a silent skip. This covers
    all three: missing output count, missing INPUT count (Diagnosis 2), missing fwhm."""
    m = {"star_count": None, "fwhm": 2.5, "core_sat_frac": 0.0}
    fails = absolute_gates(m, _FAINT_OK, "nebula", GATES, input_star_count=1390)
    assert any(f.startswith("star_preservation unavailable") for f in fails), fails

    m_in = {"star_count": 800, "fwhm": 2.5, "core_sat_frac": 0.0}
    fails_in = absolute_gates(m_in, _FAINT_OK, "nebula", GATES, input_star_count=None)
    assert any(f.startswith("star_preservation unavailable") and "input" in f for f in fails_in), fails_in

    m2 = {"star_count": 600, "fwhm": None, "core_sat_frac": 0.0}
    fails2 = absolute_gates(m2, _FAINT_OK, "nebula", GATES, input_star_count=800)
    assert any(f.startswith("fwhm unavailable") for f in fails2), fails2


def test_unknown_category_and_disabled_skip():
    m = {"star_count": 10, "fwhm": 9.9, "core_sat_frac": 0.9}
    assert absolute_gates(m, 0.0, None, GATES) == []  # category unknown
    assert absolute_gates(m, 0.0, "nebula", {"enabled": False}) == []  # gates off


def test_gate_scales_identity_at_reference():
    # S30 render (or no pixscale at all) -> thresholds apply exactly as configured
    assert gate_scale_factors(GATES, 3.99, [1080, 1920]) == {"fwhm": 1.0, "area": 1.0}
    assert gate_scale_factors(GATES, None, [2096, 3726]) == {"fwhm": 1.0, "area": 1.0}
    assert gate_scale_factors({}, 3.74, PRO_DIMS) == {"fwhm": 1.0, "area": 1.0}  # no reference cfg


def test_gate_scales_s30_pro():
    s = gate_scale_factors(GATES, PRO_PIXSCALE, PRO_DIMS)
    # finer pixel scale -> the same seeing spans MORE px, so the fwhm ceiling rises
    assert 1.05 < s["fwhm"] < 1.09
    # ~3.5x the sky area -> a fixed-size object is a ~3.3x SMALLER frame fraction
    assert 0.25 < s["area"] < 0.35


def test_faint_gate_is_fov_independent():
    """Diagnosis 3: the faint gate is source-relative -- the same healthy retention
    passes at any frame geometry (no inverse-FOV scaling to false-alarm). Historical
    context: the M81 S30 Pro render measured faint_structure 0.0162 -- a hard fail against the
    raw S30-locked floor (0.025) purely because the Pro frames ~3.5x more sky. Scaled by
    FOV area the floor drops to ~0.0076 and the same measurement passes."""
    m = {"star_count": 1897, "fwhm": 2.5, "core_sat_frac": 0.0, "dimensions": PRO_DIMS}
    assert absolute_gates(m, _FAINT_OK, "galaxy", GATES, input_star_count=2500) == []
    assert absolute_gates(m, _FAINT_OK, "galaxy", GATES, pixscale=PRO_PIXSCALE, input_star_count=2500) == []


def test_s30_pro_fwhm_ceiling_scales_but_still_gates():
    """The fwhm ceiling rises by the pixel-scale ratio (3.5 -> ~3.73 px for the Pro),
    and a genuinely bloated render still fails."""
    m = {"star_count": 1897, "core_sat_frac": 0.0, "dimensions": PRO_DIMS, "fwhm": 3.6}
    assert absolute_gates(m, None, "galaxy", GATES, input_star_count=2500) != []  # unscaled: false alarm
    assert absolute_gates(m, None, "galaxy", GATES, pixscale=PRO_PIXSCALE, input_star_count=2500) == []
    bloated = {**m, "fwhm": 4.5}
    fails = absolute_gates(bloated, None, "galaxy", GATES, pixscale=PRO_PIXSCALE, input_star_count=2500)
    assert any("fwhm" in f and "scaled" in f for f in fails)


def test_subject_parsers():
    subj = "RA 149.4417, Dec 68.9425, 3.74 arcsec/px, M81, galaxy, IRCUT, ZWO Seestar S30 Pro, profile:showcase"
    assert pixscale_from_subject(subj) == 3.74
    assert category_from_subject(subj) == "galaxy"
    assert profile_from_subject(subj) == "showcase"
    assert pixscale_from_subject("M81, galaxy") is None
    assert category_from_subject("nebula galaxy soup") is None  # ambiguous -> None
    assert profile_from_subject("M81, galaxy") is None


def test_gray_wash_gate():
    """2026-07 M81 +PCC regression: sky rendered at 40/255 (gray wash) yet no gate
    fired -- near_black only catches the CRUSHED direction. The passing finals
    measure sky 9-13; the washed M81 measured 40."""
    m = {"star_count": 1630, "fwhm": 2.5, "core_sat_frac": 0.0, "background_median_outside_mask": 39.96}
    fails = absolute_gates(m, _FAINT_OK, "galaxy", GATES, input_star_count=2200)
    assert any("gray wash" in f for f in fails)
    good = {**m, "background_median_outside_mask": 13.2}
    assert absolute_gates(good, _FAINT_OK, "galaxy", GATES, input_star_count=2200) == []
    # documentation's full-DR promise legitimately brightens the sky -> exempt
    assert absolute_gates(m, _FAINT_OK, "galaxy", GATES, profile="documentation", input_star_count=2200) == []


def test_attribute_fwhm():
    # gate not failing -> no attribution
    assert attribute_fwhm(3.5, 4.0, 3.81) is None
    assert attribute_fwhm(None, 4.0, 3.81) is None
    assert attribute_fwhm(4.2, 4.0, None) is None
    # failing + input already over the ceiling -> the data's fault
    assert attribute_fwhm(4.2, 3.9, 3.81) == "source-limited"
    # failing + input was under the ceiling -> the pipeline bloated the stars
    assert attribute_fwhm(4.2, 3.0, 3.81) == "pipeline-limited"
    # failing but no input measurement -> cannot attribute
    assert attribute_fwhm(4.2, None, 3.81) is None


def test_source_limited_fwhm_moves_to_warning():
    fails = ["fwhm 4.20 > galaxy ceiling 3.81 (ref 3.5 scaled for pixel scale)", "star_count 100 < galaxy floor 600"]
    kept, warning, attr = apply_fwhm_attribution(fails, 4.2, 3.9, 3.81)
    assert attr == "source-limited"
    assert kept == ["star_count 100 < galaxy floor 600"]  # only the fwhm fail moves
    assert warning and "SOURCE-LIMITED" in warning and "3.90" in warning


def test_pipeline_limited_fwhm_stays_hard():
    fails = ["fwhm 4.20 > galaxy ceiling 3.81 (ref 3.5 scaled for pixel scale)"]
    kept, warning, attr = apply_fwhm_attribution(fails, 4.2, 3.0, 3.81)
    assert attr == "pipeline-limited"
    assert kept == fails
    assert warning is None


def test_read_run_notes_missing_is_none(tmp_path):
    assert read_run_notes(str(tmp_path), "M81") is None  # old renders keep working
    (tmp_path / "M81_notes.json").write_text('{"target": "M81", "notes": {"a": 1}}')
    assert read_run_notes(str(tmp_path), "M81")["notes"]["a"] == 1
    (tmp_path / "BAD_notes.json").write_text("{not json")
    assert read_run_notes(str(tmp_path), "BAD") is None


def test_faint_structure_from_render(tmp_path):
    # a black frame with a bright blob -> a small fraction above bg+3sigma
    arr = np.zeros((200, 200, 3), dtype=np.uint8)
    arr[90:110, 90:110] = 255
    p = str(tmp_path / "starless.tif")
    Image.fromarray(arr).save(p)
    frac, warning = faint_structure_from_render(p)
    assert 0.0 < frac < 0.05
    assert warning is None
    # a MISSING starless raster is the normal, unremarkable case (a non-extended
    # target never has one) -- silent (None, None), no warning.
    assert faint_structure_from_render(str(tmp_path / "missing.tif")) == (None, None)


def test_faint_structure_from_render_warns_when_present_but_unreadable(tmp_path):
    """Fix round 1, item 3: a starless raster that EXISTS but cannot be decoded
    (a zero-byte/corrupt stub) is a materially different state from "no starless
    at all" -- measurement was attempted and failed, so it must surface a NAMED
    warning, not degrade silently the same way a missing file does."""
    p = tmp_path / "starless.tif"
    p.write_bytes(b"not a real tiff, just garbage bytes")
    frac, warning = faint_structure_from_render(str(p))
    assert frac is None
    assert warning is not None
    assert "unreadable" in warning
    assert "starless.tif" in warning
