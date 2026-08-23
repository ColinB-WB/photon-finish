"""Task 15: `_cleanup` is gone -- the Workspace now owns scratch, and run_target
calls `ws.cleanup()` (spec section 6). Ownership semantics: deliverables + the
notes/recipe sidecars + the QA starless tif all survive; the run-scoped scratch
dir is gone entirely; a file the run never touched (planted by the "user" before
the run, or a sibling profile's earlier deliverables) is untouched either way --
never adopted, never deleted -- because Workspace ownership is per-run, in-memory,
and by exact registration (never by path/name/suffix guessing, unlike the old
_cleanup glob)."""

from __future__ import annotations

import glob
import os

import seestar.orchestrate as orch_module
import seestar.qa as qa_module
import seestar.run as run_module
from seestar.io import load_config


class _FakeTools:
    siril = "siril-cli"
    graxpert = "GraXpert"
    starnet2 = "starnet2"
    exiftool = "exiftool"


def _make_target_dir(tmp_path):
    from conftest import write_fake_stack

    target_dir = tmp_path / "M_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_51_M_10.0s_LP_20260615-221349.fit", obj="M")
    return str(target_dir)


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


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


def _run(monkeypatch, target_dir, out_dir, **kw):
    from conftest import fake_probe_metric, install_fake_op_adapters, install_fake_run_siril

    monkeypatch.setattr(orch_module, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(orch_module, "embed", lambda *a, **kw: None)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    import seestar.execute as exec_module

    monkeypatch.setattr(exec_module, "_probe_metric", fake_probe_metric)
    from conftest import fake_measure_subject_support

    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    monkeypatch.setattr(qa_module, "input_star_metrics", lambda *a, **kw: {})
    cfg = _cfg()
    return run_module.run_target(target_dir, out_dir, cfg, **kw)


def test_cleanup_keeps_deliverables_and_sidecars_removes_scratch_leaves_unowned(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out")
    os.makedirs(out_dir, exist_ok=True)

    # a file the run never touched -- must survive untouched, never adopted.
    unrelated = os.path.join(out_dir, "users_own_file.txt")
    with open(unrelated, "w") as f:
        f.write("not part of this run")
    # a sibling profile's earlier deliverable in the SAME output dir -- must survive.
    sibling = os.path.join(out_dir, "M_natural.jpg")
    with open(sibling, "wb"):
        pass

    _force_pass(monkeypatch)  # certified-path intent: asserts the certified M_final/* names
    result = _run(monkeypatch, target_dir, out_dir, overrides={"star_forks": False})

    # deliverables for THIS run survive
    for ext in ("jpg", "png", "tif"):
        assert os.path.exists(os.path.join(out_dir, f"M_final.{ext}"))
    # sidecars survive
    assert os.path.exists(os.path.join(out_dir, "M_notes.json"))
    assert os.path.exists(os.path.join(out_dir, "M_recipe.json"))
    # nothing unowned was touched
    assert os.path.exists(unrelated)
    assert os.path.exists(sibling)
    # the run-scoped scratch dir is entirely gone (default: cleanup runs)
    assert glob.glob(os.path.join(out_dir, "_work_*")) == []
    assert glob.glob(os.path.join(out_dir, "*.fit")) == []
    assert result["scratch_dir"] is None


def test_concentrated_cluster_starless_tif_survives_as_qa_artifact(tmp_path, monkeypatch):
    """A concentrated starfield emits {target}_starless.tif as a QA-role artifact
    (output_dir), not a scratch/PRODUCTION one -- ws.cleanup() must leave it."""
    from conftest import write_fake_stack

    target_dir = tmp_path / "M2_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_60_M2_10.0s_IRCUT_20260615-221349.fit", obj="M2", filt="IRCUT")
    out_dir = str(tmp_path / "out2")

    # force the concentrated-cluster branch without depending on random fake-data
    # concentration measurement: fake measure_concentration deterministically "high".
    import seestar.measure as measure_module

    def _run_with_cluster(monkeypatch):
        monkeypatch.setattr(
            measure_module,
            "measure_concentration",
            lambda *a, **kw: {"concentration": 999.0, "center_xy": [0, 0], "block": 32},
        )
        _force_pass(monkeypatch)  # certified-path intent: asserts the certified M2_starless.tif name
        return _run(monkeypatch, str(target_dir), out_dir, overrides={"structure_enabled": True})

    result = _run_with_cluster(monkeypatch)

    assert result["params"].get("cluster") is True
    assert os.path.exists(os.path.join(out_dir, "M2_starless.tif"))
    # its sibling stars.tif is scratch -- gone after cleanup
    assert glob.glob(os.path.join(out_dir, "_work_*")) == []


def test_keep_scratch_skips_cleanup(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out3")

    result = _run(monkeypatch, target_dir, out_dir, overrides={"star_forks": False}, keep_scratch=True)

    assert result["scratch_dir"] is not None
    assert os.path.isdir(result["scratch_dir"])


def test_mid_render_exception_still_cleans_scratch(tmp_path, monkeypatch):
    """M3: any exception between workspace creation and the cleanup call used to
    orphan a `_work_<uuid>/` dir holding full-size FITS -- unbounded disk growth in
    exactly the failed-retry loop a struggling user hits. Cleanup is now in a
    `finally`, so the scratch dir goes even when the render raises."""
    import pytest

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out_fail")

    def _boom(*a, **kw):
        raise RuntimeError("boom mid-render")

    monkeypatch.setattr(orch_module, "execute_plan", _boom)

    with pytest.raises(RuntimeError, match="boom mid-render"):
        _run(monkeypatch, target_dir, out_dir, overrides={"star_forks": False})

    assert glob.glob(os.path.join(out_dir, "_work_*")) == []


def test_mid_render_exception_preserves_scratch_when_keep_scratch(tmp_path, monkeypatch):
    """The failure path honors the same opt-outs as the success path: `keep_scratch`
    (and `debug`) exist so the run's working files can be inspected -- which is most
    valuable precisely when the render failed.

    A characterization test, not TDD: before the `finally` existed, a failed render
    never cleaned up at all, so this held vacuously. It guards the mutation where
    the new cleanup ignores the opt-outs it was given."""
    import pytest

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out_fail_keep")

    def _boom(*a, **kw):
        raise RuntimeError("boom mid-render")

    monkeypatch.setattr(orch_module, "execute_plan", _boom)

    with pytest.raises(RuntimeError, match="boom mid-render"):
        _run(monkeypatch, target_dir, out_dir, overrides={"star_forks": False}, keep_scratch=True)

    assert glob.glob(os.path.join(out_dir, "_work_*")) != []


def test_scratch_cleanup_failure_warns_and_never_masks_the_render_failure(tmp_path, monkeypatch, capsys):
    """CHANGE_SAFETY rule 5, last bullet: scratch is incidental cleanup, so a
    removal failure warns. It must not replace the exception the `finally` is
    unwinding -- the user needs to read why the RENDER failed, not why a temp dir
    could not be deleted."""
    import pytest

    from seestar.workspace import Workspace

    target_dir = _make_target_dir(tmp_path)
    out_dir = str(tmp_path / "out_cleanup_fail")

    def _boom(*a, **kw):
        raise RuntimeError("boom mid-render")

    def _cleanup_boom(self):
        raise OSError("simulated scratch removal failure")

    monkeypatch.setattr(orch_module, "execute_plan", _boom)
    monkeypatch.setattr(Workspace, "cleanup", _cleanup_boom)

    with pytest.raises(RuntimeError, match="boom mid-render"):
        _run(monkeypatch, target_dir, out_dir, overrides={"star_forks": False})

    assert "working files" in capsys.readouterr().err
