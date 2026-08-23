"""Diagnoses 5 + 3 (spec 2026-08-12), Task 4: one stable subject support shared by
the stretch solver and faint-structure QA.

Phase A: `measure.compute_support` -- deterministic support from a starless probe
array; works for compact AND frame-filling subjects; too-small support withdraws
validity (conservative global fallback downstream, recorded).
Phase B: the solver freezes that support across every probe of one solve
(`Measure.support` on RENDER_METRIC), and a monotonicity guard demotes a
jump-driven "converged" result to the conservative best-valid-probe fallback.
Phase C: faint-structure QA judges retained supported structure relative to the
run's own reference, never an inverse-FOV frame fraction
(`core.qa_rules` faint gate tests live in test_core_qa_rules.py).
"""

from __future__ import annotations

import os

import numpy as np
from PIL import Image

from seestar.core.solve import (
    SolveRequest,
    monotonicity_violation,
    observe_probe,
    solve_result,
    start_solve,
)
from seestar.core.types import ProbeRecord, SolverBounds, TargetMetric
from seestar.execute import _probe_metric, _region_observation
from seestar.measure import compute_support

RNG = np.random.default_rng(11)


# ---------------------------------------------------------------------------
# Phase A: compute_support (pure array -> support mask + typed summary)
# ---------------------------------------------------------------------------


def _sky(h=200, w=300, level=0.02, noise=0.002):
    return RNG.normal(level, noise, (h, w))


def test_compact_subject_yields_support_over_the_subject():
    a = _sky()
    a[80:120, 120:180] += 0.05  # compact bright body
    mask, info = compute_support(a)
    assert info["valid"] is True
    assert info["source"] == "starless_sigma"
    # support concentrates on the subject: most of the blob is in, most sky is out
    assert mask[90:110, 130:170].mean() > 0.9
    assert mask[:40, :].mean() < 0.05
    assert abs(info["area_frac"] - mask.mean()) < 5e-5  # stored rounded to 4 dp
    assert info["dims"] == (300, 200)


def test_frame_filling_subject_does_not_collapse():
    # smooth frame-filling nebulosity: a hard clipped-sigma threshold sees the
    # pervasive signal AS the background; the low-sigma tier must still find a
    # meaningful stable support instead of collapsing to (near) nothing
    y = np.linspace(0.0, 1.0, 200)[:, None]
    x = np.linspace(0.0, 1.0, 300)[None, :]
    a = 0.02 + 0.04 * (np.sin(3 * x * np.pi) * 0.5 + 0.5) * (0.4 + 0.6 * y) + RNG.normal(0, 0.002, (200, 300))
    mask, info = compute_support(a)
    assert info["valid"] is True
    assert info["area_frac"] >= 0.05  # meaningful, not the held design's 0.24%-1.8% collapse


def test_pure_noise_field_withdraws_validity():
    mask, info = compute_support(_sky())
    assert info["valid"] is False
    assert info["source"] == "none"
    assert mask.sum() == 0  # nothing pretends to be structure


def test_support_is_deterministic():
    a = _sky()
    a[50:90, 40:100] += 0.06
    m1, i1 = compute_support(a)
    m2, i2 = compute_support(a.copy())
    assert np.array_equal(m1, m2) and i1 == i2


# ---------------------------------------------------------------------------
# Phase B: probe metric measured on the FROZEN support raster
# ---------------------------------------------------------------------------


def _write_jpg(tmp_path, arr01, name="probe.jpg"):
    img = Image.fromarray((np.clip(arr01, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    p = tmp_path / name
    img.save(p, quality=95)
    return str(p)


def _write_support(tmp_path, mask, name="support.png"):
    p = tmp_path / name
    Image.fromarray((mask.astype(np.uint8)) * 255).save(p)
    return str(p)


def test_probe_metric_uses_frozen_support(tmp_path):
    a = np.full((120, 160), 0.05)
    a[40:80, 60:120] = 0.5  # subject at ~128/255
    jpg = _write_jpg(tmp_path, a)
    mask = np.zeros((120, 160), dtype=bool)
    mask[40:80, 60:120] = True
    support = _write_support(tmp_path, mask)
    val = _probe_metric(jpg, "body_p90", support_path=support)
    assert 120 <= val <= 135  # the subject's level, not the sky-diluted global p90


def test_probe_metric_support_dims_mismatch_falls_back_global(tmp_path):
    a = np.full((120, 160), 0.05)
    a[40:80, 60:120] = 0.5
    jpg = _write_jpg(tmp_path, a)
    wrong = np.zeros((60, 80), dtype=bool)
    wrong[10:20, 10:20] = True
    support = _write_support(tmp_path, wrong)
    # never a fresh per-probe mask recomputation: a geometry mismatch takes the
    # named conservative global fallback (global p90)
    val = _probe_metric(jpg, "body_p90", support_path=support)
    global_p90 = _probe_metric(jpg, "body_p90", support_path=None, force_global=True)
    assert val == global_p90


def test_probe_metric_freezes_the_object_mask_and_measures_over_it(tmp_path):
    # Owner ruling (2026-08-14): the first body probe freezes the OBJECT MASK --
    # the region target level 72 was calibrated against -- writes it as the
    # durable raster, and measures itself over exactly those pixels.
    a = np.full((120, 160), 0.05)
    a[50:70, 70:100] = 0.5  # ~3% of frame: small enough that the GLOBAL p90 is sky
    jpg = _write_jpg(tmp_path, a)
    frozen = str(tmp_path / "M_solve_region.png")
    val = _probe_metric(jpg, "body_p90", freeze_to=frozen)

    assert os.path.exists(frozen), "the freezing probe must persist the raster it measured over"
    obs = _region_observation(frozen)
    assert obs.valid and obs.source == "frozen_object_mask" and obs.dims == (160, 120)
    # the frozen raster IS the region measured: replaying it reproduces the value
    assert _probe_metric(jpg, "body_p90", support_path=frozen) == val
    # ...and it is the subject's level, not the sky-diluted global p90
    assert val > _probe_metric(jpg, "body_p90", force_global=True)


def test_probe_metric_freeze_writes_nothing_when_the_mask_is_too_small(tmp_path):
    # a featureless frame has no object to mask: write NO raster and take the
    # recorded global fallback, so the planner hears "no region frozen" instead
    # of inheriting an empty one
    jpg = _write_jpg(tmp_path, np.full((120, 160), 0.05))
    frozen = str(tmp_path / "M_solve_region.png")
    val = _probe_metric(jpg, "body_p90", freeze_to=frozen)

    assert not os.path.exists(frozen)
    assert val == _probe_metric(jpg, "body_p90", force_global=True)
    obs = _region_observation(frozen)
    assert obs.valid is False and obs.source == "none" and "minimum" in obs.detail


def test_region_observation_describes_the_persisted_raster_not_the_intent(tmp_path):
    # the provenance must be read back off the file every later probe, the hero
    # contract and a replay will read -- never off an in-memory mask
    mask = np.zeros((120, 160), dtype=bool)
    mask[40:80, 60:120] = True  # 2400 of 19200 px
    path = _write_support(tmp_path, mask, name="frozen.png")
    obs = _region_observation(path)
    assert obs.valid is True
    assert obs.dims == (160, 120)
    assert abs(obs.area_frac - mask.mean()) < 5e-5


def test_probe_metric_without_support_matches_legacy_mask_path(tmp_path):
    # no support (starfield / support-measure failure): the pre-Task-4 object_mask
    # behavior is preserved byte-for-byte
    a = np.full((120, 160), 0.05)
    a[40:80, 60:120] = 0.5
    jpg = _write_jpg(tmp_path, a)
    assert _probe_metric(jpg, "body_p90") == _probe_metric(jpg, "body_p90", support_path=None)


# ---------------------------------------------------------------------------
# Phase B: monotonicity guard over the ordered probe trace
# ---------------------------------------------------------------------------


def _probe(mtf, value, bp=0.05, target=72.0):
    return ProbeRecord(
        phase="mtf",
        mtf_mid=mtf,
        blackpoint=bp,
        value=value,
        sky=10.0,
        target_level=target,
        distance=round(abs(value - target), 4),
    )


def test_monotonic_trace_has_zero_violation():
    # lower mtf = brighter: value strictly decreasing in mtf is the expected law
    probes = (_probe(0.10, 90.0), _probe(0.18, 55.0), _probe(0.14, 71.8), _probe(0.12, 80.0))
    assert monotonicity_violation(probes) == 0.0


def test_jumpy_trace_reports_violation_magnitude():
    # the actual S50 M82 moving-mask trace (evidence run probes): value is not a
    # function of mtf -- 75.8 @ .1566 vs 81.8 @ .1615 is a +6.0 reversal of the
    # lower-mtf-is-brighter law, far beyond solver tolerance
    probes = (
        _probe(0.1494, 78.7),
        _probe(0.1566, 75.8),
        _probe(0.1602, 80.8),
        _probe(0.1620, 68.2),
        _probe(0.1611, 81.7),
        _probe(0.1615, 81.8),
    )
    v = monotonicity_violation(probes)
    assert v >= 5.9  # >> MONO_TOL_FACTOR (4) x tol (0.5)


def test_violations_grouped_by_blackpoint():
    # probes at different bp are different curves -- never compared across groups
    probes = (_probe(0.10, 90.0, bp=0.05), _probe(0.12, 95.0, bp=0.06))
    assert monotonicity_violation(probes) == 0.0


def _drive_solve(responses, sky=10.0, target=72.0):
    req = SolveRequest(
        metric=TargetMetric.BODY_P90,
        target_level=target,
        bounds=SolverBounds(lo=0.02, hi=0.25, tol=0.5, max_iter=12),
        blackpoint=0.05,
        linear_median=0.06,
        sky_ceiling=None,
    )
    state, probe = start_solve(req)
    while probe is not None:
        value = responses(probe.mtf_mid)
        state, probe = observe_probe(state, value, sky)
    return state


def test_fixed_support_monotone_response_converges_cleanly():
    # a monotone response (what a FROZEN mask produces) converges with zero violation
    def resp(mtf):
        lo, hi = 0.02, 0.25
        t = (mtf - lo) / (hi - lo)
        return 300.0 - t * 299.0

    state = _drive_solve(resp)
    result = solve_result(state)
    assert result.converged is True
    assert monotonicity_violation(state.probes) == 0.0


# ---------------------------------------------------------------------------
# Phase C: faint_evidence composition (qa.faint_evidence)
# ---------------------------------------------------------------------------


def _write_starless(tmp_path, arr01, name="starless.tif"):
    img = Image.fromarray((np.clip(arr01, 0, 1) * 255).astype(np.uint8)).convert("RGB")
    p = tmp_path / name
    img.save(p)
    return str(p)


def test_faint_evidence_healthy_retention(tmp_path):
    from seestar.qa import faint_evidence

    a = np.full((100, 140), 0.04)
    a[30:70, 40:100] = 0.4  # revealed structure where the support says it lives
    starless = _write_starless(tmp_path, a)
    mask = np.zeros((100, 140), dtype=bool)
    mask[30:70, 40:100] = True
    support = _write_support(tmp_path, mask)
    ev, warning = faint_evidence(starless, support, support_recorded=True)
    assert ev.support_state == "valid"
    assert ev.retention is not None and ev.retention > 0.9
    assert warning is None


def test_faint_evidence_crushed_structure_low_retention(tmp_path):
    from seestar.qa import faint_evidence

    a = np.full((100, 140), 0.04)  # structure crushed into the background
    starless = _write_starless(tmp_path, a)
    mask = np.zeros((100, 140), dtype=bool)
    mask[30:70, 40:100] = True
    support = _write_support(tmp_path, mask)
    ev, _ = faint_evidence(starless, support, support_recorded=True)
    assert ev.support_state == "valid"
    assert ev.retention is not None and ev.retention < 0.3


def test_faint_evidence_not_recorded_is_none(tmp_path):
    from seestar.qa import faint_evidence

    ev, warning = faint_evidence(str(tmp_path / "missing.tif"), None, support_recorded=False)
    assert ev is None and warning is None


def test_faint_evidence_missing_promised_raster_fails_closed(tmp_path):
    from seestar.qa import faint_evidence

    starless = _write_starless(tmp_path, np.full((100, 140), 0.04))
    ev, warning = faint_evidence(starless, str(tmp_path / "gone_support.png"), support_recorded=True)
    assert ev.support_state == "missing_artifact"
    assert warning is not None


def test_faint_evidence_geometry_mismatch_fails_closed(tmp_path):
    from seestar.qa import faint_evidence

    starless = _write_starless(tmp_path, np.full((100, 140), 0.04))
    wrong = np.zeros((50, 70), dtype=bool)
    wrong[10:20, 10:20] = True
    support = _write_support(tmp_path, wrong)
    ev, warning = faint_evidence(starless, support, support_recorded=True)
    assert ev.support_state == "missing_artifact"
    assert warning is not None and "geometry" in warning


def test_faint_evidence_no_starless_render_not_measured(tmp_path):
    from seestar.qa import faint_evidence

    mask = np.zeros((100, 140), dtype=bool)
    mask[30:70, 40:100] = True
    support = _write_support(tmp_path, mask)
    ev, _ = faint_evidence(str(tmp_path / "never_rendered.tif"), support, support_recorded=True)
    assert ev.support_state == "not_measured"


# ---------------------------------------------------------------------------
# ROWORDER: the support PNG must be in RASTER order (review BLOCK finding,
# 2026-08-13). The starless probe FITS is Siril-written (ROWORDER=BOTTOM-UP:
# row 0 = bottom scanline), but every consumer of the PNG -- probe JPGs, the
# hero contract, QA's starless TIFF -- is a top-down raster. Same law as
# adapters/starnet.py::_fits_to_starnet_tif (owner-caught shipped-flip lesson).
# ---------------------------------------------------------------------------


def test_support_png_is_raster_order(tmp_path, monkeypatch):
    import tifffile

    import seestar.measure as M

    # off-center subject in the STARLESS TIFF's raster top rows: the PNG must
    # keep it at the raster top (StarNet TIFFs are already raster order -- no
    # flip; the FITS round-trip with its untrustworthy ROWORDER header is gone)
    a = RNG.normal(0.02, 0.002, (100, 140))
    a[5:35, 40:100] += 0.08
    tif_path = tmp_path / "starless.tif"
    tifffile.imwrite(tif_path, (np.clip(a, 0, 1) * 65535).astype(np.uint16))

    monkeypatch.setattr(M, "_starless_probe", lambda *args, **kw: str(tif_path))
    monkeypatch.setattr(M, "_cleanup_glob", lambda *args, **kw: None)
    out_png = tmp_path / "support.png"
    raster_mask, info = M.measure_subject_support("unused.fit", str(tmp_path), "T", None, str(out_png))
    assert info["valid"] is True
    png = np.asarray(Image.open(out_png).convert("L")) > 127
    assert png[5:35, 40:100].mean() > 0.9  # subject stays at the raster top
    assert png[60:, :].mean() < 0.05  # bottom rows are sky
    assert np.array_equal(png, raster_mask)


def test_support_handles_color_starless_tif(tmp_path, monkeypatch):
    import tifffile

    import seestar.measure as M

    a = RNG.normal(0.02, 0.002, (100, 140, 3))
    a[5:35, 40:100, :] += 0.08  # (H, W, 3) channel-last, as StarNet writes
    tif_path = tmp_path / "starless_rgb.tif"
    tifffile.imwrite(tif_path, (np.clip(a, 0, 1) * 65535).astype(np.uint16))

    monkeypatch.setattr(M, "_starless_probe", lambda *args, **kw: str(tif_path))
    monkeypatch.setattr(M, "_cleanup_glob", lambda *args, **kw: None)
    out_png = tmp_path / "support_rgb.png"
    _, info = M.measure_subject_support("unused.fit", str(tmp_path), "T", None, str(out_png))
    assert info["valid"] is True
    png = np.asarray(Image.open(out_png).convert("L")) > 127
    assert png[5:35, 40:100].mean() > 0.9


def test_small_subject_absolute_pixel_floor():
    # review finding 3: an ~1%-of-frame subject (the S50 M82 class) must still
    # produce VALID support -- validity is an absolute pixel floor, not a frame
    # fraction (no FOV dependence)
    a = RNG.normal(0.02, 0.002, (400, 600))  # 240k px frame
    a[180:220, 280:340] += 0.08  # 2400 px subject = 1% of frame
    mask, info = compute_support(a)
    assert info["valid"] is True
    assert info["source"] == "starless_sigma"


def test_read_starless_raster_falls_back_to_pil_on_lzw(tmp_path, monkeypatch):
    # StarNet writes LZW TIFFs; without imagecodecs tifffile raises ValueError.
    # The reader must fall back to PIL (which decodes LZW natively) rather than
    # failing the whole support measurement (field regression, 2026-08-13:
    # every real render silently ran mask-free because of this).
    import seestar.measure as M

    a = np.zeros((60, 80), dtype=np.uint8)
    a[5:20, 10:40] = 200
    p = tmp_path / "lzw.tif"
    Image.fromarray(a).save(p, format="TIFF", compression="tiff_lzw")

    import tifffile

    def _boom(path):
        raise ValueError("<COMPRESSION.LZW: 5> requires the 'imagecodecs' package")

    monkeypatch.setattr(tifffile, "imread", _boom)
    out = M._read_starless_raster(str(p))
    assert out.shape == (60, 80)
    assert out[5:20, 10:40].mean() > 150  # raster order preserved by the fallback


# ---------------------------------------------------------------------------
# Owner Gate 2 (2026-08-14): frame-filling collapse, catalogued-extent
# corroboration, artefact suppression, and an outside-the-subject sky metric.
# ---------------------------------------------------------------------------


def _frame_filling(h=400, w=600):
    """Pervasive nebulosity over most of the frame with a dark lane and bright
    knots -- the shape that makes a global med+3sigma threshold collapse onto the
    knots (the real NGC7000 failure)."""
    a = RNG.normal(0.02, 0.002, (h, w))
    a[:, : int(w * 0.75)] += 0.030  # nebulosity over three quarters of the field
    a[int(h * 0.3) : int(h * 0.55), int(w * 0.15) : int(w * 0.45)] += 0.010  # brighter region
    a[120:150, 120:170] += 0.060  # bright knots
    a[250:270, 300:340] += 0.055
    return a


def test_frame_filling_hard_tier_collapses_without_an_expectation():
    """Baseline: with no catalogued extent the established hard tier stands, even
    though it has collapsed onto the knots. Measurement alone cannot tell."""
    a = _frame_filling()
    mask, info = compute_support(a)
    assert info["source"] == "starless_sigma"
    assert info["area_frac"] < 0.10  # only the knots


def test_catalogued_extent_detects_the_collapse_and_recovers_broad_structure():
    a = _frame_filling()
    hard_frac = compute_support(a)[1]["area_frac"]
    mask, info = compute_support(a, expected_frac=0.60)  # catalogue says it fills the frame
    assert info["source"] == "starless_dark_anchored"
    assert info["valid"] is True
    assert info["area_frac"] > 4 * hard_frac  # broad structure, not just the knots
    assert "frame-filling" in info["detail"]
    # the recovered support follows the nebulosity, not the dark lane
    assert mask[:, : int(600 * 0.7)].mean() > mask[:, int(600 * 0.8) :].mean()


def test_a_compact_subject_is_untouched_by_its_own_expectation():
    """Prior proposes, measurement disposes: when the measured support already
    meets or beats the catalogued extent, nothing changes."""
    a = RNG.normal(0.02, 0.002, (400, 600))
    a[180:220, 280:340] += 0.08
    base = compute_support(a)[1]
    withexp = compute_support(a, expected_frac=0.01)[1]
    assert withexp["source"] == base["source"] == "starless_sigma"
    assert withexp["area_frac"] == base["area_frac"]


def test_disconnected_speckle_is_dropped_from_the_support():
    """Detector artefacts (the S50 amp-glow speckle) must not become support, or
    faint-structure retention could score on hardware noise."""
    from seestar.measure import _drop_speckle

    m = np.zeros((400, 600), dtype=bool)
    m[100:160, 100:200] = True  # one real 6000 px subject
    rng = np.random.default_rng(3)
    ys = rng.integers(0, 399, 200)
    xs = rng.integers(0, 599, 200)
    m[ys, xs] = True  # 200 single-pixel specks
    cleaned, islands, removed = _drop_speckle(m)
    assert cleaned[100:160, 100:200].all()  # the real subject survives intact
    assert islands >= 150 and removed > 0
    assert cleaned.sum() < m.sum()


def test_speckle_floor_keeps_a_small_but_real_subject_on_every_camera():
    """The floor is ABSOLUTE pixels, not a frame fraction: the same small subject
    must survive on the S30 (2.1 Mpx) and the S30 Pro (8.3 Mpx) alike. A frame
    fraction made the Pro floor 4147 px -- larger than the 1500 px validity floor
    -- which deleted a real 3025 px subject outright."""
    from seestar.measure import _drop_speckle

    for dims in ((1080, 1920), (2160, 3840)):
        m = np.zeros(dims, dtype=bool)
        m[500:530, 900:960] = True  # 1800 px small galaxy
        m[5, 5] = True  # plus a speck, so the floor really is exercised
        cleaned, islands, _ = _drop_speckle(m)
        assert cleaned.sum() == 1800, f"{dims}: real subject deleted"
        assert islands == 1  # only the speck went


def test_catalogued_extent_frac_is_none_without_usable_inputs():
    from seestar.measure import catalogued_extent_frac

    assert catalogued_extent_frac(None, None, (1080, 1920), 3.99) is None
    assert catalogued_extent_frac(120, 30, (1080, 1920), None) is None
    assert catalogued_extent_frac(120, 30, (0, 0), 3.99) is None
    # pin the VALUE, not just its range: an arcmin/arcsec slip or a
    # radius-vs-diameter swap would otherwise pass unnoticed.
    # 120'x30' ellipse = pi*60*15 = 2827 sq'; S30 frame = 71.8' x 127.7' = 9169 sq'
    assert abs(catalogued_extent_frac(120, 30, (1080, 1920), 3.99) - 0.3083) < 0.002
    # the same object on the wider S30 Pro field covers proportionally less
    assert abs(catalogued_extent_frac(120, 30, (2160, 3840), 3.74) - 0.0877) < 0.002


def test_sky_is_measured_outside_the_support_when_one_exists(tmp_path):
    """The two-point solve's sky must not be read from inside the subject."""
    from seestar.execute import _sky_metric

    h, w = 100, 100
    img = np.full((h, w, 3), 20, dtype=np.uint8)
    img[:70, :, :] = 200  # bright subject over 70% of the frame
    jpg = tmp_path / "probe.jpg"
    Image.fromarray(img).save(jpg, quality=95)
    sup = np.zeros((h, w), dtype=bool)
    sup[:70, :] = True
    sp = tmp_path / "sup.png"
    Image.fromarray(sup.astype(np.uint8) * 255).save(sp)

    globally = _sky_metric(str(jpg), None)
    outside = _sky_metric(str(jpg), str(sp))
    assert globally > 150  # the global median sits INSIDE the subject
    assert outside < 60  # measured on real sky instead


def test_sky_falls_back_to_global_when_support_leaves_no_sky(tmp_path):
    from seestar.execute import _sky_metric

    h, w = 100, 100
    img = np.full((h, w, 3), 90, dtype=np.uint8)
    jpg = tmp_path / "p.jpg"
    Image.fromarray(img).save(jpg, quality=95)
    sup = np.ones((h, w), dtype=bool)  # support covers everything
    sp = tmp_path / "s.png"
    Image.fromarray(sup.astype(np.uint8) * 255).save(sp)
    assert _sky_metric(str(jpg), str(sp)) == _sky_metric(str(jpg), None)


def test_support_code_does_not_import_scipy():
    """Both packaged builds pass `--exclude-module scipy` (packaging/build_app.sh,
    build_app_windows.ps1) and scipy is not in pyproject dependencies, so a scipy
    import here would pass every dev-venv test and then crash the shipped app at
    the support measurement. Guard the whole shipped package."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "seestar"
    offenders = []
    for py in root.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("import scipy", "from scipy")):
                offenders.append(f"{py.relative_to(root)}:{i}")
    assert not offenders, f"scipy is excluded from the packaged app: {offenders}"


def _reference_label(mask):
    """Independent 4-connected labelling by explicit flood fill -- deliberately a
    DIFFERENT algorithm from the run-based union-find under test."""
    from collections import deque

    h, w = mask.shape
    seen = np.zeros((h, w), dtype=np.int32)
    nxt = 0
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            nxt += 1
            q = deque([(y, x)])
            seen[y, x] = nxt
            while q:
                cy, cx = q.popleft()
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = nxt
                        q.append((ny, nx))
    return seen, nxt


def test_numpy_labeling_matches_an_independent_flood_fill():
    """Partition equality against a different algorithm -- island count AND the
    multiset of island sizes, over several fill densities."""
    from seestar.measure import _label_4connected

    rng = np.random.default_rng(7)
    for density in (0.3, 0.55, 0.75):
        m = rng.random((60, 90)) < density
        labels, counts = _label_4connected(m)
        ref, n_ref = _reference_label(m)
        assert len(counts) - 1 == n_ref, f"island count differs at density {density}"
        mine = np.sort(counts[1:])
        theirs = np.sort(np.bincount(ref.ravel())[1:])
        assert np.array_equal(mine, theirs), f"island sizes differ at density {density}"
        assert (labels > 0).sum() == m.sum()
        assert labels[~m].max(initial=0) == 0


def test_labeling_joins_an_l_shaped_region_and_splits_a_diagonal_one():
    """4-connectivity: an L is ONE island; diagonal-only touching is TWO."""
    from seestar.measure import _label_4connected

    m = np.zeros((6, 6), dtype=bool)
    m[1, 1:4] = True
    m[1:4, 3] = True  # L shape, 4-connected throughout
    labels, counts = _label_4connected(m)
    assert len(counts) - 1 == 1

    d = np.zeros((6, 6), dtype=bool)
    d[1, 1] = True
    d[2, 2] = True  # touch only at a corner
    labels2, counts2 = _label_4connected(d)
    assert len(counts2) - 1 == 2


def test_frame_filling_declines_a_smooth_gradient_even_with_an_expectation():
    """A catalogue hit says the subject SHOULD be large; it cannot say the bright
    region is astronomical. A pure ramp (amp glow / uncorrected sky) must never
    become 'subject' just because an expectation exists -- without this guard the
    dark anchor claimed 61% of a gradient-only frame."""
    h, w = 800, 1200
    rng = np.random.default_rng(5)
    grad = 0.02 + 0.05 * np.linspace(0, 1, w)[None, :] + rng.normal(0, 0.002, (h, w))
    mask, info = compute_support(grad, expected_frac=0.30)
    assert info["source"] != "starless_dark_anchored"
    assert "declined" in info["detail"]  # the decision is recorded, never silent


def test_frame_filling_declines_when_it_would_claim_the_whole_frame():
    a = np.full((300, 400), 0.5) + RNG.normal(0, 0.0005, (300, 400))
    a[10:290, 10:390] += 0.2  # essentially everything is "bright"
    _mask, info = compute_support(a, expected_frac=0.95)
    assert info["source"] != "starless_dark_anchored"


def test_zero_pixel_support_after_cleaning_is_reported_invalid():
    """The validity floor must be applied AFTER speckle removal: cleaning a
    just-passed support down to nothing while still claiming valid publishes an
    all-black raster and makes QA report a promised-artifact failure instead of an
    honest empty field."""
    noise = np.random.default_rng(5).normal(0.02, 0.002, (1080, 1920))
    mask, info = compute_support(noise)
    assert info["valid"] is False
    assert info["area_frac"] == 0.0 and mask.sum() == 0


def test_support_measurement_receives_the_catalogued_expectation(monkeypatch, tmp_path):
    """Integration: the shell threads ExecContext.subject_extent_frac all the way
    into the support measurement (orchestrate -> execute -> measure)."""
    import seestar.execute as E
    from seestar.adapters.base import ExecContext
    from seestar.core.types import ArtifactRef, ArtifactRole, Measure, MeasureKind

    seen = {}

    def fake_measure(linear_fit, workdir, target, tools, support_png, expected_frac=None):
        seen["expected_frac"] = expected_frac
        Image.fromarray(np.ones((8, 8), dtype=np.uint8) * 255).save(support_png)
        return np.ones((8, 8), dtype=bool), {
            "area_frac": 1.0,
            "dims": (8, 8),
            "source": "starless_sigma",
            "valid": True,
            "detail": "x",
        }

    monkeypatch.setattr("seestar.measure.measure_subject_support", fake_measure)

    class _WS:
        scratch_dir = str(tmp_path)
        output_dir = str(tmp_path)

        def path_for(self, ref, ext):
            return str(tmp_path / f"{ref.name}{ext}")

        def find_producer(self, ref):
            return str(tmp_path / "linear.fit")

        def register(self, *a, **kw):
            pass

    ref = ArtifactRef("linear_master", ArtifactRole.PRODUCTION)
    out = ArtifactRef("T_support", ArtifactRole.QA)
    ctx = ExecContext(workspace=_WS(), tools=None, resolve={"__target__": "T"})
    ctx.subject_extent_frac = 0.3083
    E.take_measurement(Measure(MeasureKind.SUBJECT_SUPPORT, ref=ref, output=out), ctx, {}, None)
    assert seen["expected_frac"] == 0.3083
