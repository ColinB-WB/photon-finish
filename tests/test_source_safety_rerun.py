"""A failed rerun must not clobber a prior successful run's HERO deliverable -- *Source
data is sacred* (docs/TENETS.md), extended to hard-won prior OUTPUTS, not just the
original FITS. `orchestrate.py`'s candidate lifecycle renders the new hero into the run's
SCRATCH dir under a CANDIDATE-role ref, measures/validates it there, and only
`os.replace`s it onto the real `{target}_{out_name}.*` deliverable path in output_dir once
that validation passes (orchestrate.py `run_target`, the "candidate lifecycle" section).

Task 7b extends the SAME guarantee to fork exports (`{T}_stars-on`/`{T}_starless`/
`{T}_star-reduced`/`{T}_closeup` -- the DEFAULT deliverable set for showcase nebulae/
galaxies, CLAUDE.md Stage 5): every non-hero Export step is now ALSO converted to a
CANDIDATE-role ref before `execute_plan` runs, and only promoted (`os.replace`d into
output_dir) in a dedicated loop after everything that could still fail (hero validation/
recovery) has succeeded. Before this fix, fork exports wrote straight into output_dir
during the very first `execute_plan` call -- unconditionally, regardless of whether the
run ultimately failed -- which is the clobber the reviewer reproduced.

Four tests: the hero guarantee (a crash injected inside hero validation, after the new
render already exists in scratch but before promotion, must leave a prior successful
run's hero deliverable byte-for-byte untouched); the SAME guarantee for a prior fork
deliverable (forks enabled, default showcase LP-nebula `star_forks: True`); and a
positive control for EACH (the identical harness WITHOUT the injected failure
legitimately overwrites the prior hero/fork deliverable) that guards its matching
failure test against ever passing vacuously -- e.g. if a future refactor moved the
injection point earlier than where promotion actually happens, or silently disabled
fork emission so the fork promotion loop never ran at all."""

from __future__ import annotations

import json
import os

import pytest

import seestar.orchestrate as orch_module
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
    target_dir.mkdir(parents=True)  # parents=True: Task 7's recipe test passes a not-yet-created tmp_path subdir
    write_fake_stack(target_dir / "Stacked_51_M_10.0s_LP_20260615-221349.fit", obj="M")
    return str(target_dir)


def _cfg():
    cfg = load_config()
    cfg["engine"]["trail_removal"]["enabled"] = False
    cfg["engine"]["catalog_prior"] = False
    return cfg


def _install_common_fakes(monkeypatch):
    """Same hermetic harness as tests/test_cleanup.py: real small files written by fake
    adapters at the tool boundary, so run_target executes end-to-end with no external
    Siril/GraXpert/StarNet2/exiftool binaries."""
    from conftest import fake_probe_metric, install_fake_op_adapters, install_fake_run_siril

    import seestar.execute as exec_module
    import seestar.orchestrate as orch_module
    import seestar.qa as qa_module

    monkeypatch.setattr(orch_module, "resolve_tools", lambda cfg: _FakeTools())
    monkeypatch.setattr(orch_module, "embed", lambda *a, **kw: None)
    install_fake_op_adapters(monkeypatch)
    install_fake_run_siril(monkeypatch)
    monkeypatch.setattr(exec_module, "_probe_metric", fake_probe_metric)
    from conftest import fake_measure_subject_support

    import seestar.measure as measure_module

    monkeypatch.setattr(measure_module, "measure_subject_support", fake_measure_subject_support)
    monkeypatch.setattr(qa_module, "input_star_metrics", lambda *a, **kw: {})


def _force_processing_failed(monkeypatch):
    """Make evaluate_hero classify the candidate PROCESSING_FAILED regardless of gates."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch_module.evaluate_hero

    def _failed(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero,
            outcome=HeroOutcome.PROCESSING_FAILED,
            processing_failed_gates=("injected",),
            reasons=("injected failure",),
        )

    monkeypatch.setattr(orch_module, "evaluate_hero", _failed)


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


def _force_source_limited(monkeypatch):
    """Make evaluate_hero classify the candidate SOURCE_LIMITED regardless of gates --
    still a CERTIFIED outcome (spec §3 invariants: "source_limited remains a certified
    outcome and publishes normal finals"), so the matrix's expect_certified=True cell
    for this force is load-bearing, not incidental."""
    from dataclasses import replace as dc_replace

    from seestar.core.types import HeroOutcome

    real = orch_module.evaluate_hero

    def _limited(*a, **kw):
        hero = real(*a, **kw)
        return dc_replace(
            hero, outcome=HeroOutcome.SOURCE_LIMITED, processing_failed_gates=(), source_limited_gates=("fwhm",)
        )

    monkeypatch.setattr(orch_module, "evaluate_hero", _limited)


def _plant_prior_hero(out_dir):
    """A prior successful run's HERO deliverables + QA sidecar, planted with known bytes."""
    prior_tif = out_dir / "M_final.tif"
    prior_jpg = out_dir / "M_final.jpg"
    prior_png = out_dir / "M_final.png"
    prior_tif.write_bytes(b"PRIOR-TIF-DELIVERABLE-BYTES")
    prior_jpg.write_bytes(b"PRIOR-JPG-DELIVERABLE-BYTES")
    prior_png.write_bytes(b"PRIOR-PNG-DELIVERABLE-BYTES")
    qa_json = out_dir / "M_qa.json"
    qa_json.write_text('{"ok": true}')
    return prior_tif, prior_jpg, prior_png, qa_json


def test_failed_rerun_preserves_prior_hero_deliverable(tmp_path, monkeypatch):
    import seestar.orchestrate as orch_module
    import seestar.run as run_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, prior_jpg, prior_png, qa_json = _plant_prior_hero(out_dir)

    _install_common_fakes(monkeypatch)

    # Inject a failure INSIDE hero-candidate validation (orchestrate.py's `_measure_hero`,
    # called from the candidate-lifecycle loop). By the time this runs, execute_plan()
    # has already rendered the new hero candidate into the run's scratch dir -- so this
    # is a genuine "failed partway through" rerun, not a no-op that never touched
    # anything. `measure_render` is called before `decide_candidate`/`os.replace`
    # promotion, so raising here must leave the real deliverable path untouched. (See
    # `test_positive_control_successful_rerun_overwrites_hero` below for proof this
    # injection point is load-bearing, not a no-op.)
    def _boom(*a, **kw):
        raise RuntimeError("injected mid-run failure: hero validation crashed")

    monkeypatch.setattr(orch_module, "measure_render", _boom)

    cfg = _cfg()
    with pytest.raises(RuntimeError, match="injected mid-run failure"):
        run_module.run_target(target_dir, str(out_dir), cfg, overrides={"star_forks": False})

    # INVARIANT -- regardless of the failure, the prior hero deliverables are untouched.
    assert prior_tif.exists()
    assert prior_tif.read_bytes() == b"PRIOR-TIF-DELIVERABLE-BYTES"
    assert prior_jpg.exists()
    assert prior_jpg.read_bytes() == b"PRIOR-JPG-DELIVERABLE-BYTES"
    assert prior_png.exists()
    assert prior_png.read_bytes() == b"PRIOR-PNG-DELIVERABLE-BYTES"
    assert qa_json.exists()
    assert qa_json.read_text() == '{"ok": true}'


def _plant_prior_fork(out_dir):
    """A prior successful run's fork deliverable (`{T}_starless`), planted with known
    bytes -- the DEFAULT showcase-nebula fork set also includes `_star-reduced`, but one
    representative fork file is enough to prove the no-clobber guarantee."""
    prior_tif = out_dir / "M_starless.tif"
    prior_jpg = out_dir / "M_starless.jpg"
    prior_png = out_dir / "M_starless.png"
    prior_tif.write_bytes(b"PRIOR-FORK-TIF-BYTES")
    prior_jpg.write_bytes(b"PRIOR-FORK-JPG-BYTES")
    prior_png.write_bytes(b"PRIOR-FORK-PNG-BYTES")
    return prior_tif, prior_jpg, prior_png


def test_failed_rerun_preserves_prior_fork_deliverable(tmp_path, monkeypatch):
    """Same guarantee as test_failed_rerun_preserves_prior_hero_deliverable above, but
    for a FORK deliverable. `_make_target_dir`'s default LP-filter target classifies as
    a nebula, and the default `profile="showcase"` gives `star_forks: True` -- so this
    run emits the same `_final`/`_star-reduced`/`_starless` set as
    test_run_ops.py::test_extended_showcase_emits_star_treatment_set_conservative_hero.
    Deliberately does NOT override `star_forks` (unlike the hero test above, which turns
    forks off to scope its coverage to the hero alone)."""
    import seestar.orchestrate as orch_module
    import seestar.run as run_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, prior_jpg, prior_png = _plant_prior_fork(out_dir)

    _install_common_fakes(monkeypatch)

    # Same injection point as the hero test: a crash inside hero-candidate validation,
    # AFTER execute_plan() has already rendered every candidate (hero AND fork) into
    # scratch, but before any promotion. Pre-fix, this assertion fails: the fork export
    # wrote straight into output_dir during the initial execute_plan() call, so the
    # prior `_starless.*` is already clobbered by the time this crash even fires.
    def _boom(*a, **kw):
        raise RuntimeError("injected mid-run failure: hero validation crashed")

    monkeypatch.setattr(orch_module, "measure_render", _boom)

    cfg = _cfg()
    with pytest.raises(RuntimeError, match="injected mid-run failure"):
        run_module.run_target(target_dir, str(out_dir), cfg)

    # INVARIANT -- regardless of the failure, the prior fork deliverable is untouched.
    assert prior_tif.exists()
    assert prior_tif.read_bytes() == b"PRIOR-FORK-TIF-BYTES"
    assert prior_jpg.exists()
    assert prior_jpg.read_bytes() == b"PRIOR-FORK-JPG-BYTES"
    assert prior_png.exists()
    assert prior_png.read_bytes() == b"PRIOR-FORK-PNG-BYTES"


def test_positive_control_successful_rerun_overwrites_hero(tmp_path, monkeypatch):
    """Non-vacuity guard for the test above: run the IDENTICAL harness minus the injected
    failure and confirm the prior hero deliverable IS legitimately overwritten. Without
    this, a future refactor that moves the crash earlier than where promotion actually
    happens (e.g. before execute_plan ever renders the candidate) could make the failure
    test above pass for the wrong reason -- because nothing ever runs, not because the
    write/cleanup contract protects a real in-flight promotion. Pinning that a normal,
    uninterrupted rerun DOES replace the file keeps the guarantee test honest."""
    import seestar.run as run_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, *_ = _plant_prior_hero(out_dir)

    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    cfg = _cfg()
    result = run_module.run_target(target_dir, str(out_dir), cfg, overrides={"star_forks": False})

    assert result["status"] is not None  # the run completed and reached the hero contract
    assert prior_tif.exists()
    assert prior_tif.read_bytes() != b"PRIOR-TIF-DELIVERABLE-BYTES"


def test_positive_control_successful_fork_rerun_overwrites_prior_fork(tmp_path, monkeypatch):
    """Non-vacuity guard for test_failed_rerun_preserves_prior_fork_deliverable above,
    mirroring test_positive_control_successful_rerun_overwrites_hero for the FORK case:
    run the IDENTICAL forks-enabled harness minus the injected failure and confirm the
    prior fork deliverable IS legitimately overwritten. Proves the fork promotion loop
    (orchestrate.py's fork `os.replace` promotion after the hero contract) actually
    reaches and overwrites `{T}_starless.tif` on a normal, uninterrupted rerun -- so the
    no-clobber fork test can't be passing vacuously (e.g. because a future refactor
    silently disabled fork emission and the promotion loop never runs at all)."""
    import seestar.run as run_module

    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, *_ = _plant_prior_fork(out_dir)

    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    cfg = _cfg()
    result = run_module.run_target(target_dir, str(out_dir), cfg)

    assert result["status"] is not None  # the run completed and reached the hero contract
    assert prior_tif.exists()
    assert prior_tif.read_bytes() != b"PRIOR-FORK-TIF-BYTES"


def test_run_target_refuses_output_equal_to_source(tmp_path):
    d = tmp_path / "M8"
    d.mkdir()
    import seestar.orchestrate as orch

    with pytest.raises(ValueError):
        orch.run_target(str(d), str(d), _cfg())
    assert not any(p.name.startswith("_work_") for p in d.iterdir())


# ---------------------------------------------------------------------------
# Task 6 (B-10): atomic accepted-run publication (hero + fork deliverables +
# plan/notes sidecars + manifest), all-or-nothing through the manifest commit.
# docs/TENETS.md tenet 7 ("a step or gate that silently changes, skips, or
# degrades is a defect") governs the transaction; tenet 2 ("source data is
# sacred") governs the NEVER-delete-or-modify-.prev_* rule for crash-era
# orphan backups.
# ---------------------------------------------------------------------------


def _raiser(exc):
    def f(*a, **k):
        raise exc

    return f


def _snapshot_prior_run(out_dir):
    """Plant a full prior ACCEPTED run and return {name: bytes}. Reuses the UNCHANGED
    _plant_prior_hero for the rasters+qa it already writes, then adds the sidecars+manifest
    the transaction now owns."""
    _plant_prior_hero(out_dir)  # M_final.{tif,png,jpg} + M_qa.json
    (out_dir / "M_plan.json").write_bytes(b'{"plan_format": 1, "prior": true}')
    (out_dir / "M_notes.json").write_bytes(b'{"prior": true}')
    (out_dir / "M_run.json").write_bytes(b'{"prior_manifest": true}')
    return {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}


def _assert_prior_intact(out_dir, prior):
    for name, data in prior.items():
        assert (out_dir / name).read_bytes() == data, f"{name} changed"
    assert not list(out_dir.glob("*.prev_*")), "in-process rollback must leave no backups"


def _replace_spy(out_dir, fail_on):
    real = os.replace
    n = {"c": 0}

    def spy(src, dst):
        if str(dst).startswith(str(out_dir)) and str(dst).endswith((".tif", ".png", ".jpg")):
            n["c"] += 1
            if n["c"] == fail_on:
                raise OSError("raster")
        return real(src, dst)

    return spy


def _inject(monkeypatch, out_dir, boundary):
    """Make exactly one transaction boundary fail."""
    if boundary == "raster":
        monkeypatch.setattr(os, "replace", _replace_spy(out_dir, fail_on=2))
    elif boundary == "embed":
        monkeypatch.setattr(orch_module, "embed", _raiser(RuntimeError("embed")))
    elif boundary == "plan":
        monkeypatch.setattr(orch_module, "write_plan_sidecar", _raiser(OSError("plan")))
    elif boundary == "manifest":
        monkeypatch.setattr(orch_module, "write_run_manifest", _raiser(OSError("manifest")))
    elif boundary == "notes":  # notes write is an inline open()+json.dump
        real_open = open

        def fake_open(path, *a, **k):
            if str(path).endswith("_notes.json"):
                raise OSError("notes")
            return real_open(path, *a, **k)

        monkeypatch.setattr("builtins.open", fake_open)


# ONE parameterized test exercises every NEW transaction boundary (reviewer point 4).
@pytest.mark.parametrize("boundary", ["raster", "embed", "plan", "notes", "manifest"])
def test_transaction_rolls_back_at_each_boundary(tmp_path, monkeypatch, boundary):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)  # patches orch_module.embed to no-op...
    _force_pass(monkeypatch)  # exercise the certified namespace (uncertified boundary matrix is Task 8)
    prior = _snapshot_prior_run(out_dir)
    _inject(monkeypatch, out_dir, boundary)  # ...so inject AFTER to override it
    with pytest.raises((OSError, RuntimeError)):
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    _assert_prior_intact(out_dir, prior)  # rasters+qa+plan+notes+manifest byte-identical


def test_first_run_failure_leaves_no_transaction_outputs(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()  # NO prior run -> new files have no backup
    _install_common_fakes(monkeypatch)
    monkeypatch.setattr(orch_module, "write_run_manifest", _raiser(OSError("manifest")))
    with pytest.raises(OSError):
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    survivors = {p.name for p in out_dir.iterdir() if p.is_file() and not p.name.startswith("_work")}
    txn = {
        n
        for n in survivors
        if n.endswith((".tif", ".png", ".jpg"))
        or n
        in (
            "M_plan.json",
            "M_notes.json",
            "M_run.json",
            "M_uncertified_plan.json",
            "M_uncertified_notes.json",
            "M_uncertified_run.json",
        )
    }
    assert txn == set(), f"first-run rollback left transaction outputs: {txn}"  # non-txn qa.json may remain


def test_forks_enabled_failure_restores_hero_and_forks(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)  # certified-path intent: prior M_final.*/fork files must actually
    # enter the transaction so the restore loop runs (else this test is vacuous)
    _snapshot_prior_run(out_dir)
    _plant_prior_fork(out_dir)  # prior hero + fork deliverables
    prior = {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}
    monkeypatch.setattr(orch_module, "write_run_manifest", _raiser(OSError("manifest")))
    with pytest.raises(OSError):
        run_module.run_target(target_dir, str(out_dir), _cfg())  # forks default ON (showcase LP nebula)
    _assert_prior_intact(out_dir, prior)


def test_orphan_backup_preserved_and_warned_across_successful_render(tmp_path, monkeypatch, capsys):
    """Owner condition 3: a crash-era .prev_ orphan is left byte-identical, a warning is
    surfaced, and a successful rerender still completes (warn-and-continue)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)  # certified-path intent: asserts M_final.tif exists after the rerender
    orphan = out_dir / "M_final.tif.prev_deadbeef"  # a crash-era recovery backup
    orphan.write_bytes(b"CRASH-ERA-BACKUP")
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert orphan.read_bytes() == b"CRASH-ERA-BACKUP"  # (1) untouched, byte-identical
    assert "interrupted" in capsys.readouterr().err.lower()  # (2a) CLI stderr surface
    assert any(
        "interrupted" in str(w).lower()  # (2b) GUI data path — the result must
        for w in (result.get("warnings") or [])
    ), "run result must carry the warning for the GUI"  #      carry it independently of stderr
    assert (out_dir / "M_final.tif").exists()  # (3) the rerender completed


# ---------------------------------------------------------------------------
# CHANGE_SAFETY.md rule 5 (tiered failure handling) -- class 1 (rollback
# failures must be REPORTED while PRESERVING the original exception) and class
# 2 (post-commit backup-cleanup failures must WARN, not silently pass).
# ---------------------------------------------------------------------------


def test_rollback_restore_failure_reports_and_preserves_original_and_backup(tmp_path, monkeypatch, capsys):
    """Class 1: if a backup RESTORE (`os.replace(bak, final_path)`) itself fails during
    rollback, the failure must not be silently swallowed (docs/CHANGE_SAFETY.md rule 5,
    second bullet: "Rollback failures must be reported while preserving the original
    exception ... never silently ignored."). This test injects a manifest-write failure
    (so the transaction has already promoted every raster, i.e. rollback has real work to
    do) AND makes exactly one backup restore fail. It asserts: an exception still
    propagates; the ORIGINAL failure (the manifest OSError) is reachable from it; the
    rollback failure is reported on stderr; and the un-restored backup file is left on
    disk, never deleted -- tenet 2, "source data is sacred"."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)  # certified-path intent: fail_final targets the certified M_final.tif backup
    prior = _snapshot_prior_run(out_dir)

    real_replace = os.replace
    fail_final = str(out_dir / "M_final.tif")

    def spy(src, dst):
        # A restore call is `os.replace(bak, final_path)`: src is the ".prev_" backup,
        # dst is the real deliverable path. Allow the initial backup-aside moves
        # (dst has ".prev_") and every promotion (src is a scratch candidate) through
        # unchanged; fail only the restore of THIS ONE final path.
        if ".prev_" in os.path.basename(str(src)) and str(dst) == fail_final:
            raise OSError("simulated rollback restore failure")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    monkeypatch.setattr(orch_module, "write_run_manifest", _raiser(OSError("manifest")))

    with pytest.raises((OSError, RuntimeError)) as excinfo:
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})

    # The original failure must be reachable, either as the propagated exception
    # itself or chained as __cause__/__context__.
    exc = excinfo.value
    chain = [exc]
    while chain[-1].__cause__ is not None:
        chain.append(chain[-1].__cause__)
    assert any(isinstance(e, OSError) and "manifest" in str(e) for e in chain), (
        f"original 'manifest' OSError not reachable from propagated exception chain: {chain}"
    )

    err = capsys.readouterr().err
    assert "rollback" in err.lower()
    assert fail_final in err or os.path.basename(fail_final) in err

    # The un-restored backup must still exist on disk, byte-identical, never deleted.
    leftover = list(out_dir.glob("M_final.tif.prev_*"))
    assert len(leftover) == 1, f"expected exactly one un-restored backup, found {leftover}"
    assert leftover[0].read_bytes() == prior["M_final.tif"]


def test_backup_cleanup_failure_warns_but_run_still_succeeds(tmp_path, monkeypatch, capsys):
    """Class 2: a failure removing a post-commit backup (this run's OWN `.prev_*` file,
    dropped only after the manifest write already succeeded) must not be silently
    swallowed -- docs/CHANGE_SAFETY.md rule 5, third bullet: "Post-commit cleanup
    failures ... must produce a warning -- leftover backups affect later runs." The run
    itself must still succeed (the commit already happened; this is cleanup, not a
    publication failure). The warning must be surfaced on BOTH stderr and
    result["warnings"] independently (rule 8: never `A or B` when both are required)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)  # certified-path intent: leftover backup pattern is M_final.tif.prev_*
    _snapshot_prior_run(out_dir)

    real_remove = os.remove

    def spy(path):
        p = str(path)
        if ".tif.prev_" in p:
            raise OSError("simulated backup cleanup failure")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", spy)

    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})

    # The run still succeeded -- the commit happened, a result came back.
    assert result["status"] is not None
    assert (out_dir / "M_run.json").exists()

    # The leftover backup remains (cleanup failed, but nothing was lost).
    leftover = list(out_dir.glob("M_final.tif.prev_*"))
    assert len(leftover) == 1, f"expected exactly one leftover backup, found {leftover}"

    err = capsys.readouterr().err
    assert "cleanup" in err.lower() or "backup" in err.lower()
    assert any(("cleanup" in str(w).lower() or "backup" in str(w).lower()) for w in (result.get("warnings") or [])), (
        "run result must carry the backup-cleanup warning for the GUI"
    )


def _assert_identity_closure(out_dir, manifest_path, target, out_name, certified):
    """Per-identity referential integrity (spec §8 rev 5): every referenced file
    exists; every on-disk file of this identity is accounted for; namespace purity.
    Also asserts extras attribution: an annotated JPG of this identity on disk is
    exactly an expected extras name (red-team finding 22). Also asserts (Task 10
    review carry-over) that the identity's plan sidecar `execution.published`
    mapping, when present, agrees with reality: every `outputs` value is a
    basename STEM that resolves to at least one on-disk raster file -- a stale or
    fabricated published-path entry would otherwise go unnoticed since nothing
    else in this suite reads that mapping back against the filesystem."""
    from seestar.core.publication import (
        FORK_LABELS,
        QA_ARTIFACT_LABELS,
        annotated_name,
        published_base,
        published_fork_base,
        published_qa_base,
        sidecar_names,
    )

    man = json.loads(manifest_path.read_text())
    referenced: set[str] = set()
    for d in man.get("deliverables") or []:
        for ext in ("tif", "png", "jpg"):
            if d.get(ext):
                referenced.add(os.path.basename(d[ext]))
    referenced |= {os.path.basename(p) for p in man.get("qa_artifacts") or []}
    for b in referenced:
        assert (out_dir / b).exists(), f"manifest references missing file {b}"
        assert ("_uncertified" in b) == (not certified), f"namespace purity violated by {b}"
    extras = {
        annotated_name(target, out_name, certified),
        sidecar_names(target, out_name, certified)["recipe"],
        sidecar_names(target, out_name, certified)["qa"],
    }
    identity_bases = (
        {published_base(target, out_name, certified)}
        | {published_fork_base(target, f, certified) for f in FORK_LABELS}
        # QA-artifact bases (support raster, frozen solve region) are part of this
        # identity too: a promoted-but-unreferenced raster is exactly the orphan
        # this closure exists to catch, and the solve region was added to the
        # transaction on 2026-08-14.
        | {published_qa_base(target, label, certified) for label in QA_ARTIFACT_LABELS}
    )
    for p in out_dir.iterdir():
        stem, _, ext = p.name.rpartition(".")
        if ext in ("tif", "png", "jpg") and stem in identity_bases:
            assert p.name in referenced or p.name in extras, f"orphan {p.name} unaccounted by its identity manifest"
        if p.name.endswith("_annotated.jpg") and (("_uncertified" in p.name) == (not certified)):
            assert p.name in extras, f"unexpected annotated artifact {p.name}"

    plan_path = out_dir / sidecar_names(target, out_name, certified)["plan"]
    if plan_path.exists():
        plan_doc = json.loads(plan_path.read_text())
        published = (plan_doc.get("execution") or {}).get("published")
        if published:
            for orig_base, stem in (published.get("outputs") or {}).items():
                assert any((out_dir / f"{stem}.{ext}").exists() for ext in ("tif", "png", "jpg")), (
                    f"execution.published outputs[{orig_base!r}] = {stem!r} names no on-disk raster"
                )


def test_manifest_referential_integrity_after_successful_run(tmp_path, monkeypatch):
    """docs/CHANGE_SAFETY.md's named invariant test: after a SUCCESSFUL accepted-run
    transaction (the manifest write is the commit point -- see orchestrate.py's "THE
    COMMIT" comment), the committed `{target}_run.json` manifest is in per-identity
    referential closure with the output dir (`_assert_identity_closure`, spec §8 rev
    5): every path it references exists; every on-disk raster of this identity's
    hero/fork bases is either a referenced deliverable, a qa_artifact, or a named
    extra (recipe/qa/annotated sidecar); namespace purity holds; and the plan
    sidecar's `execution.published` mapping (when present) names real files.

    CHANGE_SAFETY.md rule 10: this is a CHARACTERIZATION test, not TDD -- the
    closure already holds by construction from the publication transaction built in
    prior tasks (Tasks 1-13). It guards against a future regression re-introducing
    an unaccounted orphan file, a manifest entry pointing at a missing/foreign-
    namespace file, or a stale `execution.published` entry -- the flat "on-disk set
    == referenced set" equation this replaces could not have caught misattribution
    within a mixed hero+fork+qa_artifact output dir, only a size/membership mismatch
    across the whole directory. Green command:
    `pytest tests/test_source_safety_rerun.py::test_manifest_referential_integrity_after_successful_run`."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    cfg = _cfg()
    result = run_module.run_target(target_dir, str(out_dir), cfg)  # forks default ON (showcase LP nebula)
    assert result["status"] is not None  # the run completed and reached the hero contract

    manifest_path = out_dir / "M_run.json"
    assert manifest_path.exists()
    _assert_identity_closure(out_dir, manifest_path, "M", "final", True)


def test_frozen_solve_region_is_published_as_a_manifest_qa_artifact(tmp_path, monkeypatch):
    """Owner ruling (2026-08-14): the frozen solve region must be REPRODUCIBLE by a
    later `seestar qa`, so its exact pixel membership is published inside the same
    transaction as every other member -- never recomputed from the final render
    and called equivalent.

    Asserts each promised surface independently (CHANGE_SAFETY rule 8): the
    manifest references it, the raster exists on disk under the namespace-aware
    published name, the run's own provenance names the same artifact, and it is a
    SEPARATE file from the structure-support raster.

    Green command:
    `pytest tests/test_source_safety_rerun.py::test_frozen_solve_region_is_published_as_a_manifest_qa_artifact`."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    result = run_module.run_target(target_dir, str(out_dir), _cfg())  # LP nebula -> body_p90 solve

    man = json.loads((out_dir / "M_run.json").read_text())
    assert "M_solve_region.png" in man["qa_artifacts"]
    assert (out_dir / "M_solve_region.png").exists()
    # the structure support is a DIFFERENT artifact, published alongside it
    assert "M_support.png" in man["qa_artifacts"]
    assert (out_dir / "M_support.png").exists()
    # provenance and publication agree on which raster the solve measured over
    notes = result["notes"]
    assert notes["stretch_solve"]["region_source"] == "frozen_object_mask"
    assert notes["solve_region"]["artifact"] == "M_solve_region"
    assert notes["solve_region"]["valid"] is True
    _assert_identity_closure(out_dir, out_dir / "M_run.json", "M", "final", True)


def test_manifest_referential_integrity_both_namespaces_after_failed_rerun_over_certified(tmp_path, monkeypatch):
    """Both-namespaces arm of the invariant test above (spec §8 rev 5 "both" prior
    state): reuses Task 3's failed-rerun-over-certified scenario
    (`test_failed_rerun_over_certified_leaves_both_sets_consistent` -- a prior
    certified hero on disk, then a processing_failed rerun that must publish under
    the `_uncertified` namespace without touching the certified generation), then
    asserts per-identity closure independently on BOTH the untouched certified
    manifest and the freshly-committed uncertified manifest. A closure check scoped
    to only one namespace could pass while the coexisting generation's files were
    silently mismatched (e.g. an uncertified export cross-attributed to the
    certified manifest, or vice versa) -- exactly the failure mode namespace purity
    inside `_assert_identity_closure` targets, now exercised with two REAL,
    concurrently-present identities rather than one.

    CHANGE_SAFETY.md rule 10: CHARACTERIZATION, not TDD -- Task 3 already
    implements the namespace separation this asserts. Green command:
    `pytest tests/test_source_safety_rerun.py::test_manifest_referential_integrity_both_namespaces_after_failed_rerun_over_certified`."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, prior_jpg, prior_png, qa_json = _plant_prior_hero(out_dir)
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [
                    {"variant": "final", "tif": str(prior_tif), "jpg": str(prior_jpg), "png": str(prior_png)}
                ],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    (out_dir / "M_plan.json").write_text('{"plan_format": 1}')
    (out_dir / "M_notes.json").write_text('{"notes": {}}')
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "processing_failed"

    _assert_identity_closure(out_dir, out_dir / "M_run.json", "M", "final", True)
    _assert_identity_closure(out_dir, out_dir / "M_uncertified_run.json", "M", "final", False)


# ---------------------------------------------------------------------------
# Task 3 (spec rev 6): namespace-aware transaction targets. A processing_failed
# hero must publish under the `_uncertified` namespace, never `_final` -- the
# Phase 3C best-effort-`_final` decision is REVERSED.
# ---------------------------------------------------------------------------


def test_failed_rerun_over_certified_leaves_both_sets_consistent(tmp_path, monkeypatch):
    """Spec §4 prior-run behavior + §8 'both' prior state."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_tif, prior_jpg, prior_png, qa_json = _plant_prior_hero(out_dir)
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [
                    {"variant": "final", "tif": str(prior_tif), "jpg": str(prior_jpg), "png": str(prior_png)}
                ],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    (out_dir / "M_plan.json").write_text('{"plan_format": 1}')
    (out_dir / "M_notes.json").write_text('{"notes": {}}')
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "processing_failed"
    assert prior_tif.read_bytes() == b"PRIOR-TIF-DELIVERABLE-BYTES"
    assert json.loads((out_dir / "M_run.json").read_text())["outcome"] == "pass"
    assert (out_dir / "M_plan.json").read_text() == '{"plan_format": 1}'
    assert (out_dir / "M_notes.json").read_text() == '{"notes": {}}'
    assert (out_dir / "M_uncertified.tif").exists()
    assert (out_dir / "M_uncertified_run.json").exists()


def _make_cluster_target_dir(tmp_path):
    from conftest import write_fake_stack

    target_dir = tmp_path / "C_import"
    target_dir.mkdir()
    write_fake_stack(target_dir / "Stacked_51_C_10.0s_IRCUT_20260615-221349.fit", obj="C", filt="IRCUT")
    return str(target_dir)


def test_failed_cluster_rerun_preserves_prior_qa_starless(tmp_path, monkeypatch):
    """The concentrated-cluster QA starless is transactional: a failed rerun leaves
    the prior certified <T>_starless.tif byte-identical and publishes its own as
    <T>_starless_uncertified.tif (spec §3 rev 4, critic finding 1)."""
    import seestar.measure

    target_dir = _make_cluster_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_starless = out_dir / "C_starless.tif"
    prior_starless.write_bytes(b"PRIOR-QA-STARLESS-BYTES")
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    monkeypatch.setattr(
        seestar.measure,
        "measure_concentration",
        lambda *a, **kw: {"concentration": 999.0, "center_xy": [0, 0], "block": 32},
    )
    result = run_module.run_target(
        target_dir, str(out_dir), _cfg(), overrides={"structure_enabled": True, "star_forks": False}
    )
    assert result["status"] == "processing_failed"
    assert (out_dir / "C_starless_uncertified.tif").exists()
    assert prior_starless.read_bytes() == b"PRIOR-QA-STARLESS-BYTES"
    man = json.loads((out_dir / "C_uncertified_run.json").read_text())
    # Task 4 (Diagnoses 5+3): the concentrated cluster also publishes its frozen
    # subject-support raster as a qa_artifact alongside the QA starless
    assert man["qa_artifacts"] == ["C_starless_uncertified.tif", "C_support_uncertified.png"]
    assert (out_dir / "C_support_uncertified.png").exists()


def test_certified_cluster_rollback_restores_prior_qa_starless(tmp_path, monkeypatch):
    """Certified-direction counterpart to test_failed_cluster_rerun_preserves_prior_qa_starless:
    a CERTIFIED cluster rerun (forced PASS) whose manifest write fails must roll back the
    QA-artifact promotion too -- the prior certified C_starless.tif is restored byte-identical
    and no `.prev_*` backup is left behind (Task 4 fix -- QA-artifact promotion now resolves its
    published name through `_published_base_for`, the same guarded path/transaction as every
    other deliverable)."""
    import seestar.measure

    target_dir = _make_cluster_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prior_starless = out_dir / "C_starless.tif"
    prior_starless.write_bytes(b"PRIOR-CERTIFIED-QA-STARLESS-BYTES")
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    monkeypatch.setattr(
        seestar.measure,
        "measure_concentration",
        lambda *a, **kw: {"concentration": 999.0, "center_xy": [0, 0], "block": 32},
    )
    monkeypatch.setattr(orch_module, "write_run_manifest", _raiser(OSError("manifest")))
    with pytest.raises(OSError):
        run_module.run_target(
            target_dir, str(out_dir), _cfg(), overrides={"structure_enabled": True, "star_forks": False}
        )
    assert prior_starless.read_bytes() == b"PRIOR-CERTIFIED-QA-STARLESS-BYTES"
    assert not list(out_dir.glob("C_starless.tif.prev_*"))


def test_certified_run_still_publishes_final(tmp_path, monkeypatch):
    """Positive control: with _force_pass the certified path is byte-identical to today."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    assert (out_dir / "M_final.tif").exists()
    assert (out_dir / "M_run.json").exists()
    assert not (out_dir / "M_uncertified.tif").exists()
    assert result["published"] == {
        "hero_base": "M_final",
        "sidecars": {"plan": "M_plan.json", "notes": "M_notes.json", "run": "M_run.json"},
        "certified": True,
    }


def test_fewer_forks_uncertified_replacement_removes_obsolete_member(tmp_path, monkeypatch):
    """Spec §4 union rule: an uncertified rerun that plans fewer outputs removes the
    prior uncertified run's obsolete members inside the transaction."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "M_starless_uncertified.tif"
    stale.write_bytes(b"OBSOLETE-UNCERTIFIED-FORK")
    (out_dir / "M_uncertified.tif").write_bytes(b"OLD-UNCERTIFIED-HERO")
    (out_dir / "M_uncertified_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "processing_failed",
                "source_identity": None,
                "deliverables": [
                    {"variant": "final", "tif": "M_uncertified.tif"},
                    {"variant": "starless", "tif": "M_starless_uncertified.tif"},
                ],
                "qa_artifacts": [],
                "superseded": [],
                "plan_sidecar": "M_uncertified_plan.json",
                "notes_sidecar": "M_uncertified_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "processing_failed"
    assert not stale.exists(), "obsolete prior uncertified member must be removed by the union rule"
    assert (out_dir / "M_uncertified.tif").read_bytes() != b"OLD-UNCERTIFIED-HERO"


def test_invalid_prior_uncertified_manifest_falls_back_to_new_set_only(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    innocent = out_dir / "M_starless_uncertified.tif"
    innocent.write_bytes(b"KEEP-ME")
    (out_dir / "M_uncertified_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "processing_failed",
                "source_identity": None,
                "deliverables": [
                    {"variant": "x", "tif": "M_evil_uncertified.tif"},
                    {"variant": "starless", "tif": "M_starless_uncertified.tif"},
                ],
                "qa_artifacts": [],
                "superseded": [],
                "plan_sidecar": "M_uncertified_plan.json",
                "notes_sidecar": "M_uncertified_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    # one invalid member ("evil" is not a closed-set fork) voids the WHOLE inventory:
    # the innocent listed member survives, and the warning names the unaccounted files
    assert innocent.exists() and innocent.read_bytes() == b"KEEP-ME"
    joined = " ".join(result.get("warnings", []))
    assert "uncertified manifest" in joined
    assert "M_starless_uncertified.tif" in joined  # listing-for-reporting names it


@pytest.mark.parametrize(
    "manifest_bytes",
    [
        pytest.param(b"null", id="null"),
        pytest.param(b"[]", id="empty-list"),
        pytest.param(
            json.dumps({"deliverables": ["M_uncertified.tif"], "outcome": "processing_failed"}).encode("utf-8"),
            id="deliverables-entry-not-dict",
        ),
        pytest.param(
            json.dumps({"deliverables": [{"tif": 123}], "outcome": "processing_failed"}).encode("utf-8"),
            id="deliverable-ext-not-str",
        ),
        pytest.param(
            json.dumps({"qa_artifacts": [7], "outcome": "processing_failed", "deliverables": []}).encode("utf-8"),
            id="qa-artifact-not-str",
        ),
        pytest.param(b"\xff\xfe\x00\x01 this is not valid utf-8 or json", id="non-utf8-bytes"),
        pytest.param(
            json.dumps({"outcome": "processing_failed", "deliverables": 5}).encode("utf-8"),
            id="deliverables-scalar-int",
        ),
        pytest.param(
            json.dumps({"outcome": "processing_failed", "deliverables": True}).encode("utf-8"),
            id="deliverables-scalar-bool",
        ),
        pytest.param(
            json.dumps({"outcome": "processing_failed", "deliverables": [], "qa_artifacts": 5}).encode("utf-8"),
            id="qa-artifacts-scalar-int",
        ),
    ],
)
def test_malformed_uncertified_manifest_shapes_degrade_not_crash(tmp_path, monkeypatch, manifest_bytes):
    """A malformed same-identity uncertified manifest -- wrong top-level type, a
    non-dict deliverables entry, a non-str member value, or undecodable bytes -- must
    degrade to (None, []) (warn-and-proceed at the call site) rather than raising out of
    run_target (Task 5 review finding: `_read_valid_uncertified_manifest` used to crash
    on these shapes, e.g. `None.get(...)` for a top-level `null`)."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    innocent = out_dir / "M_starless_uncertified.tif"
    innocent.write_bytes(b"KEEP-ME")
    (out_dir / "M_uncertified_run.json").write_bytes(manifest_bytes)

    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})

    assert result["status"] == "processing_failed"
    assert innocent.exists() and innocent.read_bytes() == b"KEEP-ME"
    joined = " ".join(result.get("warnings", []))
    assert "uncertified manifest" in joined


# ---- Task 7 (spec rev 6, §5/§8): generation-aware post-commit cleanup ----


def _plant_uncertified_set(out_dir, target="M"):
    files = {
        "hero_tif": out_dir / f"{target}_uncertified.tif",
        "hero_jpg": out_dir / f"{target}_uncertified.jpg",
        "plan": out_dir / f"{target}_uncertified_plan.json",
        "notes": out_dir / f"{target}_uncertified_notes.json",
        "qa": out_dir / f"{target}_uncertified_qa.json",
    }
    for p in files.values():
        p.write_bytes(b"UNCERTIFIED-SET")
    man = out_dir / f"{target}_uncertified_run.json"
    man.write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "processing_failed",
                "source_identity": None,
                "deliverables": [
                    {"variant": "final", "tif": f"{target}_uncertified.tif", "jpg": f"{target}_uncertified.jpg"}
                ],
                "qa_artifacts": [],
                "superseded": [],
                "plan_sidecar": f"{target}_uncertified_plan.json",
                "notes_sidecar": f"{target}_uncertified_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    files["run"] = man
    return files


def test_certified_run_cleans_stale_uncertified_set_post_commit(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    planted = _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    for key in ("hero_tif", "hero_jpg", "plan", "notes", "run", "qa"):
        assert not planted[key].exists(), f"stale uncertified {key} must be removed"
    man = json.loads((out_dir / "M_run.json").read_text())
    assert "M_uncertified.tif" in man["superseded"]


def test_certified_transaction_failure_leaves_uncertified_set_untouched(tmp_path, monkeypatch):
    """Generation safety (spec rev 6): NOTHING deletes before the certified commit."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    planted = _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    def _boom(path, manifest):
        raise OSError("injected manifest-write failure")

    monkeypatch.setattr(orch_module, "write_run_manifest", _boom)
    with pytest.raises(OSError, match="injected"):
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    for p in planted.values():
        assert p.exists(), "no uncertified file may be touched when the commit never happened"


def test_stale_record_completes_only_when_no_uncertified_manifest_exists(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    orphan = out_dir / "M_starless_uncertified.tif"  # C1 removed the manifest, crashed before members
    orphan.write_bytes(b"LEFTOVER")
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [],
                "qa_artifacts": [],
                "superseded": ["M_starless_uncertified.tif"],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    assert not orphan.exists(), "repair completes the interrupted cleanup post-commit"


def test_corrupt_present_uncertified_manifest_freezes_both_inventories(tmp_path, monkeypatch):
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    u2_file = out_dir / "M_uncertified.tif"
    u2_file.write_bytes(b"NEWER-PUBLICATION")
    (out_dir / "M_uncertified_run.json").write_text("{corrupt json")
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [],
                "qa_artifacts": [],
                "superseded": ["M_uncertified.tif"],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert u2_file.exists(), "files that may belong to a damaged newer publication are never deleted"
    assert any("M_uncertified_run.json" in w for w in result.get("warnings", [])), (
        "warning must NAME the manifest file it could not validate"
    )


def test_certified_showcase_cleanup_ignores_other_out_names(tmp_path, monkeypatch):
    """Spec §8 cross-profile isolation."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc_hero = out_dir / "M_documentation_uncertified.tif"
    doc_hero.write_bytes(b"OTHER-IDENTITY")
    doc_man = out_dir / "M_documentation_uncertified_run.json"
    doc_man.write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "processing_failed",
                "source_identity": None,
                "deliverables": [{"variant": "documentation", "tif": "M_documentation_uncertified.tif"}],
                "qa_artifacts": [],
                "superseded": [],
                "plan_sidecar": "M_documentation_uncertified_plan.json",
                "notes_sidecar": "M_documentation_uncertified_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    assert doc_hero.exists() and doc_man.exists()


def test_cleanup_handles_moved_output_root_by_basename(tmp_path, monkeypatch):
    """Spec §8 moved-output-root: members recorded with an old absolute directory
    still clean correctly by basename in the current dir."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    planted = _plant_uncertified_set(out_dir)
    man = json.loads(planted["run"].read_text())
    man["deliverables"] = [
        {
            "variant": "final",
            "tif": "/old/root/that/moved/M_uncertified.tif",
            "jpg": "/old/root/that/moved/M_uncertified.jpg",
        }
    ]
    planted["run"].write_text(json.dumps(man))
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    assert not planted["hero_tif"].exists(), "basename-resolved cleanup must still work after a move"


def test_certified_cleanup_removes_unedited_uncertified_recipe_keeps_edited(tmp_path, monkeypatch):
    """Spec §5 recipe edit-preservation, both arms."""
    for pinned, should_survive in ((False, False), (True, True)):
        out_dir = tmp_path / f"out_{pinned}"
        out_dir.mkdir()
        target_dir = _make_target_dir(tmp_path / f"src_{pinned}")
        _plant_uncertified_set(out_dir)
        recipe = out_dir / "M_uncertified_recipe.json"
        recipe.write_text(
            json.dumps(
                {"adjustments": [{"adjustment": "saturation", "value": 0.6, "your_value": 0.8 if pinned else None}]}
            )
        )
        _install_common_fakes(monkeypatch)
        _force_pass(monkeypatch)
        result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
        assert result["status"] == "pass"
        assert recipe.exists() is should_survive
        if pinned:
            assert any("your_value pins" in w for w in result.get("warnings", []))


def test_generation_safety_sequence(tmp_path, monkeypatch):
    """Spec §8 rev 6, the full ordering regression:
    C1 supersedes U1 + killed during member cleanup -> U2 published under the same
    names -> C2 failure-injected preserves U2 byte-identical -> successful C2 cleans U2."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # State after interrupted C1: certified manifest carries superseded; U1's manifest
    # already removed (manifest-first); one member file survived the crash.
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [],
                "qa_artifacts": [],
                "superseded": ["M_uncertified.tif", "M_uncertified.jpg"],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    leftover = out_dir / "M_uncertified.jpg"
    leftover.write_bytes(b"U1-LEFTOVER")
    _install_common_fakes(monkeypatch)
    # U2 publishes under the same uncertified filenames:
    _force_processing_failed(monkeypatch)
    r_u2 = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert r_u2["status"] == "processing_failed"
    u2_tif_bytes = (out_dir / "M_uncertified.tif").read_bytes()
    assert (out_dir / "M_uncertified_run.json").exists()
    # C2's transaction fails -> U2 survives byte-identical (nothing deletes pre-commit):
    monkeypatch.undo()  # clears _force_processing_failed AND the common fakes; reinstall for C2
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    real_write = orch_module.write_run_manifest

    def _boom(path, manifest):
        raise OSError("injected C2 commit failure")

    monkeypatch.setattr(orch_module, "write_run_manifest", _boom)
    with pytest.raises(OSError, match="injected"):
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert (out_dir / "M_uncertified.tif").read_bytes() == u2_tif_bytes
    assert (out_dir / "M_uncertified_run.json").exists()
    # Successful C2 cleans U2's set (precedence: current inventory over the stale record):
    monkeypatch.setattr(orch_module, "write_run_manifest", real_write)
    r_c2 = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert r_c2["status"] == "pass"
    assert not (out_dir / "M_uncertified.tif").exists()
    assert not (out_dir / "M_uncertified_run.json").exists()


def test_post_commit_cleanup_manifest_removal_failure_warns_run_still_passes(tmp_path, monkeypatch, capsys):
    """CHANGE_SAFETY rule 5 class 2: a failure removing the stale uncertified MANIFEST
    during post-commit cleanup must warn (both stderr and result["warnings"],
    independently -- rule 8), never fail the already-committed run."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    planted = _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    real_remove = os.remove

    def spy(path):
        p = str(path)
        if p.endswith("M_uncertified_run.json"):
            raise OSError("simulated stale-manifest removal failure")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", spy)

    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})

    assert result["status"] == "pass"
    assert planted["run"].exists(), "removal failed, so the stale manifest is still there"

    err = capsys.readouterr().err
    assert "manifest" in err.lower()
    assert any("manifest" in str(w).lower() for w in (result.get("warnings") or [])), (
        "run result must carry the cleanup-failure warning for the GUI"
    )


def test_post_commit_cleanup_member_removal_failure_warns_run_still_passes(tmp_path, monkeypatch, capsys):
    """CHANGE_SAFETY rule 5 class 2: a failure removing one superseded MEMBER file
    during post-commit cleanup must warn (both stderr and result["warnings"],
    independently -- rule 8), never fail the already-committed run, and must not
    stop the rest of cleanup from proceeding."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    planted = _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    real_remove = os.remove

    def spy(path):
        p = str(path)
        if p.endswith("M_uncertified.tif"):
            raise OSError("simulated member removal failure")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", spy)

    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})

    assert result["status"] == "pass"
    assert planted["hero_tif"].exists(), "removal failed, so this one member is still there"
    # the rest of the set is still cleaned despite the one failure:
    assert not planted["hero_jpg"].exists()
    assert not planted["run"].exists()

    err = capsys.readouterr().err
    assert "member" in err.lower()
    assert any("member" in str(w).lower() for w in (result.get("warnings") or [])), (
        "run result must carry the cleanup-failure warning for the GUI"
    )


def test_cleanup_removes_manifest_before_members(tmp_path, monkeypatch):
    """Mutation-killing (Task 7 review finding #4a): assert the actual `os.remove`
    call ORDER puts the stale uncertified manifest before any of its members, not
    just that both eventually vanish -- an implementation with the two loops
    swapped would satisfy every existence-only assertion above while violating the
    spec §5 step 4 manifest-first ordering that makes crash-recovery safe."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)

    real_remove = os.remove
    call_order: list[str] = []

    def spy(path):
        call_order.append(os.path.basename(str(path)))
        return real_remove(path)

    monkeypatch.setattr(os, "remove", spy)

    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"

    manifest_name = "M_uncertified_run.json"
    member_names = {
        "M_uncertified.tif",
        "M_uncertified.jpg",
        "M_uncertified_plan.json",
        "M_uncertified_notes.json",
        "M_uncertified_qa.json",
    }
    assert manifest_name in call_order, "the stale manifest must actually be removed via os.remove"
    manifest_idx = call_order.index(manifest_name)
    member_indices = [i for i, n in enumerate(call_order) if n in member_names]
    assert member_indices, "expected at least one member/extra removal to be recorded"
    assert all(manifest_idx < i for i in member_indices), (
        f"manifest removal must precede every member/extra removal; order was {call_order}"
    )


def test_superseded_union_includes_prior_record_only_basenames(tmp_path, monkeypatch):
    """Mutation-killing (Task 7 review finding #4b): the CURRENT uncertified
    manifest's own inventory does not necessarily name every file an earlier
    interrupted cleanup still owes -- the prior certified manifest's `superseded`
    record can name a basename the current generation's manifest never mentions
    (e.g. a fork left over from an earlier uncertified generation). Spec §5 step 2's
    union (current inventory unioned with the prior record) must still catch it;
    a mutant that used ONLY the current manifest's inventory would leave this file
    behind forever."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _plant_uncertified_set(out_dir)  # the CURRENT valid uncertified manifest + its own members
    orphan = out_dir / "M_stars-on_uncertified.tif"  # prior-record-only: absent from the current manifest
    orphan.write_bytes(b"STALE-FORK-FROM-EARLIER-GENERATION")
    (out_dir / "M_run.json").write_text(
        json.dumps(
            {
                "manifest_format": 1,
                "attempt": 0,
                "outcome": "pass",
                "source_identity": None,
                "deliverables": [],
                "qa_artifacts": [],
                "superseded": ["M_stars-on_uncertified.tif"],
                "plan_sidecar": "M_plan.json",
                "notes_sidecar": "M_notes.json",
                "accepted_at": "2026-08-01T00:00:00",
            }
        )
    )
    _install_common_fakes(monkeypatch)
    _force_pass(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    assert result["status"] == "pass"
    assert not orphan.exists(), "prior-record-only basename must be unioned into this generation's cleanup"


# ---------------------------------------------------------------------------
# Task 8 (spec rev 6, §8): the full transaction matrix -- {pass, source_limited,
# processing_failed} x prior states {empty, certified, uncertified, both} -- plus
# the uncertified-namespace twin of the certified boundary-injection rollback
# test, plus a Task 7-review carry-over (interrupted-repair recipe edit-
# preservation on a prior-record-only name).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "force,expect_certified",
    [
        (_force_pass, True),
        (_force_source_limited, True),
        (_force_processing_failed, False),
    ],
)
@pytest.mark.parametrize("prior", ["empty", "certified", "uncertified", "both"])
def test_publication_matrix(tmp_path, monkeypatch, force, expect_certified, prior):
    """Spec §8: {pass, source_limited, processing_failed} x prior states.
    Invariants per cell: (1) the publishing identity's set is complete and
    internally consistent; (2) a failed run leaves certified files byte-identical;
    (3) a certified run removes the stale uncertified set; (4) manifests agree
    with result['status']."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    if prior in ("certified", "both"):
        prior_tif, _, _, _ = _plant_prior_hero(out_dir)
        (out_dir / "M_run.json").write_text(
            json.dumps(
                {
                    "manifest_format": 1,
                    "attempt": 0,
                    "outcome": "pass",
                    "source_identity": None,
                    "deliverables": [{"variant": "final", "tif": str(prior_tif)}],
                    "qa_artifacts": [],
                    "superseded": [],
                    "plan_sidecar": "M_plan.json",
                    "notes_sidecar": "M_notes.json",
                    "accepted_at": "2026-08-01T00:00:00",
                }
            )
        )
    if prior in ("uncertified", "both"):
        _plant_uncertified_set(out_dir)
    _install_common_fakes(monkeypatch)
    force(monkeypatch)
    result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    certified = result["published"]["certified"]
    assert certified is expect_certified
    if certified:
        assert (out_dir / "M_final.tif").exists() and (out_dir / "M_run.json").exists()
        assert not (out_dir / "M_uncertified_run.json").exists()  # cleaned (or never existed)
        assert not (out_dir / "M_uncertified.tif").exists()
    else:
        assert (out_dir / "M_uncertified.tif").exists() and (out_dir / "M_uncertified_run.json").exists()
        if prior in ("certified", "both"):
            assert (out_dir / "M_final.tif").read_bytes() == b"PRIOR-TIF-DELIVERABLE-BYTES"
            assert json.loads((out_dir / "M_run.json").read_text())["outcome"] == "pass"


@pytest.mark.parametrize("boundary", ["raster", "embed", "plan", "notes", "manifest"])
def test_uncertified_transaction_rolls_back_at_each_boundary(tmp_path, monkeypatch, boundary):
    """Uncertified twin of test_transaction_rolls_back_at_each_boundary (:319-330 helpers
    reused: `_raiser`/`_assert_prior_intact`/`_inject`): the SAME boundary matrix, forcing
    PROCESSING_FAILED over a planted prior UNCERTIFIED set -- proving the rollback
    mechanism is identical in the uncertified namespace. Task 8 review carry-over: the
    planted prior set also includes an obsolete member (`M_stars-on_uncertified.tif`,
    listed in the prior manifest's deliverables) that this run's plan does NOT produce
    (`star_forks: False`) -- so it only re-enters the transaction via spec §4's union
    rule, not via a same-name overwrite. Byte-identical-after-rollback alone doesn't
    prove the union loop ran (a mutant skipping it entirely would look identical --
    "never touched" and "backed up then restored" both leave the file untouched at
    the end), so this also spies on `os.replace` and asserts the obsolete member was
    actually backed up (staged into the transaction) before rollback restored it."""
    target_dir = _make_target_dir(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _install_common_fakes(monkeypatch)
    _force_processing_failed(monkeypatch)
    planted = _plant_uncertified_set(out_dir)
    obsolete = out_dir / "M_stars-on_uncertified.tif"
    obsolete.write_bytes(b"OBSOLETE-UNION-MEMBER")
    man = json.loads(planted["run"].read_text())
    man["deliverables"].append({"variant": "stars-on", "tif": "M_stars-on_uncertified.tif"})
    planted["run"].write_text(json.dumps(man))
    prior = {p.name: p.read_bytes() for p in out_dir.iterdir() if p.is_file()}
    _inject(monkeypatch, out_dir, boundary)  # ...so inject AFTER planting/snapshot to override the fakes

    # Mutation-killing (Task 8 review finding): `_assert_prior_intact` alone can't
    # distinguish "the union loop staged `obsolete`, then rollback restored it" from
    # "a mutant skipped the union loop (orchestrate.py ~:910 `for m in prior_members:`
    # -> empty) and never touched it at all" -- both leave `obsolete` byte-identical.
    # Wrap `os.replace` with a recording spy (the pattern from
    # test_cleanup_removes_manifest_before_members above, applied to os.replace instead
    # of os.remove) that delegates to whatever is CURRENTLY installed -- the real
    # os.replace for four of the five boundaries, or the "raster" boundary's own
    # failure-injecting spy from `_inject` above -- and additionally assert the union
    # actually staged `obsolete` into the transaction: its pre-failure backup call
    # `os.replace(obsolete, obsolete + ".prev_<hex>")` (orchestrate.py's
    # `for final_path in pre_existing: os.replace(final_path, bak)`) must have happened.
    # That backup loop runs before ANY of promotion/embed/plan/notes/manifest -- the
    # five injected boundaries -- so this assertion holds in every cell.
    current_replace = os.replace
    replace_calls: list[tuple[str, str]] = []

    def recording_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return current_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)

    with pytest.raises((OSError, RuntimeError)):
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
    _assert_prior_intact(out_dir, prior)  # prior uncertified set (incl. the obsolete union member) untouched

    obsolete_backups = [
        (src, dst)
        for src, dst in replace_calls
        if src == str(obsolete) and os.path.basename(dst).startswith(obsolete.name + ".prev_")
    ]
    assert obsolete_backups, (
        f"union loop never staged {obsolete.name} into the transaction (no backup "
        f"os.replace with it as source) -- 'restored on rollback' would be vacuous "
        f"for a mutant that skips `prior_members` entirely; recorded os.replace "
        f"calls: {replace_calls}"
    )


def test_prior_record_pinned_recipe_survives_repair(tmp_path, monkeypatch):
    """Task 7-review carry-over: the interrupted-repair path (spec §5 step 6 -- NO
    uncertified manifest FILE on disk, because an earlier crash happened between the
    manifest-first removal and member removal) must still run the recipe
    edit-preservation gate on a name it learns about ONLY via the prior certified
    manifest's `superseded` record, not merely via a freshly-read current uncertified
    manifest (test_certified_cleanup_removes_unedited_uncertified_recipe_keeps_edited
    above covers that case; this is the `elif not unc_manifest_file_exists:` branch --
    see test_stale_record_completes_only_when_no_uncertified_manifest_exists for the
    raster analog). A PINNED uncertified recipe survives with a warning naming the
    pins; its unedited counterpart is removed."""
    for pinned, should_survive in ((True, True), (False, False)):
        out_dir = tmp_path / f"repair_out_{pinned}"
        out_dir.mkdir()
        target_dir = _make_target_dir(tmp_path / f"repair_src_{pinned}")
        recipe = out_dir / "M_uncertified_recipe.json"
        recipe.write_text(
            json.dumps(
                {"adjustments": [{"adjustment": "saturation", "value": 0.6, "your_value": 0.8 if pinned else None}]}
            )
        )
        (out_dir / "M_run.json").write_text(
            json.dumps(
                {
                    "manifest_format": 1,
                    "attempt": 0,
                    "outcome": "pass",
                    "source_identity": None,
                    "deliverables": [],
                    "qa_artifacts": [],
                    "superseded": ["M_uncertified_recipe.json"],
                    "plan_sidecar": "M_plan.json",
                    "notes_sidecar": "M_notes.json",
                    "accepted_at": "2026-08-01T00:00:00",
                }
            )
        )
        _install_common_fakes(monkeypatch)
        _force_pass(monkeypatch)
        result = run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
        assert result["status"] == "pass"
        assert recipe.exists() is should_survive, f"pinned={pinned} recipe survival mismatch"
        if pinned:
            assert any("your_value pins" in w for w in result.get("warnings", []))


def test_variant_values_identical_between_namespaces(tmp_path, monkeypatch):
    """Spec §5 rev 5 delta: variant is a stable machine key; certification is EXIF-only.
    A certified and an uncertified run of the SAME target must record the exact same
    set of `variant` values in their manifests -- only the namespace (file names,
    certification EXIF field) differs, never the variant label itself."""
    variants = {}
    for label, force in (("cert", _force_pass), ("unc", _force_processing_failed)):
        out_dir = tmp_path / label
        out_dir.mkdir()
        target_dir = _make_target_dir(tmp_path / f"src_{label}")
        _install_common_fakes(monkeypatch)
        force(monkeypatch)
        run_module.run_target(target_dir, str(out_dir), _cfg(), overrides={"star_forks": False})
        man_name = "M_run.json" if label == "cert" else "M_uncertified_run.json"
        man = json.loads((out_dir / man_name).read_text())
        variants[label] = sorted(d["variant"] for d in man["deliverables"])
        monkeypatch.undo()
    assert variants["cert"] == variants["unc"]
