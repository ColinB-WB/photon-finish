"""QA unit tests -- pure functions only (no Siril, no file IO).

Encodes the M8 guardrail lessons: mask the object, separate body from background,
and catch the orientation flip the numbers missed.
"""

import os

import numpy as np
import pytest
from PIL import Image

from seestar.qa import measure_render, object_mask, orientation_match


def _synthetic():
    """Dark sky + a bright extended blob (the 'nebula') + scattered point stars,
    with the blob OFF-CENTER and asymmetric so flips are distinguishable."""
    h, w = 320, 180
    img = np.zeros((h, w), np.float64)
    yy, xx = np.mgrid[0:h, 0:w]
    # extended blob in the upper third (asymmetric vertically)
    blob = 120.0 * np.exp(-(((yy - 90) ** 2 + (xx - 90) ** 2) / (2 * 35.0**2)))
    img += blob
    rng = np.random.default_rng(0)
    for _ in range(120):
        y, x = rng.integers(0, h), rng.integers(0, w)
        img[y, x] = 255.0
    return np.clip(img, 0, 255)


def test_object_mask_separates_blob_from_background():
    luma = _synthetic()
    mask, bgmed, _ = object_mask(luma, blur=4)
    assert 0.01 < mask.mean() < 0.5  # a real subject, not whole frame
    assert luma[mask].mean() > luma[~mask].mean() + 20  # body clearly brighter
    assert bgmed < 10  # background measured as dark


def test_orientation_detects_vertical_flip():
    luma = _synthetic()
    same = orientation_match(luma, luma)
    assert same["ok"] and same["best"] == "identity"
    flipped = orientation_match(luma, np.flipud(luma))
    assert not flipped["ok"]  # a flip is a hard fail
    assert flipped["best"] == "flipud"


def test_global_black_high_but_body_bright_is_fine(tmp_path):
    """The touched-up-S30 case: most of the frame is near-black, yet the masked body
    is bright. Global black fraction must NOT condemn it; body metrics carry signal."""
    luma = _synthetic()
    rgb = np.stack([luma, luma * 0.5, luma * 0.5], axis=-1).astype(np.uint8)
    p = tmp_path / "synthetic.png"
    Image.fromarray(rgb).save(p)
    m = measure_render(str(p))
    assert m["global_black_frac"] > 0.6  # mostly near-black globally
    assert m["body_p90"] > 40  # but the body is bright
    assert m["background_median_outside_mask"] < 10


def _rgb_from(luma, r=1.0, g=1.0, b=1.0):
    return np.clip(np.stack([luma * r, luma * g, luma * b], axis=-1), 0, 255).astype(np.uint8)


def test_core_saturation_low_when_core_burns_pale():
    """A bright core that is near-neutral (pale/white) reads LOW core saturation,
    even if the surrounding body is colored red."""
    from seestar.qa import core_star_saturation, object_mask

    luma = _synthetic()
    mask, _, _ = object_mask(luma, blur=4)
    # red body, but the brightest pixels are pale (all channels near-equal & high)
    rgb = _rgb_from(luma, r=1.0, g=0.35, b=0.30)
    core_pixels = luma >= np.percentile(luma[mask], 95)
    rgb[core_pixels] = [250, 245, 245]  # pale core
    core_med, _star, _v = core_star_saturation(rgb, luma, mask, core_pct=95.0)
    assert core_med < 0.15, f"pale core should read low S, got {core_med}"


def test_core_saturation_high_when_core_stays_red():
    """A core that stays saturated red (the M8 complaint) reads HIGH core saturation."""
    from seestar.qa import core_star_saturation, object_mask

    luma = _synthetic()
    mask, _, _ = object_mask(luma, blur=4)
    rgb = _rgb_from(luma, r=1.0, g=0.30, b=0.25)  # whole body incl. core stays red
    core_med, _star, _v = core_star_saturation(rgb, luma, mask, core_pct=95.0)
    assert core_med > 0.5, f"red core should read high S, got {core_med}"


def test_star_saturation_measures_pixels_outside_the_body_mask():
    """Field stars (outside the body mask) that are tinted read elevated star S;
    white stars read ~0. The body mask excludes them, so this is a distinct region."""
    from seestar.qa import core_star_saturation, object_mask

    luma = _synthetic()
    mask, _, _ = object_mask(luma, blur=4)
    white = _rgb_from(luma, 1.0, 1.0, 1.0)
    tinted = _rgb_from(luma, 1.0, 0.4, 0.4)
    _c, star_white, _v = core_star_saturation(white, luma, mask)
    _c2, star_tinted, _v2 = core_star_saturation(tinted, luma, mask)
    assert star_white < 0.1 < star_tinted, f"white={star_white} tinted={star_tinted}"


def test_core_value_median_distinguishes_luminous_from_dim_core():
    """Low core saturation alone can't tell a PALE LUMINOUS core (high V, low S) from a
    DEAD GRAY one (low V, low S) -- so the instrument also reports core luminance.
    A pale core reads high core_value; the same geometry at a quarter the luma reads low."""
    from seestar.qa import core_star_saturation, object_mask

    luma = _synthetic()  # blob peaks ~120
    mask, _, _ = object_mask(luma, blur=4)
    _s, _star, v_bright = core_star_saturation(_rgb_from(luma), luma, mask)
    assert v_bright > 90, f"brightest-percentile core of a ~120-peak blob should be high, got {v_bright}"
    dim = np.clip(luma * 0.25, 0, 255)  # same mask geometry, far dimmer
    _s2, _star2, v_dim = core_star_saturation(_rgb_from(dim), dim, mask)
    assert v_dim < v_bright, f"dim core {v_dim} should read below bright core {v_bright}"


def test_measure_render_emits_the_new_metrics(tmp_path):
    luma = _synthetic()
    rgb = _rgb_from(luma, 1.0, 0.5, 0.5)
    p = tmp_path / "m.png"
    Image.fromarray(rgb).save(p)
    m = measure_render(str(p))
    for k in ("core_saturation_median", "star_saturation_median", "core_value_median"):
        assert k in m, f"missing {k}"
    assert 0.0 <= m["core_saturation_median"] <= 1.0
    assert 0.0 <= m["star_saturation_median"] <= 1.0
    assert 0.0 <= m["core_value_median"] <= 255.0


def test_body_saturation_median_is_monotonic_in_chroma(tmp_path):
    """Scaling chroma DOWN never increases the measured body_saturation_median.
    This is the premise the saturation backoff depends on (lower satu -> lower
    measured S). A pure metric property -- no Siril."""
    luma = _synthetic()
    prev = None
    for scale in (1.0, 0.7, 0.4, 0.1):  # 1.0 = full red, ->0 = neutral
        g = 0.30 + (1.0 - 0.30) * (1.0 - scale)  # raise G/B toward luma as scale drops
        rgb = _rgb_from(luma, r=1.0, g=g, b=g)
        p = tmp_path / f"s{scale}.png"
        Image.fromarray(rgb).save(p)
        s = measure_render(str(p))["body_saturation_median"]
        if prev is not None:
            assert s <= prev + 1e-9, f"scale {scale}: S {s} rose above {prev}"
        prev = s


def test_absolute_metrics_labels_actual_star_source(tmp_path):
    # qa_target passes the stars-on master for showcase renders; the label must
    # name the file actually measured, not a hardcoded "final.tif".
    from seestar.qa import absolute_metrics

    out = absolute_metrics(str(tmp_path / "X1_stars-on.tif"), tools=None)
    assert out["star_count"]["source"] == "X1_stars-on.tif"
    assert out["fwhm_px"]["source"] == "X1_stars-on.tif"


def test_solver_warnings_pure():
    """Ported from the deleted tests/test_solver.py (Task 23 coverage-loss fix):
    solver_warnings is a PURE function (no core/solve.py state-machine law, so it
    was never going to be superseded by test_core_solve.py) with its own
    production callers (cli.py, qa.py::qa_target) -- it needed its own new home."""
    from seestar.qa import solver_warnings

    assert solver_warnings(None) == []
    assert solver_warnings({"pinned": {"mtf": None, "mtf_converged": True, "bp": False}}) == []
    pinned = solver_warnings(
        {
            "metric": "body_p90",
            "target": 72,
            "achieved": 45.2,
            "pinned": {"mtf": "lo", "mtf_converged": False, "bp": False},
        }
    )
    assert len(pinned) == 1 and "PINNED at mtf lo" in pinned[0]
    # non-pinned non-convergence (the M100 small-subject overshoot) also warns
    wobble = solver_warnings(
        {
            "metric": "body_p90",
            "target": 72,
            "achieved": 88.2,
            "pinned": {"mtf": None, "mtf_converged": False, "bp": False},
        }
    )
    assert len(wobble) == 1 and "did NOT converge" in wobble[0]
    both = solver_warnings(
        {
            "metric": "body_p90",
            "target": 72,
            "achieved": 60,
            "sky": 25,
            "pinned": {"mtf": "lo", "mtf_converged": False, "bp": True},
        }
    )
    assert len(both) == 2 and "bp < sky-median" in both[1]


def test_star_count_returns_none_none_when_run_siril_raises(monkeypatch):
    """qa.star_count is a soft measurement -- a raising run_siril (findstar tool
    unavailable/crashed) must degrade to (None, None), never propagate. The GATE
    policy decision (fail-closed vs skip) belongs entirely to evaluate_gates/score,
    not to this measurement function."""
    from conftest import FakeTools

    import seestar.adapters.siril as siril_module

    def _boom(lines, tools):
        raise RuntimeError("simulated findstar crash")

    monkeypatch.setattr(siril_module, "run_siril", _boom)
    from seestar.qa import star_count

    assert star_count("/nonexistent/whatever.fit", FakeTools()) == (None, None)


def test_score_hard_fails_when_active_star_floor_has_no_measurement(tmp_path, monkeypatch):
    """M2: `score()`'s own (deprecated but still-live) `star_floor` check must fail
    closed too -- an active floor with an unavailable measurement is a hard failure,
    never a silent pass. Uses a real tiny jpg (score reads real pixels) with
    run_siril raising so star_count genuinely returns (None, None)."""
    from conftest import FakeTools

    import seestar.adapters.siril as siril_module

    def _boom(lines, tools):
        raise RuntimeError("simulated findstar crash")

    monkeypatch.setattr(siril_module, "run_siril", _boom)

    luma = _synthetic()
    rgb = np.stack([luma, luma * 0.5, luma * 0.5], axis=-1).astype(np.uint8)
    render = tmp_path / "render.png"
    Image.fromarray(rgb).save(render)

    from seestar.qa import score

    sc = score(str(render), star_floor=500, tools=FakeTools())
    assert sc["metrics"]["star_count"] is None
    assert any(f.startswith("star_count unavailable") for f in sc["hard_fails"]), sc["hard_fails"]
    assert sc["passed"] is False


def test_star_count_reraises_external_tool_timeout_instead_of_degrading(monkeypatch):
    """H1 Task 4: a bounded-runner timeout is a real production failure, not a
    'findstar unavailable' measurement gap -- star_count must propagate
    ExternalToolTimeout, never swallow it into (None, None) the way an ordinary
    findstar crash degrades (test_star_count_returns_none_none_when_run_siril_raises,
    above, is the contrasting normal-failure case and must stay unchanged)."""
    from conftest import FakeTools

    import seestar.adapters.siril as siril_module
    from seestar.adapters._external_process import ExternalToolTimeout
    from seestar.qa import star_count

    def _timeout(lines, tools):
        raise ExternalToolTimeout("Siril did not finish within 30 minutes (configured timeout 1800s).")

    monkeypatch.setattr(siril_module, "run_siril", _timeout)

    with pytest.raises(ExternalToolTimeout):
        star_count("/nonexistent/whatever.fit", FakeTools())


def test_input_star_metrics_reraises_external_tool_timeout_from_direct_probe(monkeypatch, tmp_path):
    """The unguarded top-level star_count(linear_fit, tools) call (the "direct"
    probe) must let ExternalToolTimeout through rather than being caught anywhere
    in input_star_metrics."""
    from conftest import FakeTools

    import seestar.adapters.siril as siril_module
    from seestar.adapters._external_process import ExternalToolTimeout
    from seestar.qa import input_star_metrics

    def _timeout(lines, tools):
        raise ExternalToolTimeout("Siril did not finish within 30 minutes (configured timeout 1800s).")

    monkeypatch.setattr(siril_module, "run_siril", _timeout)
    linear_fit = str(tmp_path / "in.fit")
    open(linear_fit, "wb").close()

    with pytest.raises(ExternalToolTimeout):
        input_star_metrics(linear_fit, str(tmp_path), FakeTools())


def test_input_star_metrics_reraises_external_tool_timeout_from_autostretch_probe(monkeypatch, tmp_path):
    """The direct findstar probe genuinely measures a low (but real, <20) star
    count first (no raise), so execution reaches the autostretch fallback probe
    -- ExternalToolTimeout raised there must also propagate, not degrade to {}."""
    from conftest import FakeTools

    import seestar.adapters.siril as siril_module
    from seestar.adapters._external_process import ExternalToolTimeout
    from seestar.qa import input_star_metrics

    calls = {"n": 0}

    def _fake_run_siril(lines, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Found 5 Gaussian profile stars, FWHM 2.50"  # cnt=5 < 20 -> falls through to the probe
        raise ExternalToolTimeout("Siril did not finish within 30 minutes (configured timeout 1800s).")

    monkeypatch.setattr(siril_module, "run_siril", _fake_run_siril)
    linear_fit = str(tmp_path / "in.fit")
    open(linear_fit, "wb").close()

    with pytest.raises(ExternalToolTimeout):
        input_star_metrics(linear_fit, str(tmp_path), FakeTools())
    assert calls["n"] == 2, "expected the direct probe to succeed, then the autostretch probe to time out"


def test_qa_target_adds_hero_outcome_for_non_converged_solve(tmp_path, monkeypatch):
    import seestar.qa as qa_module

    proc = tmp_path
    monkeypatch.setattr(qa_module, "resolve_tools", lambda cfg: None)
    monkeypatch.setattr(qa_module, "read_xmp_subject", lambda render, cfg, tools=None: "nebula, LP, profile:showcase")
    monkeypatch.setattr(
        qa_module,
        "score",
        lambda render, **kw: {
            "render": "T_final.jpg",
            "metrics": {
                "dimensions": [100, 100],
                "body_p90": 64.8,
                "star_count": 5000,
                "fwhm": 2.7,
                "core_sat_frac": 0.0,
                "background_median_outside_mask": 11.0,
                "near_black_frac_outside_mask": 0.0,
                "pegged_chroma_frac_body": 0.0,
                "envelope_area_frac": 0.01,
            },
            "scores": {},
            "hard_fails": [],
            "warnings": [],
            "passed": True,
        },
    )
    monkeypatch.setattr(
        qa_module,
        "read_run_notes",
        lambda proc_dir, target, **kw: {
            "notes": {
                "stretch_solve": {
                    "metric": "body_p90",
                    "target": 72.0,
                    "achieved": 64.8,
                    "pinned": {"mtf": None, "mtf_converged": False, "bp": False},
                },
                "input_quality": {},
            }
        },
    )
    cfg = {"qa": {"gates": {"enabled": True, "hero_contract_tolerance": {"nebula": 2.0}}}}
    sc = qa_module.qa_target(str(proc), cfg, target="T")
    assert sc["hero"]["outcome"] == "processing_failed"
    assert "did not converge" in " ".join(sc["hero"]["reasons"])


def test_qa_target_excuses_source_limited_fwhm_but_not_chroma(tmp_path, monkeypatch):
    import seestar.qa as qa_module

    monkeypatch.setattr(qa_module, "resolve_tools", lambda cfg: None)
    monkeypatch.setattr(qa_module, "read_xmp_subject", lambda render, cfg, tools=None: "nebula, LP, profile:showcase")
    monkeypatch.setattr(
        qa_module,
        "score",
        lambda render, **kw: {
            "render": "T_final.jpg",
            "metrics": {
                "dimensions": [100, 100],
                "body_p90": 57.2,
                "star_count": 2583,
                "fwhm": 5.90,
                "core_sat_frac": 0.0,
                "background_median_outside_mask": 9.2,
                "near_black_frac_outside_mask": 0.006,
                "pegged_chroma_frac_body": 0.2607,
                "envelope_area_frac": 0.075,
            },
            "scores": {},
            "hard_fails": [],
            "warnings": [],
            "passed": True,
        },
    )
    monkeypatch.setattr(
        qa_module,
        "read_run_notes",
        lambda proc_dir, target, **kw: {
            "notes": {
                "stretch_solve": {
                    "metric": "body_p90",
                    "target": 66.2,
                    "achieved": 66.1,
                    "pinned": {"mtf": None, "mtf_converged": True, "bp": False},
                },
                "input_quality": {"fwhm_px": 5.504, "measured_on": "linear"},
            }
        },
    )
    cfg = {
        "qa": {
            "gates": {
                "enabled": True,
                "fwhm_ceiling": {"nebula": 3.3},
                "pegged_chroma_ceiling": {"nebula": 0.20},
                "hero_contract_tolerance": {"nebula": 2.0},
            }
        }
    }
    sc = qa_module.qa_target(str(tmp_path), cfg, target="T")
    assert sc["hero"]["outcome"] == "processing_failed"
    assert sc["hero"]["processing_failed_gates"] == ["pegged_chroma"]
    assert sc["hero"]["source_limited_gates"] == ["fwhm"]


def test_resolve_stars_on_tif_prefers_run_manifest_over_stale_filename_guess(tmp_path):
    """2026-08-01 bug: a folder can carry a `_stars-on.tif` left over from an older
    run/naming convention (conservative_star_hero now ships stars-on AS `_final`,
    so a fresh run never writes a `_stars-on.tif` at all). qa_target must resolve
    THIS run's stars-on master from its own manifest, never re-measure whatever
    stale file happens to share that guessed name."""
    import json

    from seestar.qa import _resolve_stars_on_tif

    proc = tmp_path
    final_tif = str(proc / "T_final.tif")
    open(final_tif, "wb").close()
    # a stale leftover from a much older run, using the pre-Phase-3A naming
    stale = proc / "T_stars-on.tif"
    stale.write_bytes(b"stale bytes from an old run")
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",
        "deliverables": [
            {
                "variant": "stars-on",
                "tif": final_tif,
                "png": str(proc / "T_final.png"),
                "jpg": str(proc / "T_final.jpg"),
            },
            {"variant": "star-reduced", "tif": str(proc / "T_star-reduced.tif")},
        ],
    }
    (proc / "T_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    resolved = _resolve_stars_on_tif(str(proc), "T", final_tif)
    assert resolved == final_tif  # never the stale T_stars-on.tif guess


def test_resolve_stars_on_tif_falls_back_to_filename_guess_without_manifest(tmp_path):
    """No {target}_run.json (pre-manifest legacy render, or a standalone `seestar
    qa` scan): the old best-effort filename guess is the only option left, and
    stays exactly as it was."""
    from seestar.qa import _resolve_stars_on_tif

    proc = tmp_path
    final_tif = str(proc / "T_final.tif")
    open(final_tif, "wb").close()
    stars_on = proc / "T_stars-on.tif"
    stars_on.write_bytes(b"a real stars-on master, no manifest present")

    resolved = _resolve_stars_on_tif(str(proc), "T", final_tif)
    assert resolved == str(stars_on)


def test_resolve_stars_on_tif_manifest_without_stars_on_variant_uses_final(tmp_path):
    """A starfield render's manifest never has a 'stars-on' variant at all -- the
    manifest is still authoritative, so this must resolve to final, not fall back
    to a filename guess that could pick up unrelated stale litter."""
    import json

    from seestar.qa import _resolve_stars_on_tif

    proc = tmp_path
    final_tif = str(proc / "T_final.tif")
    open(final_tif, "wb").close()
    manifest = {"manifest_format": 1, "outcome": "pass", "deliverables": [{"variant": "final", "tif": final_tif}]}
    (proc / "T_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    resolved = _resolve_stars_on_tif(str(proc), "T", final_tif)
    assert resolved == final_tif


def _minimal_qa_target_cfg():
    """Gates OFF -- these tests are about namespace resolution/publication
    reporting, not gate specifics."""
    return {"qa": {"gates": {"enabled": False}}}


def _isolate_qa_target(monkeypatch):
    """The :341 pattern: fake the tool resolution and the render read so
    `qa_target` never shells out or decodes real pixels -- only namespace
    resolution and manifest bookkeeping are under test here."""
    import seestar.qa as qa_module

    monkeypatch.setattr(qa_module, "resolve_tools", lambda cfg: None)
    monkeypatch.setattr(qa_module, "read_xmp_subject", lambda render, cfg, tools=None: "")
    monkeypatch.setattr(
        qa_module,
        "score",
        lambda render, **kw: {
            "render": os.path.basename(render),
            "metrics": {},
            "scores": {},
            "hard_fails": [],
            "warnings": [],
            "passed": True,
        },
    )


def test_qa_target_evaluates_uncertified_only_set(tmp_path, monkeypatch):
    """A directory that only ever published UNCERTIFIED (every attempt was
    processing_failed): qa_target resolves the uncertified namespace, scores
    it, and reports BOTH the manifest's own outcome and this pass's recomputed
    one -- never asserting they agree (spec rev 6 T11)."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    (proc / "M_uncertified_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert sc["publication"]["certified"] is False
    assert sc["publication"]["manifest_outcome"] == "processing_failed"
    assert "recomputed_outcome" in sc["publication"]
    assert (proc / "M_uncertified_qa.json").exists()
    assert not (proc / "M_qa.json").exists()


def test_qa_target_uncertified_missing_referenced_notes_fails_closed(tmp_path, monkeypatch):
    """Same dir, minus the notes sidecar the manifest itself references: the
    manifest's referential integrity is a FAIL-CLOSED, NAMED hard failure --
    never a silent skip and never a crash."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    # M_uncertified_notes.json deliberately NOT written.
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert sc["passed"] is False
    assert any("M_uncertified_notes.json" in f for f in sc["hard_fails"])


def test_qa_target_certified_set_unchanged(tmp_path, monkeypatch):
    """Regression guard: a normal, fully self-consistent CERTIFIED directory
    behaves exactly as it did before namespace resolution existed -- no
    `publication` key, the plain (unnamespaced) qa sidecar name."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_final.jpg", "wb").close()
    open(proc / "M_final.tif", "wb").close()
    (proc / "M_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "pass",
        "deliverables": [{"variant": "final", "jpg": "M_final.jpg", "tif": "M_final.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_notes.json",
    }
    (proc / "M_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert "publication" not in sc
    assert (proc / "M_qa.json").exists()


def test_qa_target_both_sets_reports_certified_and_notes_uncertified(tmp_path, monkeypatch):
    """Both a certified and an uncertified publication set exist side by side
    (e.g. an older accepted run plus a newer failed re-render attempt): the
    certified set is always the one reported, but the uncertified sibling's
    presence is surfaced as a warning, never silently ignored (spec §7)."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_final.jpg", "wb").close()
    open(proc / "M_final.tif", "wb").close()
    (proc / "M_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "pass",
        "deliverables": [{"variant": "final", "jpg": "M_final.jpg", "tif": "M_final.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_notes.json",
    }
    (proc / "M_run.json").write_text(json.dumps(manifest), encoding="utf-8")
    # A stale/failed uncertified attempt also present alongside it.
    (proc / "M_uncertified_run.json").write_text(
        json.dumps({"manifest_format": 1, "outcome": "processing_failed", "deliverables": []}), encoding="utf-8"
    )

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert "publication" not in sc
    assert any("uncertified" in w for w in sc["warnings"])


def test_qa_target_corrupt_certified_manifest_degrades_to_next_candidate(tmp_path, monkeypatch):
    """Fix round 1, item 1(a): a manifest file that EXISTS but fails to parse
    must not abort resolution straight to legacy -- the NEXT candidate
    (certified first, then uncertified) is tried, and a valid uncertified
    manifest still resolves after a corrupt certified one. The corrupt manifest
    is named in a delivered warning, never silently dropped."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    (proc / "M_run.json").write_text("{not valid json", encoding="utf-8")  # corrupt CERTIFIED manifest

    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    (proc / "M_uncertified_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert sc["publication"]["certified"] is False
    assert sc["publication"]["manifest_outcome"] == "processing_failed"
    assert any("M_run.json" in w and "could not be parsed" in w for w in sc["warnings"])
    assert (proc / "M_uncertified_qa.json").exists()


def test_qa_target_corrupt_uncertified_manifest_alone_degrades_to_legacy_with_warning(tmp_path, monkeypatch):
    """Fix round 1, item 1(b): a corrupt M_uncertified_run.json with NO
    certified manifest present has no next candidate to fall through to (the
    uncertified slot is the last one tried) -- resolution degrades to the
    ordinary pre-namespace LEGACY file names (`{target}_final.*`), exactly as
    when no manifest exists at all, but carrying the corrupt-manifest warning.

    Chosen semantics (documented here per the task's ambiguity note): the real
    (if now unreachable) failed-publication rasters on disk are named
    `M_uncertified.*`, which legacy resolution does NOT look for -- it looks for
    `M_final.*`. Without a legacy-named raster present, `qa_target` would crash
    inside `score()`/`PIL.Image.open` trying to read a nonexistent `M_final.jpg`
    -- that IS the pre-existing legacy crash behavior, unchanged and out of
    scope for this fix (the task explicitly permits either construction). This
    test plants BOTH the uncertified rasters (representing the real failed
    attempt) and a legacy-named raster set (so resolution can actually produce a
    report to assert against), and asserts only the corrupt-manifest warning and
    the plain unnamespaced qa sidecar name -- no namespace-specific behavior
    applies once resolution has degraded to legacy."""
    from seestar.qa import qa_target

    proc = tmp_path
    (proc / "M_uncertified_run.json").write_text("{not valid json", encoding="utf-8")
    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    # Legacy-named rasters so the degraded-to-legacy resolution can produce a
    # report at all (see docstring) -- without these, score() would crash.
    open(proc / "M_final.jpg", "wb").close()
    open(proc / "M_final.tif", "wb").close()

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert "publication" not in sc  # legacy resolution never namespaces
    assert any("M_uncertified_run.json" in w and "could not be parsed" in w for w in sc["warnings"])
    assert (proc / "M_qa.json").exists()  # legacy (unnamespaced) qa sidecar name


def test_qa_target_uncertified_qualifies_inconsistency_when_category_unknown(tmp_path, monkeypatch):
    """Fix round 1, item 4: when the RECOMPUTATION couldn't genuinely evaluate
    quality (category unreadable from EXIF -- every category-aware gate is
    SKIPPED), a manifest/recomputed outcome mismatch is qualified as a warning,
    never asserted as a hard 'publication inconsistency' failure: the
    recomputation didn't judge anything, so a disagreement isn't evidence of
    anything either way (spec §7 -- never asserted or forced in either
    direction).

    `passed` below is VERIFIED, not assumed: with category unknown, every
    category-aware gate SKIPS entirely (`core/qa_rules.py::evaluate_gates`
    short-circuits on `Category.UNKNOWN` before the star_count/fwhm M2
    fail-closed exception is ever reached -- that exception only applies to a
    KNOWN category with a missing measurement, per `PerCategory.for_category`
    refusing to serve UNKNOWN). Nothing else in this scenario fails either, so
    `passed` is honestly True -- the task note's guess of "still False (fail-
    closed via M2)" does not hold for a genuinely unknown category and is
    corrected here."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    (proc / "M_uncertified_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",  # historical
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)  # read_xmp_subject -> "" -> category unreadable
    cfg = _minimal_qa_target_cfg()
    cfg["qa"]["gates"]["enabled"] = True
    cfg["qa"]["gates"]["star_preservation"] = {"min_retention": 0.5, "catastrophe_floor": 20}
    cfg["qa"]["gates"]["fwhm_ceiling"] = {"nebula": 3.3, "starfield": 3.6, "galaxy": 3.5}
    sc = qa_target(str(proc), cfg, target="M")

    assert sc["publication"]["manifest_outcome"] == "processing_failed"
    assert sc["publication"]["recomputed_outcome"] == "pass"  # nothing to judge -> no failing gate
    assert not any(f.startswith("publication inconsistency:") for f in sc["hard_fails"])
    assert any("publication inconsistency not asserted" in w and "category unknown" in w for w in sc["warnings"])
    assert sc["passed"] is True


def test_qa_target_uncertified_qualifies_inconsistency_when_gates_disabled(tmp_path, monkeypatch):
    """Fix round 2, item 1: `evaluable` must be derived from whether the gate
    report itself judged anything (any non-SKIPPED verdict), never from
    category presence alone. A KNOWN category (pinned via XMP to "nebula")
    with `qa.gates.enabled: false` short-circuits
    `core/qa_rules.py::evaluate_gates` (`not thresholds.enabled`) to an
    all-SKIPPED report carrying ZERO warnings (`_skipped_report(category,
    ())` -- unlike the UNKNOWN-category branch, which at least warns). The
    OLD guard (`cat is not None`) read True here and wrongly asserted a
    manifest/recomputed mismatch as a hard 'publication inconsistency' with
    no warning at all explaining why -- exactly the false accusation the
    qualified path exists to prevent. The fixed guard reads the gate report's
    own verdicts and qualifies this the same way an unknown category is
    qualified."""
    import json

    import seestar.qa as qa_module
    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_uncertified.jpg", "wb").close()
    open(proc / "M_uncertified.tif", "wb").close()
    (proc / "M_uncertified_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",  # historical
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    # Category IS readable (pinned to a real value), unlike the unknown-category
    # test above -- the disabled-gate-set case is the distinct failure mode.
    monkeypatch.setattr(qa_module, "read_xmp_subject", lambda render, cfg, tools=None: "nebula, LP, profile:showcase")
    cfg = _minimal_qa_target_cfg()  # qa.gates.enabled: False
    sc = qa_target(str(proc), cfg, target="M")

    assert sc["publication"]["manifest_outcome"] == "processing_failed"
    assert sc["publication"]["recomputed_outcome"] == "pass"  # nothing judged -> no failing gate
    assert not any(f.startswith("publication inconsistency:") for f in sc["hard_fails"])
    assert any("publication inconsistency not asserted" in w for w in sc["warnings"])
    assert sc["passed"] is True


def test_qa_target_resolves_hero_for_certified_non_final_out_name(tmp_path, monkeypatch):
    """Fix round 1, item 5: `_resolve_namespace` no longer assumes out_name
    "final" -- a certified `seestar run --profile natural` publishes
    `M_natural.*`, and a subsequent `seestar qa` must resolve THOSE names, not
    manufacture two false 'publication-referenced file missing: M_final.*' hard
    fails against a manifest that never had an M_final anything."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    open(proc / "M_natural.jpg", "wb").close()
    open(proc / "M_natural.tif", "wb").close()
    (proc / "M_notes.json").write_text(json.dumps({"notes": {}}), encoding="utf-8")
    manifest = {
        "manifest_format": 1,
        "outcome": "pass",
        "deliverables": [{"variant": "natural", "jpg": "M_natural.jpg", "tif": "M_natural.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_notes.json",
    }
    (proc / "M_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert "publication" not in sc  # certified
    assert not any("M_final" in f for f in sc["hard_fails"])
    assert sc["render"] == "M_natural.jpg"
    assert (proc / "M_qa.json").exists()  # certified sidecars are shared/unnamespaced (spec §1 caveat)


def test_qa_target_missing_hero_branch_reports_none_recomputed_outcome(tmp_path, monkeypatch):
    """Fix round 1, item 6: the missing-hero fail-closed branch (the hero raster
    itself is gone -- nothing downstream could be measured) must report
    `recomputed_outcome: None`, never a fabricated 'processing_failed' constant
    -- no recomputation actually ran. The manifest/None mismatch is qualified
    exactly like the main path's category-unknown case (parity, item 4): a
    warning, never an additional hard 'publication inconsistency' fail (this
    branch is already failed/closed on the missing file alone)."""
    import json

    from seestar.qa import qa_target

    proc = tmp_path
    # M_uncertified.jpg deliberately NOT written -- the hero raster is missing.
    manifest = {
        "manifest_format": 1,
        "outcome": "processing_failed",
        "deliverables": [{"variant": "final", "jpg": "M_uncertified.jpg", "tif": "M_uncertified.tif"}],
        "qa_artifacts": [],
        "notes_sidecar": "M_uncertified_notes.json",
    }
    (proc / "M_uncertified_run.json").write_text(json.dumps(manifest), encoding="utf-8")

    _isolate_qa_target(monkeypatch)
    sc = qa_target(str(proc), _minimal_qa_target_cfg(), target="M")
    assert sc["passed"] is False
    assert sc["publication"]["certified"] is False
    assert sc["publication"]["manifest_outcome"] == "processing_failed"
    assert sc["publication"]["recomputed_outcome"] is None
    assert not any(f.startswith("publication inconsistency:") for f in sc["hard_fails"])
    assert any("publication inconsistency not asserted" in w for w in sc["warnings"])
    assert any("M_uncertified.jpg" in f for f in sc["hard_fails"])
