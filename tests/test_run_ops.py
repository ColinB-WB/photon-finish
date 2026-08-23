"""Task 15 acceptance: the render path of `run_target` executes through the
typed-op executor + Workspace (planning is still legacy/inline in run.py --
that moves to the planner in slice 5). All four external tools are faked at
the adapter-registry boundary (conftest.install_fake_op_adapters) -- same
"fake at the boundary, real files" philosophy as write_fake_stack -- so this
is a real run_target() call, real Workspace, real executor, real files on
disk; only the Siril/GraXpert/StarNet2 subprocess calls are stubbed.

Placeholder target name "X1" (never a real astronomical object) per
CLAUDE.md's target-agnostic rule.
"""

from __future__ import annotations

import glob
import json
import os

import pytest

import seestar.orchestrate as orch_module
import seestar.qa as qa_module
import seestar.run as run_module
from seestar.io import load_config

FAKE_FIT_NAME = "Stacked_51_X1_10.0s_LP_20260615-221349.fit"


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _make_target_dir(tmp_path):
    from conftest import write_fake_stack

    target_dir = tmp_path / "X1_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / FAKE_FIT_NAME, obj="X1")
    return str(target_dir)


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


def _fake_probe_metric(jpg_path, metric, mask_cfg=None, **kw):
    """The stretch solve now runs through the core state machine (planner-driven);
    fake the JPG probe metric so the search converges deterministically without a
    real render (body target 72, sky well below any ceiling)."""
    return 72.0 if metric == "body_p90" else 10.0


def _apply_mocks(monkeypatch):
    from conftest import fake_measure_subject_support, install_fake_op_adapters, install_fake_run_siril

    monkeypatch.setattr(orch_module, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(orch_module, "embed", lambda *a, **kw: None)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    import seestar.execute as exec_module

    monkeypatch.setattr(exec_module, "_probe_metric", _fake_probe_metric)
    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    monkeypatch.setattr(qa_module, "input_star_metrics", lambda *a, **kw: {})


def _run(monkeypatch, target_dir, out_dir, **kw):
    _apply_mocks(monkeypatch)
    cfg = _cfg()
    return run_module.run_target(
        target_dir,
        out_dir,
        cfg,
        profile=kw.pop("profile", "showcase"),
        **kw,
    )


def _force_pass(monkeypatch):
    """Make evaluate_hero classify the candidate PASS. REQUIRED for any certified-path
    test: the hermetic harness's fake renders fail star_count/fwhm/sky_median by
    default, so without this every end-to-end run is processing_failed."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch_module.evaluate_hero

    def _passed(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero, outcome=HeroOutcome.PASS, processing_failed_gates=(), source_limited_gates=(), reasons=()
        )

    monkeypatch.setattr(orch_module, "evaluate_hero", _passed)


def test_extended_showcase_emits_star_treatment_set_conservative_hero(tmp_path, monkeypatch):
    """LP nebula under showcase -> star_forks True -> the three-look deliverable
    set. Phase 3A: the _final hero is the stars-on look; the star-reduced render
    ships under its own _star-reduced name (no separate _stars-on file)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_final/* names
    result = _run(monkeypatch, target_dir, out_dir)

    assert result["params"]["category"] == "nebula"
    for base in ("X1_final", "X1_star-reduced", "X1_starless"):
        for ext in ("jpg", "png", "tif"):
            p = os.path.join(out_dir, f"{base}.{ext}")
            assert os.path.exists(p), f"missing deliverable {p}"
    assert not os.path.exists(os.path.join(out_dir, "X1_stars-on.jpg"))

    variants = {o["variant"] for o in result["outputs"]}
    assert variants == {"stars-on", "starless", "star-reduced"}


def test_no_fit_scratch_left_in_output_dir(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _run(monkeypatch, target_dir, out_dir)

    stray = glob.glob(os.path.join(out_dir, "*.fit")) + glob.glob(os.path.join(out_dir, "*.fits"))
    assert stray == []
    # the run-scoped scratch dir itself must be gone too (cleanup ran -- no keep_scratch/debug)
    assert glob.glob(os.path.join(out_dir, "_work_*")) == []


def test_notes_and_recipe_sidecars_emitted(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_notes.json name
    _run(monkeypatch, target_dir, out_dir)

    notes_path = os.path.join(out_dir, "X1_notes.json")
    recipe_path = os.path.join(out_dir, "X1_recipe.json")
    assert os.path.exists(notes_path)
    assert os.path.exists(recipe_path)
    notes_doc = json.loads(open(notes_path, encoding="utf-8").read())
    assert notes_doc["target"] == "X1"
    recipe_doc = json.loads(open(recipe_path, encoding="utf-8").read())
    assert recipe_doc["target"] == "X1"


def test_build_notes_stretch_solve_includes_probe_history(tmp_path, monkeypatch):
    """`_build_notes`'s stretch_solve branch must record every solve probe, not
    just the final settled value (Task 4: a prerequisite for later picking the
    best probe on non-convergence)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_notes.json name
    _run(monkeypatch, target_dir, out_dir)

    notes_path = os.path.join(out_dir, "X1_notes.json")
    notes_doc = json.loads(open(notes_path, encoding="utf-8").read())
    probes = notes_doc["notes"]["stretch_solve"]["probes"]
    assert isinstance(probes, list) and len(probes) >= 1
    assert "mtf_mid" in probes[0]


def test_keep_scratch_survives_and_is_reported(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    result = _run(monkeypatch, target_dir, out_dir, keep_scratch=True)

    assert result["scratch_dir"] is not None
    assert os.path.isdir(result["scratch_dir"])
    assert os.path.dirname(result["scratch_dir"]) == os.path.abspath(out_dir)


def test_combine_late_failure_falls_back_to_latest_session(tmp_path, monkeypatch):
    """Combine-sessions regression (reviewer, Task 15): the CombineSessions op can
    SUCCEED (its artifact written + registered) while a LATER line of the try block
    (the post-combine re-measure/classify) raises. The fallback must then behave as
    if combine never happened: PrepareSource the LATEST single session, single-session
    stats/provenance, and -- critically -- the render must consume the prepared latest
    raw, NOT the already-registered combined pixels."""
    from conftest import write_fake_stack

    target_dir = tmp_path / "X3_import"
    target_dir.mkdir()
    write_fake_stack(
        target_dir / "Stacked_40_X3_10.0s_LP_20250601-000000.fit",
        obj="X3",
        stackcnt=40,
        date_obs="2025-06-01T00:00:00",
        seed=1,
    )
    latest = write_fake_stack(target_dir / "Stacked_51_X3_10.0s_LP_20260615-221349.fit", obj="X3", stackcnt=51, seed=2)
    out_dir = str(tmp_path / "out3")

    _apply_mocks(monkeypatch)

    # The planner emits CombineSessions as the source op; inject a failure at the
    # adapter boundary so the planner's combine-fallback fires: PrepareSource the
    # LATEST single session under a FRESH ref (working_copy_latest), and the render
    # must consume that prepared latest raw, not the (unwritten) combined artifact.
    from seestar.adapters import base as B
    from seestar.adapters.base import AdapterError
    from seestar.core.types import CombineSessions as _Combine

    class _BoomCombine:
        name = "boom-combine"

        def execute(self, op, ctx):
            raise AdapterError("combine boom")

    monkeypatch.setitem(B.REGISTRY, _Combine, _BoomCombine())
    cfg = _cfg()
    cfg["engine"]["combine_sessions"] = True

    result = run_module.run_target(str(target_dir), out_dir, cfg, keep_scratch=True)

    assert "combine_failed" in result["notes"]
    assert "combined_sessions" not in result["notes"]
    assert "Stacked_51" in result["notes"]["combine_failed"]  # fell back to the LATEST
    assert result["inspection"]["meta"]["frames"] == 51  # single-session stats, not 40+51
    # the working copy the render consumed is the PREPARED latest raw (a byte-identical
    # copy under the fallback's fresh ref), not the combined artifact
    prepared = os.path.join(result["scratch_dir"], "working_copy_latest.fit")
    assert os.path.exists(prepared)
    with open(prepared, "rb") as a, open(latest, "rb") as b:
        assert a.read() == b.read()


def test_e_gate_notes_extended_signal_is_plain_float(tmp_path, monkeypatch):
    """Regression (final-review finding): `_build_notes`'s e-gate branch did
    `round(side["e"], 4)`, but `take_measurement`'s EXTENDED_SIGNAL side-data is a
    DICT (`{"E": E}`, execute.py:240) like every other branch in `_build_notes`
    -- `side["e"]` is not a bare float, so `round` raised
    `TypeError: type dict doesn't define __round__ method`. Reachable whenever an
    IRCUT target's category isn't decided by the catalog prior and `engine.e_gate`
    is on. Drive a real IRCUT target through `run_target` with e_gate enabled and
    no catalog prior (so EXTENDED_SIGNAL actually runs) and assert the run
    completes and `notes["e_gate"]["E"]` is a plain float."""
    from conftest import write_fake_stack

    target_dir = tmp_path / "X4_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_60_X4_10.0s_IRCUT_20260615-221349.fit", obj="X4", filt="IRCUT")
    out_dir = str(tmp_path / "out4")

    _apply_mocks(monkeypatch)
    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "probe_extended_signal", lambda *a, **kw: 0.012345)

    cfg = _cfg()
    cfg["engine"]["e_gate"] = True

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X4_notes.json name
    result = run_module.run_target(str(target_dir), out_dir, cfg, profile="showcase")

    e_gate_notes = result["notes"]["e_gate"]
    assert e_gate_notes["E"] == 0.0123
    assert isinstance(e_gate_notes["E"], float)

    notes_path = os.path.join(out_dir, "X4_notes.json")
    notes_doc = json.loads(open(notes_path, encoding="utf-8").read())
    assert notes_doc["notes"]["e_gate"]["E"] == 0.0123


def test_starfield_single_final_no_star_treatment(tmp_path, monkeypatch):
    """IRCUT star field (globular/cluster category) -> a single stars-on final,
    never the star-treatment set (CLAUDE.md §8a)."""
    from conftest import write_fake_stack

    target_dir = tmp_path / "X2_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_60_X2_10.0s_IRCUT_20260615-221349.fit", obj="X2", filt="IRCUT")
    out_dir = str(tmp_path / "out2")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X2_final.jpg name
    result = _run(monkeypatch, str(target_dir), out_dir, overrides={"structure_enabled": False})

    assert result["params"]["category"] == "starfield"
    assert os.path.exists(os.path.join(out_dir, "X2_final.jpg"))
    assert not os.path.exists(os.path.join(out_dir, "X2_stars-on.jpg"))


def test_run_target_hero_is_measured_as_a_candidate_before_promotion(tmp_path, monkeypatch):
    """The hero Export's ONE execution must happen under ArtifactRole.CANDIDATE --
    proven by spying on execute_plan to confirm no DELIVERABLE-role Export for the
    hero's output_base is ever executed, only a CANDIDATE-role one, and that
    evaluate_hero ran exactly once. The real deliverable file must still exist
    after run_target returns (via promotion)."""
    from seestar.core.types import ArtifactRole, Export

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    _apply_mocks(monkeypatch)
    cfg = _cfg()

    calls = {"evaluate_hero": 0}
    real_evaluate_hero = orch_module.evaluate_hero

    def _spy(*args, **kwargs):
        calls["evaluate_hero"] += 1
        return real_evaluate_hero(*args, **kwargs)

    monkeypatch.setattr(orch_module, "evaluate_hero", _spy)
    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_final.jpg name;
    # composes on top of the spy above (captures it as `real`) so the call count stays accurate

    executed_export_roles = []
    real_execute_plan = orch_module.execute_plan

    def _spy_execute_plan(plan, *args, **kwargs):
        for op in plan.steps:
            if isinstance(op, Export):
                executed_export_roles.append((op.output_base, op.role))
        return real_execute_plan(plan, *args, **kwargs)

    monkeypatch.setattr(orch_module, "execute_plan", _spy_execute_plan)

    result = orch_module.run_target(target_dir, out_dir, cfg, profile="showcase")

    assert calls["evaluate_hero"] == 1
    hero_base = f"{result['target']}_final"
    hero_roles = [role for base, role in executed_export_roles if base == hero_base]
    assert hero_roles == [ArtifactRole.CANDIDATE]  # the hero's ONE execution was as a candidate, never DELIVERABLE
    final_jpg = os.path.join(out_dir, f"{hero_base}.jpg")
    assert os.path.exists(final_jpg)  # promoted after acceptance, post-execution
    assert not os.path.exists(os.path.join(out_dir, "X2_starless.jpg"))


def test_run_target_documentation_profile_annotation_finds_the_promoted_hero(tmp_path, monkeypatch):
    """Review-finding regression coverage for the Task 5 promotion fix (orchestrate.py:506-510):
    once the hero Export is promoted from its CANDIDATE-role scratch path to its real
    DELIVERABLE-role path via os.replace, `ws.register(...)` re-registers that promoted
    path under the hero's ORIGINAL DELIVERABLE-role ref. The `documentation` profile's
    annotate step (orchestrate.py:528) resolves the hero jpg via `ws.find_producer` on
    that EXACT ref -- before the Task 5 fix this lookup returned None (only the
    CANDIDATE-role ref had ever been registered for this op) and annotation silently
    produced nothing. No test in this suite drove the `documentation` profile before this
    one, so this fix had zero coverage.

    Every other test in this file uses profile="showcase"; documentation is the only
    profile with decisions.annotate True (core/policy.py PROFILE_OVERLAYS), so this is
    the one path that exercises orchestrate.py's annotate block at all."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified X1_final/annotated names
    result = _run(monkeypatch, target_dir, out_dir, profile="documentation")

    assert result["params"]["category"] == "nebula"

    hero_jpg = os.path.join(out_dir, "X1_final.jpg")
    assert os.path.exists(hero_jpg)  # the promoted hero deliverable itself

    annotated = os.path.join(out_dir, "X1_final_annotated.jpg")
    assert os.path.exists(annotated), "documentation profile must emit the annotated JPG"
    assert os.path.getsize(annotated) > 0  # a real PIL-drawn JPEG, not an empty stub

    variants = {o["variant"] for o in result["outputs"]}
    assert "final-annotated" in variants


def _make_starfield_target_dir(tmp_path, name="X5"):
    """IRCUT star field (globular/cluster category, per
    test_starfield_single_final_no_star_treatment above) -> a single stars-on
    final, with variant literally "final" -- unlike the LP nebula fixture
    (_make_target_dir), whose showcase star_forks emit "star-reduced" for the
    canonical hero base instead."""
    from conftest import write_fake_stack

    target_dir = tmp_path / f"{name}_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / f"Stacked_60_{name}_10.0s_IRCUT_20260615-221349.fit", obj=name, filt="IRCUT")
    return str(target_dir)


def _run_starfield(monkeypatch, target_dir, out_dir):
    return _run(monkeypatch, target_dir, out_dir, overrides={"structure_enabled": False})


def test_promote_writes_the_run_manifest_last_and_it_names_every_deliverable(tmp_path, monkeypatch):
    """The manifest must describe exactly what was promoted, and per its
    docstring is the single transactional commit point -- this test proves its
    *content* is correct (format, attempt, the hero deliverable, and that every
    path it names actually exists on disk). Task 6's second test below proves
    the *ordering* half (never written before every rename has succeeded)."""
    target_dir = _make_starfield_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified {target}_run.json name
    result = _run_starfield(monkeypatch, target_dir, out_dir)

    manifest_path = os.path.join(out_dir, f"{result['target']}_run.json")
    assert os.path.exists(manifest_path)
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["manifest_format"] == 1
    assert manifest["attempt"] == 0
    assert any(d["variant"] == "final" for d in manifest["deliverables"])
    for d in manifest["deliverables"]:
        for path in (d.get("jpg"), d.get("png"), d.get("tif")):
            if path:
                assert os.path.exists(path)


def test_a_failed_promotion_does_not_overwrite_the_prior_manifest(tmp_path, monkeypatch):
    """Simulate a mid-promotion failure (one deliverable rename raises) and
    confirm the manifest describing the PREVIOUS successful run is still
    present and unchanged -- the core "no stale-final-masking" guarantee."""
    target_dir = _make_starfield_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified {target}_run.json name
    # First, a real successful run to establish a manifest.
    result1 = _run_starfield(monkeypatch, target_dir, out_dir)
    manifest_path = os.path.join(out_dir, f"{result1['target']}_run.json")
    with open(manifest_path, encoding="utf-8") as f:
        original_manifest = f.read()

    # Second run: force a rename to fail partway through promotion. The first
    # os.replace call of a run is write_plan_sidecar's (before promotion even
    # starts); the second is the hero's first deliverable rename -- exactly the
    # "partway through promotion" point this test needs.
    real_replace = orch_module.os.replace
    call_count = {"n": 0}

    def _flaky_replace(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated failure mid-promotion")
        return real_replace(src, dst)

    monkeypatch.setattr(orch_module.os, "replace", _flaky_replace)

    with pytest.raises(OSError, match="simulated failure"):
        _run_starfield(monkeypatch, target_dir, out_dir)

    with open(manifest_path, encoding="utf-8") as f:
        assert f.read() == original_manifest  # untouched by the failed attempt
