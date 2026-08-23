"""Task 25: QA gates as pure typed rules. Every scenario here mirrors a case in
tests/test_qa_gates.py (the dict-level equivalence guard for `qa.absolute_gates`/
`qa.gate_scale_factors`, which now thinly wrap these typed functions) -- this file
is the typed-level source of truth for the same behavior."""

from seestar.core.qa_rules import (
    compute_star_retention,
    evaluate_gates,
    scale_factors,
    star_retention_warning,
)
from seestar.core.types import Category, FaintEvidence, GateThresholds, PerCategory, RenderMetrics, Verdict
from seestar.io import gate_thresholds_from_cfg, load_config


def _metrics(**kw) -> RenderMetrics:
    d = dict(
        dimensions=(0, 0),
        envelope_area_frac=0.0,
        body_p90=None,
        body_p95=None,
        body_rgb=None,
        background_median_outside_mask=None,
        near_black_frac_outside_mask=0.0,
        core_sat_frac=0.0,
        pegged_chroma_frac_body=0.0,
        body_saturation_median=0.0,
        core_saturation_median=0.0,
        core_value_median=0.0,
        star_saturation_median=0.0,
        global_black_frac=0.0,
        star_count=None,
        fwhm=None,
    )
    d.update(kw)
    return RenderMetrics(**d)


# Mirrors config.yaml qa.gates (Phase C) -- same fixture as test_qa_gates.py's GATES.
# star_preservation is SOURCE-RELATIVE (Diagnosis 2): calibration from
# docs/assessments/2026-08-13-cross-camera-validation-coverage.md.
GATES = {
    "enabled": True,
    "reference": {"pixscale_arcsec": 3.99, "dims": [1080, 1920]},
    "star_preservation": {"min_retention": 0.5, "catastrophe_floor": 20},
    "fwhm_ceiling": {"nebula": 3.3, "starfield": 3.6, "galaxy": 3.5},
    "core_sat_ceiling": {"nebula": 0.01, "starfield": 0.01, "galaxy": 0.01},
    "faint_structure": {"retention_min": 0.3, "catastrophe_floor": 0.002},
    "sky_median_ceiling": {"nebula": 25, "starfield": 25, "galaxy": 25},
}
THRESHOLDS = gate_thresholds_from_cfg(GATES)

# healthy source-relative faint evidence (Diagnosis 3): most of the measured
# support stays visible in the output starless
_FAINT_OK = FaintEvidence(retention=0.8, output_frac=0.05, support_state="valid")

PRO_PIXSCALE = 3.74
PRO_DIMS = (2096, 3726)

# (category, render metrics, faint fraction, measured INPUT star count) -- the
# inputs are the characterization values where a real run exists (M8 1390, M5
# 1121) and healthy-retention synthetics elsewhere.
FINALS = [
    (Category.NEBULA, {"star_count": 1055, "fwhm": 2.30, "core_sat_frac": 0.0}, _FAINT_OK, 1390),
    (Category.NEBULA, {"star_count": 1328, "fwhm": 2.33, "core_sat_frac": 0.0}, _FAINT_OK, 1770),
    (Category.STARFIELD, {"star_count": 734, "fwhm": 2.48, "core_sat_frac": 3e-5}, None, 1121),
    (Category.STARFIELD, {"star_count": 810, "fwhm": 2.59, "core_sat_frac": 0.0}, None, 1080),
    (Category.STARFIELD, {"star_count": 1546, "fwhm": 2.43, "core_sat_frac": 0.0}, None, 2060),
    (Category.GALAXY, {"star_count": 1417, "fwhm": 2.52, "core_sat_frac": 0.0}, _FAINT_OK, 1890),
]


def _fails(report):
    return [g.detail for g in report.gates if g.verdict is Verdict.FAIL]


def test_all_six_finals_pass():
    for cat, m, faint, input_stars in FINALS:
        report = evaluate_gates(_metrics(**m), faint, cat, THRESHOLDS, None, None, input_star_count=input_stars)
        assert _fails(report) == [], f"{cat} {m} should pass"
        assert report.passed is True


def test_overcook_gates_fail_the_m8_regression_and_pass_healthy():
    gates = load_config()["qa"]["gates"]
    th = gate_thresholds_from_cfg(gates)
    overcooked = _metrics(
        star_count=615,
        fwhm=2.6,
        core_sat_frac=0.0,
        near_black_frac_outside_mask=0.0458,
        pegged_chroma_frac_body=0.4726,
    )
    report = evaluate_gates(overcooked, _FAINT_OK, Category.NEBULA, th, None, None, input_star_count=900)
    fails = _fails(report)
    assert any("background crushed" in f for f in fails)
    assert any("neon color" in f for f in fails)
    assert report.passed is False

    healthy = _metrics(
        star_count=1475,
        fwhm=2.5,
        core_sat_frac=0.0003,
        near_black_frac_outside_mask=0.0001,
        pegged_chroma_frac_body=0.0,
    )
    report2 = evaluate_gates(healthy, _FAINT_OK, Category.NEBULA, th, None, None, input_star_count=1390)
    assert _fails(report2) == []
    assert report2.passed is True


def test_overcook_gates_clear_all_passing_finals():
    gates = load_config()["qa"]["gates"]
    th = gate_thresholds_from_cfg(gates)
    finals = [
        (Category.STARFIELD, 0.0, 0.0117),
        (Category.STARFIELD, 0.0, 0.0348),
        (Category.GALAXY, 0.0, 0.0782),
    ]
    for cat, near_black, pegged in finals:
        m = _metrics(
            star_count=1000,
            fwhm=2.5,
            core_sat_frac=0.0,
            near_black_frac_outside_mask=near_black,
            pegged_chroma_frac_body=pegged,
        )
        report = evaluate_gates(m, None, cat, th, None, None, input_star_count=1400)
        assert _fails(report) == [], f"{cat} healthy final must pass"


# ---------------------------------------------------------------------------
# star_preservation (Diagnosis 2): SOURCE-RELATIVE. Output is judged against the
# stars measured in THIS capture's linear master, never an absolute per-camera
# floor. Fail-closed when either required measurement is missing.
# ---------------------------------------------------------------------------


def _star_gate(report):
    return next(g for g in report.gates if g.gate == "star_preservation")


def test_destructive_star_loss_fires():
    # the real M8 black-point catastrophe: 916 measured in, 26 out (retention 0.028)
    m = _metrics(star_count=26, fwhm=2.5, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=916)
    g = _star_gate(report)
    assert g.verdict is Verdict.FAIL
    # the detail carries counts, ratio, and threshold -- the recorded evidence
    assert "26" in g.detail and "916" in g.detail and "0.5" in g.detail
    assert report.passed is False


def test_m82_sparse_source_passes_source_relative():
    # Diagnosis 2 regression: the S50 M82 evidence run (294 measured in, 216 out,
    # retention 0.735) must PASS -- an output is never failed for lacking stars
    # its source never contained (the old absolute galaxy floor demanded 600).
    m = _metrics(star_count=216, fwhm=2.53, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.GALAXY, THRESHOLDS, None, None, input_star_count=294)
    assert _star_gate(report).verdict is Verdict.PASS
    assert _fails(report) == []


def test_catastrophe_floor_fires_on_nearly_empty_render():
    m = _metrics(star_count=15, fwhm=2.5, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1000)
    g = _star_gate(report)
    assert g.verdict is Verdict.FAIL
    assert "15" in g.detail


def test_catastrophe_floor_never_exceeds_input():
    # input below the configured floor (20): the effective floor caps at the input,
    # so an output equal to its input can never fail the catastrophe arm
    m = _metrics(star_count=10, fwhm=2.5, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=10)
    assert _star_gate(report).verdict is Verdict.PASS


def test_missing_input_measurement_fails_closed():
    m = _metrics(star_count=800, fwhm=2.5, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=None)
    g = _star_gate(report)
    assert g.verdict is Verdict.FAIL
    assert "unavailable" in g.detail and "input" in g.detail
    assert report.passed is False


def test_missing_output_measurement_fails_closed():
    m = _metrics(star_count=None, fwhm=2.5, core_sat_frac=0.0, background_median_outside_mask=15.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1390)
    g = _star_gate(report)
    assert g.verdict is Verdict.FAIL
    assert "unavailable" in g.detail
    assert report.passed is False


def test_star_preservation_unconfigured_skips():
    gates_off = dict(GATES)
    gates_off.pop("star_preservation")
    th = gate_thresholds_from_cfg(gates_off)
    m = _metrics(star_count=None, fwhm=2.5, core_sat_frac=0.0)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, th, None, None, input_star_count=None)
    g = _star_gate(report)
    assert g.verdict is Verdict.SKIPPED
    assert "not configured" in g.detail


def test_bloated_fwhm_fires():
    m = _metrics(star_count=800, fwhm=4.2, core_sat_frac=0.0)
    report = evaluate_gates(m, None, Category.STARFIELD, THRESHOLDS, None, None, input_star_count=1080)
    assert any("fwhm" in f for f in _fails(report))


def test_blown_core_fires():
    m = _metrics(star_count=1400, fwhm=2.5, core_sat_frac=0.05)
    report = evaluate_gates(m, _FAINT_OK, Category.GALAXY, THRESHOLDS, None, None, input_star_count=1890)
    assert any("core_sat" in f for f in _fails(report))


def test_lost_faint_structure_fires_source_relative():
    # Diagnosis 3: the gate judges RETAINED supported structure vs the run's own
    # measured reference -- crushed structure (low retention) fails
    m = _metrics(star_count=1100, fwhm=2.3, core_sat_frac=0.0)
    crushed = FaintEvidence(retention=0.1, output_frac=0.02, support_state="valid")
    report = evaluate_gates(m, crushed, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1470)
    fails = _fails(report)
    assert any("faint_structure retained" in f for f in fails)
    # no support measured (sparse star field, older run) -> skipped, never a false fail
    m2 = _metrics(star_count=700, fwhm=2.5, core_sat_frac=0.0)
    report2 = evaluate_gates(m2, None, Category.STARFIELD, THRESHOLDS, None, None, input_star_count=1000)
    assert _fails(report2) == []
    g = next(g for g in report2.gates if g.gate == "faint_structure")
    assert g.verdict is Verdict.SKIPPED


def test_faint_structure_is_not_applicable_to_starfields():
    """Owner ruling (2026-08-14): a globular's signal is primarily STARS, and the
    starless artifact this gate reads intentionally removes them -- M5's support
    measures bright in the final and at background in its starless, so an
    unchanged, healthy render failed a diffuse-retention test it can never pass.
    Star fields are held to source-relative STAR preservation instead. The
    verdict is an explicit SKIPPED with a stated reason, never a silent absence
    (Tenet 7) and never a hard fail."""
    m = _metrics(star_count=915, fwhm=2.4, core_sat_frac=0.0)
    # the exact M5 evidence that hard-failed before the ruling
    m5 = FaintEvidence(retention=0.1087, output_frac=0.0161, support_state="valid")
    report = evaluate_gates(m, m5, Category.STARFIELD, THRESHOLDS, None, None, input_star_count=1174)
    g = next(g for g in report.gates if g.gate == "faint_structure")
    assert g.verdict is Verdict.SKIPPED
    assert g.detail == "not applicable — stars are the subject"
    assert _fails(report) == []
    # ...and the same evidence still FAILS for the categories whose starless
    # really does represent the subject
    for cat in (Category.NEBULA, Category.GALAXY):
        rep = evaluate_gates(m, m5, cat, THRESHOLDS, None, None, input_star_count=1174)
        assert any("faint_structure retained" in f for f in _fails(rep)), cat


def test_faint_catastrophe_fires_on_nearly_empty_starless():
    m = _metrics(star_count=1100, fwhm=2.3, core_sat_frac=0.0)
    empty = FaintEvidence(retention=0.9, output_frac=0.0005, support_state="valid")
    report = evaluate_gates(m, empty, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1470)
    assert any("faint_structure catastrophic" in f for f in _fails(report))


def test_faint_missing_promised_support_fails_closed():
    m = _metrics(star_count=1100, fwhm=2.3, core_sat_frac=0.0)
    missing = FaintEvidence(retention=None, output_frac=0.05, support_state="missing_artifact")
    report = evaluate_gates(m, missing, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1470)
    g = next(g for g in report.gates if g.gate == "faint_structure")
    assert g.verdict is Verdict.FAIL
    assert "unavailable" in g.detail


def test_faint_not_measured_skips_honestly():
    m = _metrics(star_count=1100, fwhm=2.3, core_sat_frac=0.0)
    nm = FaintEvidence(retention=None, output_frac=None, support_state="not_measured")
    report = evaluate_gates(m, nm, Category.NEBULA, THRESHOLDS, None, None, input_star_count=1470)
    g = next(g for g in report.gates if g.gate == "faint_structure")
    assert g.verdict is Verdict.SKIPPED and "not measured" in g.detail


def test_active_fwhm_ceiling_fails_when_measurement_unavailable():
    """Same fail-closed contract for fwhm: an active ceiling with fwhm=None FAILS.
    Unlike a genuinely bloated fwhm, a missing measurement can never be attributed
    source-limited (qa.attribute_fwhm short-circuits to None on a None render_fwhm)
    -- it is a plain, unexcusable processing failure."""
    m = _metrics(
        star_count=600,
        fwhm=None,
        core_sat_frac=0.0,
        background_median_outside_mask=15.0,
    )
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, THRESHOLDS, None, None, input_star_count=800)
    g = next(g for g in report.gates if g.gate == "fwhm")
    assert g.verdict is Verdict.FAIL
    assert g.detail.startswith("fwhm unavailable")
    assert report.passed is False


def test_absent_findstar_thresholds_still_skip_missing_measurements():
    """The distinction the fail-closed change must preserve: threshold ABSENT (not
    configured for this category) still SKIPS a missing measurement -- only an
    ACTIVE threshold turns a missing measurement into a FAIL. Both families absent
    here; every other active gate stays healthy so the report still passes."""
    gates_no_findstar = dict(GATES)
    gates_no_findstar.pop("star_preservation", None)
    gates_no_findstar.pop("fwhm_ceiling", None)
    th_no_findstar = gate_thresholds_from_cfg(gates_no_findstar)

    m = _metrics(
        star_count=None,
        fwhm=None,
        core_sat_frac=0.0,
        background_median_outside_mask=15.0,
    )
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, th_no_findstar, None, None)
    verdicts = {g.gate: g for g in report.gates}
    assert verdicts["star_preservation"].verdict is Verdict.SKIPPED
    assert verdicts["star_preservation"].detail  # non-empty "threshold not configured" detail
    assert "threshold not configured" in verdicts["star_preservation"].detail
    assert verdicts["fwhm"].verdict is Verdict.SKIPPED
    assert verdicts["fwhm"].detail
    assert "threshold not configured" in verdicts["fwhm"].detail
    assert report.passed is True


def test_unknown_category_yields_all_skipped_and_warning():
    """The brief's headline case: UNKNOWN category must NOT call PerCategory.for_category
    (that raises, by design, on the engine/decision path -- see PerCategory.for_category).
    Every gate is instead a visible SKIPPED verdict, plus an explicit warning -- never a
    silent no-op."""
    m = _metrics(star_count=10, fwhm=9.9, core_sat_frac=0.9)
    report = evaluate_gates(m, _FAINT_OK, Category.UNKNOWN, THRESHOLDS, None, None)
    assert report.category is Category.UNKNOWN
    assert len(report.gates) > 0
    assert all(g.verdict is Verdict.SKIPPED for g in report.gates)
    assert report.passed is True
    assert len(report.warnings) == 1
    assert "category unreadable" in report.warnings[0]
    assert "SKIPPED" in report.warnings[0]


def test_disabled_gates_skip_without_crashing():
    th = GateThresholds(
        enabled=False,
        reference_pixscale=None,
        reference_dims=None,
        star_preservation_min_retention=None,
        star_preservation_catastrophe_floor=None,
        fwhm_ceiling=None,
        core_sat_ceiling=None,
        faint_retention_min=None,
        faint_catastrophe_floor=None,
        near_black_ceiling=None,
        sky_median_ceiling=None,
        pegged_chroma_ceiling=None,
        core_saturation_ceiling=None,
        star_saturation_ceiling=None,
        body_rgb_max_ratio_starfield=None,
        body_rgb_max_ratio_galaxy=None,
        star_retention_warn=0.35,
    )
    m = _metrics(star_count=10, fwhm=9.9, core_sat_frac=0.9)
    report = evaluate_gates(m, _FAINT_OK, Category.NEBULA, th, None, None)
    assert all(g.verdict is Verdict.SKIPPED for g in report.gates)
    assert report.passed is True
    assert report.warnings == ()  # disabled (not unknown-category) -> no warning


def test_gate_scales_identity_at_reference():
    assert scale_factors(THRESHOLDS, 3.99, (1080, 1920)) == (1.0, 1.0)
    assert scale_factors(THRESHOLDS, None, (2096, 3726)) == (1.0, 1.0)
    empty = gate_thresholds_from_cfg({})
    assert scale_factors(empty, 3.74, PRO_DIMS) == (1.0, 1.0)  # no reference cfg


def test_gate_scales_s30_pro():
    fwhm_scale, area_scale = scale_factors(THRESHOLDS, PRO_PIXSCALE, PRO_DIMS)
    assert 1.05 < fwhm_scale < 1.09
    assert 0.25 < area_scale < 0.35


def test_faint_gate_is_fov_independent():
    # Diagnosis 3 regression: the same healthy retention passes at ANY frame
    # geometry -- there is no inverse-FOV scaling to false-alarm a wider or
    # narrower field (the S50 M82 0.0168-vs-0.0745 failure class is gone)
    small_field = _metrics(star_count=216, fwhm=2.5, core_sat_frac=0.0, dimensions=(1080, 1920))
    wide_field = _metrics(star_count=1897, fwhm=2.5, core_sat_frac=0.0, dimensions=PRO_DIMS)
    for m, px, input_stars in ((small_field, 2.381, 294), (wide_field, PRO_PIXSCALE, 2500)):
        report = evaluate_gates(m, _FAINT_OK, Category.GALAXY, THRESHOLDS, px, None, input_star_count=input_stars)
        assert _fails(report) == [], report.gates


def test_s30_pro_fwhm_ceiling_scales_but_still_gates():
    m = _metrics(star_count=1897, core_sat_frac=0.0, dimensions=PRO_DIMS, fwhm=3.6)
    assert (
        _fails(evaluate_gates(m, None, Category.GALAXY, THRESHOLDS, None, None, input_star_count=2500)) != []
    )  # unscaled: false alarm
    assert _fails(evaluate_gates(m, None, Category.GALAXY, THRESHOLDS, PRO_PIXSCALE, None, input_star_count=2500)) == []
    bloated = _metrics(star_count=1897, core_sat_frac=0.0, dimensions=PRO_DIMS, fwhm=4.5)
    fails = _fails(
        evaluate_gates(bloated, None, Category.GALAXY, THRESHOLDS, PRO_PIXSCALE, None, input_star_count=2500)
    )
    assert any("fwhm" in f and "scaled" in f for f in fails)


def test_gray_wash_gate():
    m = _metrics(star_count=1630, fwhm=2.5, core_sat_frac=0.0, background_median_outside_mask=39.96)
    fails = _fails(evaluate_gates(m, _FAINT_OK, Category.GALAXY, THRESHOLDS, None, None, input_star_count=2200))
    assert any("gray wash" in f for f in fails)
    good = _metrics(star_count=1630, fwhm=2.5, core_sat_frac=0.0, background_median_outside_mask=13.2)
    assert _fails(evaluate_gates(good, _FAINT_OK, Category.GALAXY, THRESHOLDS, None, None, input_star_count=2200)) == []
    # documentation's full-DR promise legitimately brightens the sky -> exempt
    doc_report = evaluate_gates(m, _FAINT_OK, Category.GALAXY, THRESHOLDS, None, "documentation", input_star_count=2200)
    assert _fails(doc_report) == []
    g = next(g for g in doc_report.gates if g.gate == "sky_median")
    assert g.verdict is Verdict.SKIPPED


def test_body_rgb_max_ratio_warns_not_fails():
    real_gates = load_config()["qa"]["gates"]
    th = gate_thresholds_from_cfg(real_gates)
    assert th.body_rgb_max_ratio_starfield == 1.25
    assert th.body_rgb_max_ratio_galaxy == 1.25

    # M5 evidence: body_rgb [40.8, 29.8, 30.2] (R/G 1.37) -> imbalance WARN, never a FAIL
    m = _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, body_rgb=(40.8, 29.8, 30.2))
    report = evaluate_gates(m, None, Category.STARFIELD, th, None, None, input_star_count=1400)
    assert _fails(report) == []  # never a hard fail
    g = next(g for g in report.gates if g.gate == "body_rgb")
    assert g.verdict is Verdict.WARN
    assert "body RGB imbalance 1.37 > 1.25" in g.detail
    assert "R=40.8 G=29.8 B=30.2" in g.detail

    # a balanced body passes
    balanced = _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, body_rgb=(30.0, 29.0, 30.0))
    report2 = evaluate_gates(balanced, None, Category.STARFIELD, th, None, None, input_star_count=1400)
    g2 = next(g for g in report2.gates if g.gate == "body_rgb")
    assert g2.verdict is Verdict.PASS

    # nebula has no threshold (LP Halpha naturally dominates red) -> always skipped
    nebula_m = _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, body_rgb=(80.0, 20.0, 20.0))
    report3 = evaluate_gates(nebula_m, None, Category.NEBULA, th, None, None, input_star_count=1400)
    g3 = next(g for g in report3.gates if g.gate == "body_rgb")
    assert g3.verdict is Verdict.SKIPPED


def test_star_retention_warn():
    real_gates = load_config()["qa"]["gates"]
    th = gate_thresholds_from_cfg(real_gates)
    assert th.star_retention_warn == 0.35

    retention = compute_star_retention(300, 1000)
    assert retention == 0.3
    warning = star_retention_warning(retention, 300, 1000, th.star_retention_warn)
    assert warning is not None
    assert "star field emptied: final keeps 30% of stars-on count" in warning
    assert "(300/1000, warn floor 35%)" in warning

    # kept enough stars -> no warning
    retention_ok = compute_star_retention(500, 1000)
    assert star_retention_warning(retention_ok, 500, 1000, th.star_retention_warn) is None


def _thresholds(core_ceiling=None, star_ceiling=None):
    """GATES fixture (module-level, mirrors config.yaml qa.gates) with the two new
    saturation ceilings overridden -- everything else stays at the passing defaults."""
    gates = dict(GATES)
    gates["core_saturation_ceiling"] = {"nebula": core_ceiling, "starfield": core_ceiling, "galaxy": core_ceiling}
    gates["star_saturation_ceiling"] = {"nebula": star_ceiling, "starfield": star_ceiling, "galaxy": star_ceiling}
    return gate_thresholds_from_cfg(gates)


def test_core_and_star_saturation_skip_when_ceilings_absent():
    # thresholds with core/star ceilings None -> both gates SKIPPED, never fail
    report = evaluate_gates(
        _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, core_saturation_median=0.9, star_saturation_median=0.9),
        None,
        Category.NEBULA,
        _thresholds(core_ceiling=None, star_ceiling=None),
        3.99,
        "showcase",
        input_star_count=1400,
    )
    verdicts = {g.gate: g.verdict for g in report.gates}
    assert verdicts["core_saturation"] is Verdict.SKIPPED
    assert verdicts["star_saturation"] is Verdict.SKIPPED
    assert report.passed  # WARN/SKIPPED never blocks


def test_core_saturation_warns_when_over_ceiling_but_never_fails():
    report = evaluate_gates(
        _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, core_saturation_median=0.8),
        None,
        Category.NEBULA,
        _thresholds(core_ceiling=0.3),
        3.99,
        "showcase",
        input_star_count=1400,
    )
    g = next(g for g in report.gates if g.gate == "core_saturation")
    assert g.verdict is Verdict.WARN
    assert report.passed  # WARN is not a fail


def test_star_saturation_warns_when_over_ceiling():
    report = evaluate_gates(
        _metrics(star_count=1000, fwhm=2.5, core_sat_frac=0.0, star_saturation_median=0.6),
        None,
        Category.NEBULA,
        _thresholds(star_ceiling=0.2),
        3.99,
        "showcase",
        input_star_count=1400,
    )
    g = next(g for g in report.gates if g.gate == "star_saturation")
    assert g.verdict is Verdict.WARN


def test_percat_or_none_absent_empty_and_all_null_are_equivalent():
    """io._percat_or_none collapses every 'no usable threshold' form to None (Task 4
    of 4C introduced the all-null collapse to keep empty-config == shipped-config
    equality). Absent family, empty dict, and an explicit all-null dict must all
    produce None -- the form the config uses to ship a gate inert."""

    from seestar.io import _percat_or_none

    for d in (None, {}, {"nebula": None, "galaxy": None, "starfield": None}):
        assert _percat_or_none(d) is None, f"{d!r} should collapse to None"


def test_percat_or_none_partial_family_retains_category_behavior():
    """A PARTIALLY populated family must NOT collapse -- it keeps per-category values,
    returning None only for the categories that were null (per-category skip)."""
    from seestar.io import _percat_or_none

    pc = _percat_or_none({"nebula": 0.5, "galaxy": None, "starfield": None})
    assert pc is not None
    assert pc.for_category(Category.NEBULA) == 0.5
    assert pc.for_category(Category.GALAXY) is None
    assert pc.for_category(Category.STARFIELD) is None


def test_gate_verdict_identical_for_absent_vs_all_null_family():
    """Consumer-level equivalence: a gate whose ceiling family is ABSENT and one whose
    family is explicitly ALL-NULL must produce the same SKIPPED verdict (both guard
    `if thresholds.X else None`). Proven here for pegged_chroma."""
    m = _metrics(pegged_chroma_frac_body=0.99)  # would FAIL a real ceiling
    absent = gate_thresholds_from_cfg({"enabled": True})
    all_null = gate_thresholds_from_cfg(
        {"enabled": True, "pegged_chroma_ceiling": {"nebula": None, "galaxy": None, "starfield": None}}
    )
    va = next(
        g for g in evaluate_gates(m, None, Category.NEBULA, absent, None, None).gates if g.gate == "pegged_chroma"
    )
    vn = next(
        g for g in evaluate_gates(m, None, Category.NEBULA, all_null, None, None).gates if g.gate == "pegged_chroma"
    )
    assert va.verdict is Verdict.SKIPPED and vn.verdict is Verdict.SKIPPED


def test_percategory_for_category_still_refuses_unknown():
    """The design contrast the brief calls out: PerCategory.for_category (the engine/
    decision path, core/policy.py) RAISES for UNKNOWN -- evaluate_gates must never call
    it with Category.UNKNOWN, which is exactly why it short-circuits first."""
    import pytest

    pc = PerCategory(nebula=1.0, galaxy=2.0, starfield=3.0)
    with pytest.raises(ValueError):
        pc.for_category(Category.UNKNOWN)


def test_evaluate_hero_pass_when_gates_pass_and_converged_and_within_tolerance():
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA, gates=(GateResult("pegged_chroma", Verdict.PASS, ""),), warnings=(), passed=True
    )
    r = evaluate_hero(qa, solve_converged=True, contract_delta=1.5, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PASS
    assert r.processing_failed_gates == ()


def test_evaluate_hero_ngc7000_style_non_convergence_fails_even_with_clean_gates():
    """NGC7000: every hard gate passes, but the solver never converged."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(GateResult("star_count", Verdict.PASS, ""), GateResult("near_black", Verdict.PASS, "")),
        warnings=(),
        passed=True,
    )
    r = evaluate_hero(qa, solve_converged=False, contract_delta=None, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert any("did not converge" in reason for reason in r.reasons)


def test_evaluate_hero_m8_style_converged_but_contract_drift_fails():
    """M8: solver converged (66.1 vs 66.2), but the final hero (post star-reduction)
    measured body_p90 57.2 -- a drift no convergence check alone would catch."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA, gates=(GateResult("star_count", Verdict.PASS, ""),), warnings=(), passed=True
    )
    r = evaluate_hero(qa, solve_converged=True, contract_delta=57.2 - 66.1, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert any("drifted" in reason for reason in r.reasons)


def test_evaluate_hero_m8_style_chroma_hard_fail_is_not_excused_by_source_limited_fwhm():
    """M8's real capture: fwhm fails but is source-limited (excused); pegged_chroma
    fails and is NOT source-limited -- must not be excused by the fwhm attribution."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(
            GateResult("fwhm", Verdict.FAIL, "fwhm 5.90 > nebula ceiling 3.3"),
            GateResult("pegged_chroma", Verdict.FAIL, "neon color: pegged_chroma_frac_body 26.07% > 20.0%"),
        ),
        warnings=(),
        passed=False,
    )
    r = evaluate_hero(
        qa, solve_converged=True, contract_delta=0.1, contract_tolerance=2.0, source_limited_gates=frozenset({"fwhm"})
    )
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert r.processing_failed_gates == ("pegged_chroma",)
    assert r.source_limited_gates == ("fwhm",)


def test_evaluate_hero_source_limited_only_when_no_other_failure():
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(GateResult("fwhm", Verdict.FAIL, "fwhm 5.90 > nebula ceiling 3.3"),),
        warnings=(),
        passed=False,
    )
    r = evaluate_hero(
        qa, solve_converged=True, contract_delta=0.1, contract_tolerance=2.0, source_limited_gates=frozenset({"fwhm"})
    )
    assert r.outcome is HeroOutcome.SOURCE_LIMITED


def test_evaluate_hero_pipeline_limited_fwhm_alone_is_soft_not_blocking():
    """2026-08-01, the M8 false-positive finding: a genuinely pipeline-attributed
    (not source-limited) fwhm miss must not by itself alarm the end user -- it's
    an aesthetic-only note, never a processing failure, by default."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(GateResult("fwhm", Verdict.FAIL, "fwhm 5.90 > nebula ceiling 3.3"),),
        warnings=(),
        passed=False,
    )
    r = evaluate_hero(qa, solve_converged=True, contract_delta=0.1, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PASS
    assert r.processing_failed_gates == ()
    assert r.soft_fail_gates == ("fwhm",)
    assert any("soft quality note" in reason for reason in r.reasons)


def test_evaluate_hero_soft_fwhm_does_not_mask_a_real_chroma_failure():
    """A pipeline-limited fwhm miss alongside a genuine pegged_chroma failure must
    still report processing_failed_gates == ('pegged_chroma',) -- exactly the shape
    core.recovery.decide_candidate's chroma-recovery arm requires, so a co-occurring
    soft fwhm miss no longer blocks the existing chroma recovery loop from firing."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(
            GateResult("fwhm", Verdict.FAIL, "fwhm 5.90 > nebula ceiling 3.3"),
            GateResult("pegged_chroma", Verdict.FAIL, "neon color: pegged_chroma_frac_body 26.07% > 20.0%"),
        ),
        warnings=(),
        passed=False,
    )
    r = evaluate_hero(qa, solve_converged=True, contract_delta=0.1, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert r.processing_failed_gates == ("pegged_chroma",)
    assert r.soft_fail_gates == ("fwhm",)


def test_evaluate_hero_fwhm_unavailable_stays_hard_despite_default_soft_policy():
    """M2 fail-closed: an 'unavailable' fwhm FAIL (the findstar instrument itself
    broke) is never soft, even though fwhm is soft-eligible by default -- a broken
    measurement stays a hard, visible failure regardless of which gate it hit."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA,
        gates=(
            GateResult("star_count", Verdict.FAIL, "star_count unavailable: findstar measurement failed"),
            GateResult("fwhm", Verdict.FAIL, "fwhm unavailable: findstar measurement failed"),
        ),
        warnings=(),
        passed=False,
    )
    r = evaluate_hero(qa, solve_converged=True, contract_delta=0.1, contract_tolerance=2.0)
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert r.processing_failed_gates == ("star_count", "fwhm")
    assert r.soft_fail_gates == ()


def test_evaluate_hero_near_miss_nonconvergence_within_tolerance_passes():
    """2026-08-01, the M8 finding: the solver landed body_p90 71.3 vs target 72.0
    (miss 0.7) and exhausted its iteration budget without hitting its own internal
    bisection tolerance -- but 0.7 is well within the category's existing
    contract_tolerance (2.0), so this is not the catastrophe the NGC7000 lesson
    guards against and must not alarm the end user."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA, gates=(GateResult("star_count", Verdict.PASS, ""),), warnings=(), passed=True
    )
    r = evaluate_hero(
        qa, solve_converged=False, contract_delta=None, contract_tolerance=2.0, solve_miss=abs(71.3 - 72.0)
    )
    assert r.outcome is HeroOutcome.PASS
    assert not any("stretch solver did not converge" == reason for reason in r.reasons)
    assert any("within tolerance" in reason for reason in r.reasons)


def test_evaluate_hero_large_miss_nonconvergence_still_fails():
    """A non-convergence with a large miss (well beyond contract_tolerance) must
    still block -- the NGC7000 lesson survives the near-miss carve-out; leniency is
    bounded, not a blanket exemption for every non-converged solve."""
    from seestar.core.qa_rules import evaluate_hero
    from seestar.core.types import GateResult, HeroOutcome, QAReport, Verdict

    qa = QAReport(
        category=Category.NEBULA, gates=(GateResult("star_count", Verdict.PASS, ""),), warnings=(), passed=True
    )
    r = evaluate_hero(qa, solve_converged=False, contract_delta=None, contract_tolerance=2.0, solve_miss=15.0)
    assert r.outcome is HeroOutcome.PROCESSING_FAILED
    assert any("did not converge" in reason for reason in r.reasons)
