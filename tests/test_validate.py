"""Back-validation harness (seestar validate): pure diff logic, staging, and
orchestration -- no Siril/GraXpert. The contract: an engine change is DONE only when
every available reference is identical-or-better; regressions are a lost `passed`,
a NEW gate-fail kind, or metric drift beyond the configured tolerances."""

import json
import os

import numpy as np
from astropy.io import fits

from seestar import validate as V


def _qa(
    star_count=1976,
    fwhm=4.195,
    body_p90=58.8,
    sky=14.73,
    envelope=0.0576,
    near_black=0.0,
    core_sat=0.0,
    pegged=0.01,
    faint=0.0123,
    passed=True,
    fails=None,
):
    return {
        "metrics": {
            "star_count": star_count,
            "fwhm": fwhm,
            "body_p90": body_p90,
            "body_p95": 92.1,
            "background_median_outside_mask": sky,
            "envelope_area_frac": envelope,
            "near_black_frac_outside_mask": near_black,
            "core_sat_frac": core_sat,
            "pegged_chroma_frac_body": pegged,
        },
        "absolute_gates": {"faint_retention": faint},
        "hard_fails": fails or [],
        "passed": passed,
    }


# --- tolerance boundaries ---------------------------------------------------------


def test_identical_passes():
    d = V.diff_qa(_qa(), _qa())
    assert d["ok"] is True
    assert d["metric_fails"] == []


def test_star_count_relative_boundary():
    assert V.diff_qa(_qa(star_count=1000), _qa(star_count=1049))["ok"] is True  # +4.9%
    d = V.diff_qa(_qa(star_count=1000), _qa(star_count=1060))  # +6%
    assert d["ok"] is False and "star_count" in d["metric_fails"]
    assert V.diff_qa(_qa(star_count=1000), _qa(star_count=951))["ok"] is True  # -4.9%


def test_fwhm_absolute_boundary():
    assert V.diff_qa(_qa(fwhm=3.0), _qa(fwhm=3.14))["ok"] is True
    assert "fwhm" in V.diff_qa(_qa(fwhm=3.0), _qa(fwhm=3.20))["metric_fails"]


def test_level_absolute_boundary():
    assert V.diff_qa(_qa(sky=13.0), _qa(sky=14.9))["ok"] is True
    assert "sky_median" in V.diff_qa(_qa(sky=13.0), _qa(sky=15.5))["metric_fails"]


def test_fraction_relative_with_abs_floor():
    # 20% relative on a healthy fraction. Measured on a GATING fraction metric --
    # envelope_area_frac carries the same tolerance but is diagnostic-only (2026-08),
    # so it can no longer demonstrate a fail.
    assert V.diff_qa(_qa(pegged=0.05), _qa(pegged=0.059))["ok"] is True
    assert "pegged_chroma_frac" in V.diff_qa(_qa(pegged=0.05), _qa(pegged=0.065))["metric_fails"]
    # near-zero fraction: relative tolerance would be ~0, the abs floor rescues noise
    assert V.diff_qa(_qa(near_black=0.0001), _qa(near_black=0.004))["ok"] is True
    assert "near_black_frac" in V.diff_qa(_qa(near_black=0.0001), _qa(near_black=0.02))["metric_fails"]


# --- hard regressions ---------------------------------------------------------------


def test_passed_regression_always_fails():
    d = V.diff_qa(_qa(passed=True), _qa(passed=False))
    assert d["ok"] is False and d["hard"]["passed_regressed"] is True


def test_new_gate_kind_is_regression_same_kind_is_not():
    base = _qa(passed=False, fails=["fwhm 4.20 > galaxy ceiling 3.81 (ref 3.5 scaled for pixel scale)"])
    same_kind = _qa(passed=False, fails=["fwhm 4.53 > galaxy ceiling 3.81 (ref 3.5 scaled for pixel scale)"])
    assert V.diff_qa(base, same_kind)["hard"]["new_gate_fail_kinds"] == []
    new_kind = _qa(
        passed=False,
        fails=[
            "fwhm 4.20 > galaxy ceiling 3.81",
            "background crushed: near_black_frac_outside_mask 4.02% > galaxy ceiling 2.0%",
        ],
    )
    d = V.diff_qa(base, new_kind)
    assert d["hard"]["new_gate_fail_kinds"] == ["background crushed"]
    assert d["ok"] is False


def test_resolved_gate_kind_is_improvement_not_failure():
    base = _qa(passed=False, fails=["faint_structure 0.0073 < galaxy floor 0.0079 (ref 0.025 scaled for FOV area)"])
    fresh = _qa(passed=True, fails=[])
    d = V.diff_qa(base, fresh)
    assert d["ok"] is True
    assert d["hard"]["resolved_gate_fail_kinds"] == ["faint_structure"]


def test_gate_kinds_parses_real_fail_strings():
    fails = [
        "star_count 120 < nebula floor 500",
        "fwhm 4.20 > galaxy ceiling 3.81 (ref 3.5 scaled for pixel scale)",
        "core_sat 5.0000% > galaxy ceiling 1.0%",
        "faint_structure 0.0073 < galaxy floor 0.0079 (ref 0.025 scaled for FOV area)",
        "background crushed: near_black_frac_outside_mask 4.02% > galaxy ceiling 2.0%",
        "gray wash: background_median_outside_mask 40.0 > galaxy ceiling 25",
        "neon color: pegged_chroma_frac_body 47.26% > nebula ceiling 20.0%",
        "dimensions [100, 100] != expected [200, 200]",
    ]
    assert V.gate_kinds(fails) == {
        "star_count",
        "fwhm",
        "core_sat",
        "faint_structure",
        "background crushed",
        "gray wash",
        "neon color",
        "dimensions",
    }


def test_missing_metric_skipped_not_failed():
    base, fresh = _qa(), _qa()
    del base["absolute_gates"]["faint_retention"]  # e.g. a starfield baseline
    d = V.diff_qa(base, fresh)
    assert d["metrics"]["faint_retention"]["ok"] is None
    assert d["ok"] is True
    assert any("faint_retention" in w for w in d["warnings"])


# --- staging -----------------------------------------------------------------------


def test_stage_raw_folder_passthrough(tmp_path):
    folder = tmp_path / "M81"
    folder.mkdir()
    assert V.stage_raw("M81", str(folder), str(tmp_path / "scratch")) == str(folder)


def test_stage_raw_single_fit_staged_into_scratch(tmp_path):
    loose = tmp_path / "Stacked_62_M 100_10.0s_IRCUT_20260703-214541.fit"
    fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(str(loose))
    scratch = tmp_path / "scratch"
    staged = V.stage_raw("M100", str(loose), str(scratch))
    assert staged == str(scratch / "M100_input")
    assert (scratch / "M100_input" / loose.name).exists()
    assert loose.exists()  # the original is never moved


def test_stage_raw_missing_is_none(tmp_path):
    assert V.stage_raw("M8", str(tmp_path / "nope"), str(tmp_path / "s")) is None


# --- orchestration -------------------------------------------------------------------


def test_run_validation_skips_and_filters(tmp_path, monkeypatch):
    rendered = []
    monkeypatch.setattr(
        V,
        "validate_target",
        lambda name, raw, cfg, scratch, baselines, update=False: rendered.append(name)
        or {"target": name, "status": "pass"},
    )
    replayed = []
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: replayed.append(name)
        or {"target": name, "status": "replay_pass", "param_diffs": {}, "pixels": {}, "tolerances": {}},
    )
    cfg = {
        "validation": {
            "scratch_dir": str(tmp_path / "s"),
            "baselines_dir": str(tmp_path / "b"),
            "targets": {"M81": "x", "M100": "y", "M8": "z"},
        }
    }
    out = V.run_validation(cfg, targets=["M81", "M100"])
    assert rendered == ["M81", "M100"]  # filter respected
    assert replayed == ["M81"]  # replay runs on the first rendered target only
    assert out["ok"] is True and out["counts"] == {"pass": 2, "replay_pass": 1}


def test_run_validation_fail_and_skip_summary(tmp_path, monkeypatch):
    def fake(name, raw, cfg, scratch, baselines, update=False):
        return {"target": name, "status": {"A": "pass", "B": "fail", "C": "skipped"}[name]}

    monkeypatch.setattr(V, "validate_target", fake)
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: {
            "target": name,
            "status": "replay_pass",
            "param_diffs": {},
            "pixels": {},
            "tolerances": {},
        },
    )
    cfg = {"validation": {"scratch_dir": str(tmp_path / "s"), "targets": {"A": "1", "B": "2", "C": "3"}}}
    out = V.run_validation(cfg)
    assert out["ok"] is False  # one real regression
    assert out["counts"] == {"fail": 1, "pass": 1, "skipped": 1, "replay_pass": 1}


def test_run_validation_refused_uncertified_flips_ok(tmp_path, monkeypatch):
    """A --update-baselines run where a target's render is refused for lacking
    owner certification must NOT report ok: True -- that would silently accept an
    uncertified baseline (tenet 7, no silent degrade)."""

    def fake(name, raw, cfg, scratch, baselines, update=False):
        return {"target": name, "status": {"A": "pass", "B": "refused_uncertified"}[name]}

    monkeypatch.setattr(V, "validate_target", fake)
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: {
            "target": name,
            "status": "replay_pass",
            "param_diffs": {},
            "pixels": {},
            "tolerances": {},
        },
    )
    cfg = {"validation": {"scratch_dir": str(tmp_path / "s"), "targets": {"A": "1", "B": "2"}}}
    out = V.run_validation(cfg, update=True)
    assert out["ok"] is False
    assert out["counts"] == {"pass": 1, "refused_uncertified": 1, "replay_pass": 1}
    assert any(r["status"] == "refused_uncertified" for r in out["results"])


def test_run_validation_replay_fail_flips_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(
        V,
        "validate_target",
        lambda name, raw, cfg, scratch, baselines, update=False: {"target": name, "status": "pass"},
    )
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: {
            "target": name,
            "status": "replay_fail",
            "param_diffs": {"denoise": {"original": 0.5, "replay": 0.6}},
            "pixels": {"max_abs": 5, "rmse": 1.0},
            "tolerances": {"max_abs_ceiling": 0, "rmse_ceiling": 0.0},
        },
    )
    cfg = {"validation": {"scratch_dir": str(tmp_path / "s"), "targets": {"A": "1"}}}
    out = V.run_validation(cfg)
    assert out["ok"] is False  # replay_fail is a regression too
    assert out["counts"] == {"pass": 1, "replay_fail": 1}


def test_run_validation_no_baseline_is_replay_eligible(tmp_path, monkeypatch):
    """A rendered target without a committed baseline still exercises the replay path
    (it rendered fine -- only the baseline diff is unavailable)."""
    monkeypatch.setattr(
        V,
        "validate_target",
        lambda name, raw, cfg, scratch, baselines, update=False: {
            "target": name,
            "status": "no_baseline",
            "reason": "missing baseline",
        },
    )
    replayed = []
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: replayed.append(name)
        or {"target": name, "status": "replay_pass", "param_diffs": {}, "pixels": {}, "tolerances": {}},
    )
    cfg = {"validation": {"scratch_dir": str(tmp_path / "s"), "targets": {"A": "1"}}}
    out = V.run_validation(cfg)
    assert replayed == ["A"]
    assert out["ok"] is True
    assert out["counts"] == {"no_baseline": 1, "replay_pass": 1}


def test_run_validation_replay_targets_first_eligible_result(tmp_path, monkeypatch):
    def fake(name, raw, cfg, scratch, baselines, update=False):
        return {"target": name, "status": {"A": "skipped", "B": "pass", "C": "pass"}[name]}

    monkeypatch.setattr(V, "validate_target", fake)
    replayed = []
    monkeypatch.setattr(
        V,
        "replay_check",
        lambda name, src, out_dir, cfg, scratch: replayed.append(name)
        or {"target": name, "status": "replay_pass", "param_diffs": {}, "pixels": {}, "tolerances": {}},
    )
    cfg = {"validation": {"scratch_dir": str(tmp_path / "s"), "targets": {"A": "1", "B": "2", "C": "3"}}}
    V.run_validation(cfg)
    assert replayed == ["B"]  # "skipped" (A) is not eligible; first eligible is B, then loop stops


# --- orientation gate (defense-in-depth vs the milestone flip) ------------------------


def _star_jpg(path, flip=False):
    """Synthetic asymmetric star field: random point sources + a corner anchor, so
    orientation_match's high-passed correlation can tell the 4 orientations apart."""
    from PIL import Image

    rng = np.random.default_rng(7)
    h, w = 240, 360
    img = rng.normal(12, 2, size=(h, w)).clip(0, 255)
    for y, x in zip(rng.integers(2, h - 2, 60), rng.integers(2, w - 2, 60), strict=False):
        img[y - 1 : y + 2, x - 1 : x + 2] = 250
    img[:30, :60] = 200  # asymmetric anchor (top-left block)
    if flip:
        img = img[::-1]
    Image.fromarray(img.astype("uint8")).convert("RGB").save(str(path), quality=95)


def test_validate_target_orientation_gate(tmp_path, monkeypatch):
    """A vertically flipped fresh render must HARD-FAIL against the archived
    baseline image even when every scalar metric matches (all are flip-invariant --
    exactly how the milestone flip slipped through)."""
    import seestar.qa as qa_module
    import seestar.run as run_module

    name = "X9"
    raw = tmp_path / "raw"
    raw.mkdir()
    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    out_dir = scratch / name
    out_dir.mkdir(parents=True)

    qa_doc = _qa()
    (baselines / f"{name}_qa.json").write_text(json.dumps(qa_doc))
    _star_jpg(baselines / f"{name}_final.jpg")
    _star_jpg(out_dir / f"{name}_final.jpg", flip=True)

    monkeypatch.setattr(run_module, "run_target", lambda *a, **k: {})
    monkeypatch.setattr(qa_module, "qa_target", lambda *a, **k: qa_doc)

    r = V.validate_target(name, str(raw), {}, str(scratch), str(baselines))
    assert r["status"] == "fail"
    assert r["reason"] == "orientation vs baseline: flipud"
    assert r["orientation"]["best"] == "flipud"

    # identity render passes the gate (and the whole diff)
    _star_jpg(out_dir / f"{name}_final.jpg", flip=False)
    r2 = V.validate_target(name, str(raw), {}, str(scratch), str(baselines))
    assert r2["status"] == "pass"
    assert r2["orientation"]["best"] == "identity"


# --- raw identity stamping and mismatch detection -----------------------------------


def test_raw_identity_hashes_the_staged_fit(tmp_path, fake_stack):
    from seestar.validate import _raw_identity

    target_dir = tmp_path / "M8"
    target_dir.mkdir()
    fake_stack(target_dir / "Stacked_51_M 8_10.0s_LP_20260615-221349.fit", obj="M 8", filt="LP", stackcnt=51)

    identity = _raw_identity(str(target_dir))
    assert identity["basename"] == "Stacked_51_M 8_10.0s_LP_20260615-221349.fit"
    assert identity["frames"] == 51
    assert identity["filter"] == "LP"
    assert len(identity["sha256"]) == 64  # hex sha256


def test_raw_identity_empty_dir_returns_empty_dict(tmp_path):
    from seestar.validate import _raw_identity

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _raw_identity(str(empty)) == {}


def test_validate_target_skips_on_raw_identity_mismatch(tmp_path, fake_stack, monkeypatch):
    from seestar import validate as V

    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    raw_dir = tmp_path / "M8raw"
    raw_dir.mkdir()
    fake_stack(raw_dir / "Stacked_13_M 8_10.0s_LP_20260101-000000.fit", obj="M 8", filt="LP", stackcnt=13)

    # A baseline recorded against a DIFFERENT capture (different sha256/frames).
    (baselines / "M8_qa.json").write_text(
        '{"passed": true, "raw_identity": {"sha256": "deadbeef", "frames": 51, '
        '"datetime": "2026-06-15T22:13:49", "filter": "LP"}}'
    )

    # run_target must NOT be called on a mismatch -- patch it to fail the test if it is.
    monkeypatch.setattr(V, "stage_raw", lambda name, raw, scratch: str(raw_dir))
    called = {"run": False}

    def _fail_if_called(*a, **kw):
        called["run"] = True
        raise AssertionError("run_target must not be called on a raw-identity mismatch")

    import seestar.run as run_module

    monkeypatch.setattr(run_module, "run_target", _fail_if_called)

    result = V.validate_target("M8", str(raw_dir), {}, str(scratch), str(baselines))
    assert result["status"] == "skipped"
    assert "raw stack differs from baseline" in result["reason"]
    assert called["run"] is False


# --- uncertified reference renders hard-fail, never silently skip (Task 12) --------
#
# docs/TENETS.md tenet 7: "a step or gate that silently changes, skips, or degrades
# is a defect." An uncertified render (HeroOutcome.PROCESSING_FAILED,
# core/publication.py's decide_publication) isn't a real deliverable; comparing its
# orientation/QA against a certified baseline would validate against garbage, and
# capturing it AS a baseline would poison every future comparison silently.


def test_validate_target_hard_fails_on_uncertified_reference_render(tmp_path, monkeypatch):
    import seestar.run as run_module

    name = "M"
    raw = tmp_path / "raw"
    raw.mkdir()
    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"

    monkeypatch.setattr(
        run_module,
        "run_target",
        lambda *a, **k: {
            "target": name,
            "published": {"certified": False, "hero_base": f"{name}_uncertified", "sidecars": {}},
        },
    )

    r = V.validate_target(name, str(raw), {}, str(scratch), str(baselines))
    assert r["status"] == "fail"
    assert "uncertified" in r["reason"]


def test_update_baselines_refuses_uncertified(tmp_path, monkeypatch):
    import seestar.run as run_module

    name = "M"
    raw = tmp_path / "raw"
    raw.mkdir()
    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"

    monkeypatch.setattr(
        run_module,
        "run_target",
        lambda *a, **k: {
            "target": name,
            "published": {"certified": False, "hero_base": f"{name}_uncertified", "sidecars": {}},
        },
    )

    r = V.validate_target(name, str(raw), {}, str(scratch), str(baselines), update=True)
    assert r["status"] == "refused_uncertified"
    assert not os.path.exists(baselines / f"{name}_qa.json")  # no baseline json written
    assert not os.path.exists(baselines / f"{name}_final.jpg")  # no baseline image copied


# --- config key vs render's own derived label (the M8_noisy scenario) --------------
#
# A validation config key (e.g. "M8_noisy", a short/noisy M8 capture added as its own
# validation target) need NOT match the label run_target actually renders under --
# that label is derived from the FITS header/filename (never the config key), so a
# capture whose header still says OBJECT="M 8" renders as M8_final.jpg / M8_qa.json,
# not M8_noisy_*. Only the baselines/ dir and status reporting stay keyed by the
# config name; files INSIDE out_dir must be addressed by the render's own label.


def test_validate_target_uses_render_own_label_not_config_key(tmp_path, monkeypatch):
    import seestar.qa as qa_module
    import seestar.run as run_module

    name = "M8_noisy"
    label = "M8"  # what the header-derived render actually calls itself
    raw = tmp_path / "raw"
    raw.mkdir()
    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"
    baselines.mkdir()
    out_dir = scratch / name
    out_dir.mkdir(parents=True)

    qa_doc = _qa()
    # baselines/ stays keyed by the config name.
    (baselines / f"{name}_qa.json").write_text(json.dumps(qa_doc))
    _star_jpg(baselines / f"{name}_final.jpg")
    # the render's own output file inside out_dir is named by the ACTUAL label.
    _star_jpg(out_dir / f"{label}_final.jpg")

    monkeypatch.setattr(run_module, "run_target", lambda *a, **k: {"target": label})
    qa_calls = []
    monkeypatch.setattr(
        qa_module,
        "qa_target",
        lambda out_dir_arg, cfg_arg, label_arg: qa_calls.append(label_arg) or qa_doc,
    )

    r = V.validate_target(name, str(raw), {}, str(scratch), str(baselines))

    assert qa_calls == [label]  # qa_target was asked for M8_*, not M8_noisy_*
    assert r["status"] == "pass"
    assert r["target"] == name  # status/reporting stays keyed by the config name
    assert r["orientation"]["best"] == "identity"  # fresh_img was found under the actual label


def test_validate_target_render_returning_no_target_key_falls_back_to_name(tmp_path, monkeypatch):
    """Defense-in-depth: if run_target's return doesn't carry a usable 'target' (e.g.
    a stub in a test double), fall back to the config name rather than crashing."""
    import seestar.qa as qa_module
    import seestar.run as run_module

    name = "X9"
    raw = tmp_path / "raw"
    raw.mkdir()
    scratch = tmp_path / "scratch"
    baselines = tmp_path / "baselines"
    out_dir = scratch / name
    out_dir.mkdir(parents=True)

    monkeypatch.setattr(run_module, "run_target", lambda *a, **k: {})
    qa_calls = []
    monkeypatch.setattr(
        qa_module,
        "qa_target",
        lambda out_dir_arg, cfg_arg, label_arg: qa_calls.append(label_arg) or _qa(),
    )

    r = V.validate_target(name, str(raw), {}, str(scratch), str(baselines))

    assert qa_calls == [name]
    assert r["status"] == "no_baseline"


def test_render_label_prefers_header_object_over_config_key(tmp_path, fake_stack):
    from seestar.validate import _render_label

    target_dir = tmp_path / "M8_noisy"
    target_dir.mkdir()
    fake_stack(target_dir / "Stacked_13_M 8_10.0s_LP_20260701-000000.fit", obj="M 8", filt="LP", stackcnt=13)

    assert _render_label(str(target_dir), "M8_noisy") == "M8"


def test_render_label_falls_back_to_name_when_no_raw_found(tmp_path):
    from seestar.validate import _render_label

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _render_label(str(empty), "M8_noisy") == "M8_noisy"


def test_replay_check_uses_render_own_label_for_files(tmp_path, fake_stack, monkeypatch):
    import tifffile

    import seestar.run as run_module

    name = "M8_noisy"
    label = "M8"
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    fake_stack(raw_dir / "Stacked_13_M 8_10.0s_LP_20260701-000000.fit", obj="M 8", filt="LP", stackcnt=13)

    scratch = tmp_path / "scratch"
    out_dir = scratch / name
    out_dir.mkdir(parents=True)

    px = np.zeros((4, 4), dtype=np.uint16)
    # The original render's own output files are named by the ACTUAL label, not the
    # config key (mirroring what run_target really writes).
    (out_dir / f"{label}_recipe.json").write_text(
        json.dumps({"adjustments": [{"adjustment": "denoise", "value": 0.5}]})
    )
    tifffile.imwrite(str(out_dir / f"{label}_final.tif"), px)

    def _fake_replay_render(src, replay_dir, cfg, **kw):
        os.makedirs(replay_dir, exist_ok=True)
        with open(os.path.join(replay_dir, f"{label}_recipe.json"), "w", encoding="utf-8") as f:
            json.dump({"adjustments": [{"adjustment": "denoise", "value": 0.5}]}, f)
        tifffile.imwrite(os.path.join(replay_dir, f"{label}_final.tif"), px)
        return {"target": label}

    monkeypatch.setattr(run_module, "run_target", _fake_replay_render)

    cfg = {"validation": {"replay": {"max_abs_ceiling": 0, "rmse_ceiling": 0.0}}}
    r = V.replay_check(name, str(raw_dir), str(out_dir), cfg, str(scratch))

    assert r["status"] == "replay_pass"
    assert r["target"] == name  # status/reporting stays keyed by the config name


# --- OS-aware path resolution (macOS vs Windows validation raw paths) -----------------


def _os_vcfg():
    return {
        "targets": {"M8": "C:/win/M8"},
        "targets_darwin": {"M8": "/mac/M8"},
        "baselines_dir": "validation/baselines",
        "baselines_dir_darwin": "validation/baselines-mac",
    }


def test_resolve_os_paths_darwin_prefers_darwin_keys():
    tmap, baselines = V._resolve_os_paths(_os_vcfg(), "darwin")
    assert tmap == {"M8": "/mac/M8"}
    assert baselines == "validation/baselines-mac"


def test_resolve_os_paths_windows_uses_base_keys():
    tmap, baselines = V._resolve_os_paths(_os_vcfg(), "win32")
    assert tmap == {"M8": "C:/win/M8"}
    assert baselines == "validation/baselines"


def test_resolve_os_paths_darwin_falls_back_when_no_darwin_keys():
    vcfg = {"targets": {"M8": "C:/win/M8"}, "baselines_dir": "validation/baselines"}
    tmap, baselines = V._resolve_os_paths(vcfg, "darwin")
    assert tmap == {"M8": "C:/win/M8"}  # no targets_darwin -> base Windows/default map
    assert baselines == "validation/baselines"


def test_resolve_os_paths_defaults_when_absent():
    tmap, baselines = V._resolve_os_paths({}, "win32")
    assert tmap == {}
    assert baselines == "validation/baselines"


def test_run_validation_uses_darwin_targets_on_mac(tmp_path, monkeypatch):
    """run_validation must route through the OS resolver: on darwin it renders the
    targets_darwin entries, not the base (Windows) targets."""
    monkeypatch.setattr(V.sys, "platform", "darwin")
    rendered = []
    monkeypatch.setattr(
        V,
        "validate_target",
        lambda name, raw, cfg, scratch, baselines, update=False: rendered.append((name, raw, baselines))
        or {"target": name, "status": "skipped", "reason": "stub"},
    )
    cfg = {
        "validation": {
            "scratch_dir": str(tmp_path / "s"),
            "baselines_dir": str(tmp_path / "b"),
            "baselines_dir_darwin": str(tmp_path / "bmac"),
            "targets": {"M8": "C:/win/M8"},
            "targets_darwin": {"M8": str(tmp_path / "mac_M8")},
        }
    }
    V.run_validation(cfg)
    assert rendered and rendered[0][0] == "M8"
    assert rendered[0][1] == str(tmp_path / "mac_M8")  # darwin raw path, not the C:/ one
    assert rendered[0][2] == os.path.abspath(str(tmp_path / "bmac"))  # darwin baselines dir


# --- fixed-area brightness (mask-independent, 2026-08) -------------------------------


def test_fixed_area_p90_is_independent_of_mask_membership():
    """The same pixels compared over the same PINNED area fraction give the same
    number no matter how a subject mask would have carved them up."""
    luma = np.zeros((100, 100), dtype=np.float64)
    luma[:10, :] = 200.0  # 10% of the frame is bright
    assert V.fixed_area_p90(luma, 0.10) == 200.0
    assert V.fixed_area_p90(luma, 0.05) == 200.0


def test_fixed_area_p90_tracks_a_real_brightness_change():
    a = np.zeros((100, 100), dtype=np.float64)
    a[:10, :] = 100.0
    b = a.copy()
    b[:10, :] = 120.0
    assert V.fixed_area_p90(b, 0.10) - V.fixed_area_p90(a, 0.10) == 20.0


def test_fixed_area_p90_rejects_a_nonsense_fraction():
    luma = np.ones((10, 10), dtype=np.float64)
    assert V.fixed_area_p90(luma, 0.0) is None
    assert V.fixed_area_p90(luma, 1.5) is None


def test_masked_percentiles_are_diagnostic_not_failing():
    """A mask that grew moves body_p90 hard. That is information, not a regression."""
    d = V.diff_qa(_qa(body_p90=58.8), _qa(body_p90=40.0))
    assert d["metrics"]["body_p90"]["ok"] is False
    assert d["metrics"]["body_p90"]["diagnostic"] is True
    assert "body_p90" not in d["metric_fails"]
    assert d["ok"] is True
    assert "envelope_area_frac" not in V.diff_qa(_qa(envelope=0.05), _qa(envelope=0.09))["metric_fails"]


def test_fixed_area_metric_can_fail_the_diff():
    extra = {"fixed_area_p90": {"baseline": 58.0, "fresh": 40.0, "kind": "level_abs"}}
    d = V.diff_qa(_qa(), _qa(), extra_metrics=extra)
    assert "fixed_area_p90" in d["metric_fails"] and d["ok"] is False
    near = {"fixed_area_p90": {"baseline": 58.0, "fresh": 59.5, "kind": "level_abs"}}
    assert V.diff_qa(_qa(), _qa(), extra_metrics=near)["ok"] is True
