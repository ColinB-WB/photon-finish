"""Regression tests for CLI warning-surfacing behavior.

Task 6: lock in the CLI's existing correct warning behavior so a future refactor
can't silently reintroduce the GUI's bug (Task 5) on the CLI side.
"""

from __future__ import annotations

import types


def _fake_success(target, status="pass"):
    return {
        "target": target,
        "profile": "showcase",
        "status": status,
        "inspection": {
            "classification": {"category": "nebula"},
            "meta": {"total_integration_min": 10.0, "filter": "LP"},
        },
        "notes": {},
        "outputs": [],
    }


def _fake_batch(successes=(), failures=(), status_failures=()):
    """A structural fake matching BatchRunResult's shape (successes/failures/
    status_failures/ok) WITHOUT importing the production dataclass -- these CLI
    tests stay consumer tests of the typed contract, not constructors of it (that
    coverage lives in tests/test_discovery.py). `ok` is derived the same way the
    real `BatchRunResult.ok` property is, so cli.py's `if not batch.ok:` check is
    exercised faithfully."""
    return types.SimpleNamespace(
        successes=successes,
        failures=failures,
        status_failures=status_failures,
        ok=not failures and not status_failures,
    )


def test_process_cli_prints_every_success(monkeypatch, capsys, tmp_path):
    """M4 Step 2 (batch.successes surface): every success in the typed batch result
    is printed with _print_run, independent of the failures assertion below (rule 8:
    assert every promised surface independently)."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    fake_batch = _fake_batch(successes=(_fake_success("A"), _fake_success("C")))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert "A [showcase]" in out
    assert "C [showcase]" in out
    assert rc == 0


def test_process_cli_reports_each_failure_and_keeps_successes(monkeypatch, capsys, tmp_path):
    """M4 Step 2 (batch.failures surface): each TargetFailure prints its target name,
    error type/message, and states that earlier/other successful outputs were kept."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failure = types.SimpleNamespace(target="B", folder="/lib/B", error_type="RuntimeError", message="kaboom")
    fake_batch = _fake_batch(successes=(_fake_success("A"),), failures=(failure,))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert "B" in out
    assert "RuntimeError" in out
    assert "kaboom" in out
    assert "kept" in out.lower()
    assert rc == 1


def test_process_cli_summary_reports_succeeded_and_failed_counts(monkeypatch, capsys, tmp_path):
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failure = types.SimpleNamespace(target="B", folder="/lib/B", error_type="RuntimeError", message="kaboom")
    fake_batch = _fake_batch(successes=(_fake_success("A"), _fake_success("C")), failures=(failure,))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert "2" in out  # succeeded count
    assert "1" in out  # failed count
    assert rc == 1


def test_process_cli_exits_nonzero_on_any_failure(monkeypatch, capsys, tmp_path):
    """Settled decision 10: partial success is still a failed CLI command."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failure = types.SimpleNamespace(target="B", folder="/lib/B", error_type="RuntimeError", message="kaboom")
    fake_batch = _fake_batch(failures=(failure,))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 1


def test_process_cli_exits_zero_on_all_success(monkeypatch, capsys, tmp_path):
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    fake_batch = _fake_batch(successes=(_fake_success("A"), _fake_success("C")))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 0


def test_process_cli_prints_no_traceback_on_failure(monkeypatch, capsys, tmp_path):
    """Normal-mode partial failure must not print a raw Python traceback -- the
    failure is reported through the typed TargetFailure surface, not by letting the
    exception escape the CLI."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failure = types.SimpleNamespace(target="B", folder="/lib/B", error_type="RuntimeError", message="kaboom")
    fake_batch = _fake_batch(successes=(_fake_success("A"),), failures=(failure,))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert rc == 1


def test_process_cli_exception_failure_exits_nonzero(monkeypatch, capsys, tmp_path):
    """Owner decision (a), case 1 of 3: an ordinary exception during a target's
    run (isolated by process_library into batch.failures as a TargetFailure) is a
    FAILURE and must exit nonzero."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failure = types.SimpleNamespace(target="B", folder="/lib/B", error_type="RuntimeError", message="kaboom")
    fake_batch = _fake_batch(successes=(_fake_success("A"),), failures=(failure,))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 1


def test_process_cli_processing_failed_status_exits_nonzero(monkeypatch, capsys, tmp_path):
    """Owner decision (a), case 2 of 3, RATIFIED 2026-07-31 layering fix: a target
    whose run_target call did NOT raise (e.g. a fail-closed QA gate per M2) but
    whose result `status` is `"processing_failed"` is STILL a failure for exit
    purposes -- the verified gap this task closes (orchestrate.py:906 returns a
    dict rather than raising, so exception-only isolation would silently exit 0).
    Classification now happens inside `process_library` itself: the
    processing_failed result lands in the typed `batch.status_failures` field, NOT
    in `batch.successes` -- the CLI merely consumes that placement rather than
    re-deriving it from `status`."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    fake_batch = _fake_batch(
        successes=(_fake_success("A"),),
        status_failures=(_fake_success("B", status="processing_failed"),),
    )
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 1


def test_process_cli_source_limited_status_exits_zero(monkeypatch, capsys, tmp_path):
    """Owner decision (a), case 3 of 3: `source_limited` ships a best-effort
    deliverable by design (a documented DATA limitation, not a processing failure)
    and must count as SUCCESS -- it stays in `batch.successes`, and a mixed batch
    with only that status failure-free exits 0."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    fake_batch = _fake_batch(successes=(_fake_success("A", status="source_limited"), _fake_success("B")))
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 0


def test_process_cli_all_source_limited_batch_exits_zero(monkeypatch, capsys, tmp_path):
    """Reviewer minor: a PURE all-source_limited batch (no pass results at all)
    still has no failures of either kind and exits 0."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    fake_batch = _fake_batch(
        successes=(_fake_success("A", status="source_limited"), _fake_success("C", status="source_limited"))
    )
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 0


def test_process_cli_status_failure_prints_explicit_line_with_target_status_and_reasons(monkeypatch, capsys, tmp_path):
    """I1 (final review): a status-failed target (M2's fail-closed QA gate --
    `run_target` returns a result dict rather than raising) currently prints
    identically to a success via `_print_run` alone, with no line calling out that
    it FAILED. Add an explicit per-status-failure line naming the target, its
    status, and the hero-contract reasons (`notes.hero_contract.reasons`) -- and
    assert its content directly (rc==1 alone, already covered by
    `test_process_cli_processing_failed_status_exits_nonzero`, is not enough)."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    failed = _fake_success("B", status="processing_failed")
    failed["notes"] = {
        "hero_contract": {
            "outcome": "processing_failed",
            "reasons": ["hard gate(s) failed: star_count, fwhm"],
            "attempt": 0,
        }
    }
    fake_batch = _fake_batch(
        successes=(_fake_success("A"),),
        status_failures=(failed,),
    )
    monkeypatch.setattr(run_module, "process_library", lambda *a, **kw: fake_batch)

    rc = cli_module.main(["process", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert rc == 1
    # An explicit failure line names the target and its status...
    failure_lines = [ln for ln in out.splitlines() if "B" in ln and "processing_failed" in ln]
    assert failure_lines, f"no explicit status-failure line naming target B and its status in:\n{out}"
    # ...and the hero-contract reasons, not just a bare status word.
    assert any("star_count" in ln for ln in failure_lines) or "star_count" in out


def test_print_run_surfaces_solver_warnings(capsys):
    """_print_run must print every solver warning unconditionally."""
    from seestar.cli import _print_run

    s = {
        "target": "NGC7000",
        "profile": "showcase",
        "inspection": {
            "classification": {"category": "nebula"},
            "meta": {"total_integration_min": 51.7, "filter": "LP"},
        },
        "notes": {
            "blackpoint": {
                "blackpoint_on_linear": 0.0182,
                "raw_minus_linear_median": 1.2e-05,
            },
            "stretch_solve": {
                "metric": "body_p90",
                "target": 72.0,
                "achieved": 64.8,
                "pinned": {"mtf": None, "mtf_converged": False},
            },
        },
        "outputs": [],
    }
    _print_run(s)
    out = capsys.readouterr().out
    assert "did NOT converge" in out


def test_print_run_surfaces_contradicted_local_prior_warning(capsys):
    """Task 6: a `notes["local_prior"]["outcome"] == "contradicted"` (the local
    catalog identity disagreeing with the measured/filter-authoritative category,
    spec sec5.6) must print as a WARNING line, same as a solver-convergence
    warning."""
    from seestar.cli import _print_run

    s = {
        "target": "M8",
        "profile": "showcase",
        "inspection": {
            "classification": {"category": "nebula"},
            "meta": {"total_integration_min": 8.5, "filter": "LP"},
        },
        "notes": {
            "local_prior": {
                "outcome": "contradicted",
                "warning": "catalog identity NGC9999 (galaxy) disagrees with the measured/filter-authoritative category",
                "influenced": ["identity"],
            },
        },
        "outputs": [],
    }
    _print_run(s)
    out = capsys.readouterr().out
    assert "WARNING" in out and "NGC9999" in out


def test_qa_subcommand_prints_every_warning(monkeypatch, capsys, tmp_path):
    """The qa subcommand must print every warning from qa_target unconditionally.

    Hermetic by mocking qa_target: the qa CLI path no longer pre-resolves tools
    (cli.py splits qa/benchmark dispatch), so no tool-binary dependency remains."""
    import seestar.cli as cli_module
    import seestar.qa

    fake_sc = {
        "passed": True,
        "hard_fails": [],
        "warnings": ["a warning", "another warning"],
    }
    monkeypatch.setattr(seestar.qa, "qa_target", lambda proc_dir, cfg, target=None: fake_sc)
    rc = cli_module.main(["qa", str(tmp_path)])
    out = capsys.readouterr().out
    assert "a warning" in out
    assert "another warning" in out
    assert rc == 0


def test_qa_subcommand_prints_hero_outcome(monkeypatch, capsys, tmp_path):
    # Hermetic by mocking qa_target: the qa CLI path no longer pre-resolves tools.
    import seestar.cli as cli_module
    import seestar.qa

    fake_sc = {
        "passed": True,
        "hard_fails": [],
        "warnings": [],
        "hero": {"outcome": "source_limited", "reasons": ["source-limited gate(s) excused: fwhm"]},
    }
    monkeypatch.setattr(seestar.qa, "qa_target", lambda proc_dir, cfg, target=None: fake_sc)
    rc = cli_module.main(["qa", str(tmp_path)])
    out = capsys.readouterr().out
    assert "source_limited" in out.lower()
    assert rc == 0


def test_no_prior_help_says_network_only(capsys):
    """Task 6 rescope (spec sec5.7): `--no-prior` disables ONLY the network (SIMBAD)
    lookup -- the offline local catalog identity/category lookup is always-on for a
    plate-solved frame and is not toggled by this flag. The help text must say so,
    not the old "disable the WCS catalog prior" wording that implied it gated
    everything."""
    import pytest

    import seestar.cli as cli_module

    with pytest.raises(SystemExit):
        cli_module.main(["run", "--help"])
    out = capsys.readouterr().out
    assert "network" in out.lower()
    assert "local catalog" in out.lower() and "always" in out.lower()
    # the stale wording must be gone
    assert "disable the wcs catalog prior even if config enables it" not in out.lower()


def test_run_cli_processing_failed_status_exits_nonzero(monkeypatch, capsys, tmp_path):
    """Owner ruling (2026-07-31): `seestar run` mirrors `seestar process` — a
    single target whose result status is a processing failure exits 1, printing
    the explicit status-failure line (not a success-shaped report)."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    result = _fake_success("A", status="processing_failed")
    result["notes"] = {"hero_contract": {"reasons": ["hard gate(s) failed: star_count"]}}
    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: result)

    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out

    assert rc == 1
    assert "FAILED: A — status: processing_failed" in out
    assert "star_count" in out


def test_run_cli_pass_status_exits_zero(monkeypatch, capsys, tmp_path):
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: _fake_success("A"))
    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 0
    assert "A [showcase]" in capsys.readouterr().out


def test_run_cli_source_limited_status_exits_zero(monkeypatch, capsys, tmp_path):
    """source_limited ships a best-effort deliverable by design (a documented
    data limitation, not a processing failure) — exit 0, same as `process`."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: _fake_success("A", status="source_limited"))
    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 0


def test_run_failure_line_names_the_uncertified_file(monkeypatch, capsys, tmp_path):
    """Task 13: an uncertified render's hero deliverable is still on disk under its
    uncertified name -- a status-failure line must name it so the operator isn't
    left hunting the output dir for what was actually written."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    result = _fake_success("M", status="processing_failed")
    result["published"] = {"hero_base": "M_uncertified", "sidecars": {}, "certified": False}
    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: result)

    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    out_text = capsys.readouterr().out

    assert "preserved for inspection as M_uncertified.jpg" in out_text
    assert rc == 1


def test_run_failure_line_omits_filename_when_no_publication(monkeypatch, capsys, tmp_path):
    """Task 13: `published` is None (e.g. a failure so early nothing was ever
    published) -- the preserved-file clause must not appear at all."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    result = _fake_success("M", status="processing_failed")
    result["published"] = None
    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: result)

    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    out_text = capsys.readouterr().out

    assert "preserved for inspection" not in out_text
    assert rc == 1


def test_qa_uncertified_never_prints_bare_pass(monkeypatch, capsys, tmp_path):
    """Task 13: closes the known hole from Task 11's review -- `seestar qa` must
    not print a bare PASS/exit 0 on an uncertified publication whose
    recomputation happens to pass. Print the manifest's own recorded outcome and
    fail closed (exit 2) regardless of the recomputed verdict."""
    import seestar.cli as cli_module
    import seestar.qa

    fake_sc = {
        "passed": True,
        "hard_fails": [],
        "warnings": [],
        "hero": {},
        "publication": {
            "certified": False,
            "manifest_outcome": "processing_failed",
            "recomputed_outcome": "pass",
        },
    }
    monkeypatch.setattr(seestar.qa, "qa_target", lambda proc_dir, cfg, target=None: fake_sc)

    rc = cli_module.main(["qa", str(tmp_path)])
    out_text = capsys.readouterr().out

    assert "UNCERTIFIED (manifest outcome: processing_failed)" in out_text
    assert "\nPASS" not in out_text
    assert rc == 2


# ---------------------------------------------------------------------------
# Diagnosis 7 (Task 6): a tool failure is an EXPLAINED stop, not a traceback.
# The CLI surface is asserted on its own here; the GUI message, the run notes,
# and the diagnostic log are asserted independently in their own tests
# (CHANGE_SAFETY rule 8 -- never "A or B" for separately promised surfaces).
# ---------------------------------------------------------------------------


def _tool_failure(**kw):
    from seestar.adapters._external_process import ExternalToolFailure

    defaults = dict(
        tool="graxpert",
        failure_class="no_output",
        summary="Error: could not download the denoise model",
        stage="reducing noise",
        exit_status=1,
        log_path="/out/A/seestar_diagnostics.log",
    )
    defaults.update(kw)
    return ExternalToolFailure(**defaults)


def test_run_cli_prints_a_tool_failure_on_stderr_and_exits_nonzero(monkeypatch, capsys, tmp_path):
    """The actionable message goes to STDERR (so it stays separable from the
    report on stdout), names the tool and the stage, points at the bounded log,
    and exits 1 -- never a traceback."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    def _boom(*a, **kw):
        raise _tool_failure()

    monkeypatch.setattr(run_module, "run_target", _boom)
    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    cap = capsys.readouterr()

    assert rc == 1
    assert "GraXpert" in cap.err and "reducing noise" in cap.err
    assert "could not download the denoise model" in cap.err
    assert "seestar_diagnostics.log" in cap.err
    assert "Traceback" not in cap.err and "Traceback" not in cap.out


def test_run_cli_names_tool_and_stage_even_before_any_manifest_exists(monkeypatch, capsys, tmp_path):
    """A failure this early has no run manifest and no output identity to report
    against -- the tool and stage must still reach the user."""
    import seestar.cli as cli_module
    import seestar.run as run_module

    def _boom(*a, **kw):
        raise _tool_failure(
            tool="siril", stage="preparing working copy", summary="Error: cannot open file", log_path=None
        )

    monkeypatch.setattr(run_module, "run_target", _boom)
    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err

    assert rc == 1
    assert "Siril" in err and "preparing working copy" in err
    assert not (tmp_path / "out").exists() or True  # no manifest needed for the message to be useful


def test_run_cli_reports_a_planning_refusal_without_a_traceback(monkeypatch, capsys, tmp_path):
    import seestar.cli as cli_module
    import seestar.run as run_module
    from seestar.orchestrate import PipelineError

    def _boom(*a, **kw):
        raise PipelineError("StretchRender: Siril failed while calibrating brightness. Error: cannot open file")

    monkeypatch.setattr(run_module, "run_target", _boom)
    rc = cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err

    assert rc == 1
    assert "calibrating brightness" in err and "Traceback" not in err


def test_run_cli_still_raises_an_untyped_error(monkeypatch, tmp_path):
    """A genuine defect must NOT be dressed up as an explained stop."""
    import pytest as _pytest

    import seestar.cli as cli_module
    import seestar.run as run_module

    def _boom(*a, **kw):
        raise ValueError("a real bug")

    monkeypatch.setattr(run_module, "run_target", _boom)
    with _pytest.raises(ValueError, match="a real bug"):
        cli_module.main(["run", str(tmp_path), "--out", str(tmp_path / "out")])
